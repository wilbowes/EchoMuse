// Package wakeword implements the openWakeWord streaming feature pipeline
// on the device: raw 16kHz PCM in, wake-word score out.
//
// The pipeline is three models chained per 80ms of audio —
//
//	audio → melspectrogram → (ring of mel frames) → embedding → (ring of
//	embeddings) → classifier → score
//
// — and this file contains ONLY the buffering between them. Inference itself
// sits behind the Inferer interface so this package stays pure Go: it builds
// and tests on the host with no ONNX Runtime, no cgo and no hardware, which
// is what lets stream_test.go verify it against a fixture captured from the
// Python implementation.
//
// The buffering is not incidental detail — it IS the algorithm. Every
// constant here mirrors openwakeword/utils.py (AudioFeatures), and the
// non-obvious ones are called out where they are used, because getting any
// of them wrong produces a detector that runs, scores plausibly, and is
// quietly wrong:
//
//   - the melspectrogram is computed over the new chunk PLUS 480 samples of
//     left context, so 1280 new samples yield 8 new mel frames, not 5;
//   - the mel ring starts pre-filled with 1.0, not zero or empty;
//   - the embedding window steps 8 frames but is 76 wide, so consecutive
//     windows overlap by 89%.
//
// Deliberate divergence from Python: upstream seeds its feature ring by
// running four seconds of RANDOM audio through the embedding model, so its
// first ~16 scores are computed against noise. This implementation starts
// empty and reports no score until 16 real embeddings exist (~1.28s). That
// is a behaviour change at startup only, and a better one — a score derived
// from random warm-up data is not a score. Ready() reports the state.
package wakeword

import (
	"errors"
	"fmt"
	"time"
)

const (
	// SampleRate is fixed by the melspectrogram model.
	SampleRate = 16000
	// ChunkSamples is the quantum the pipeline advances by: 80ms.
	ChunkSamples = 1280

	// MelBins is fixed by the melspectrogram model's output width.
	MelBins = 32
	// MelContextSamples (160*3) is prepended to each chunk before the
	// melspectrogram is computed. Without it a 1280-sample chunk yields 5
	// mel frames instead of the 8 the embedding window's step size assumes,
	// and the two rings drift apart silently.
	MelContextSamples = 480
	// MelWindow is the embedding model's input height (76 mel frames).
	MelWindow = 76
	// MelStep is how far the embedding window advances per chunk. 8 of 76,
	// i.e. consecutive embeddings share 89% of their input — the redundancy
	// that makes this the expensive stage.
	MelStep = 8
	// MelBufMax caps the mel ring at ~10s (10*97 frames).
	MelBufMax = 970

	// melScale and melOffset are applied to every melspectrogram model
	// output before it enters the ring (upstream's
	// `melspec_transform = lambda x: x/10 + 2`). It exists to bring the ONNX
	// export back in line with Google's original TensorFlow speech_embedding
	// input distribution, so it is not cosmetic scaling — drop it and the
	// embedding model receives out-of-distribution input and every score is
	// wrong while everything still runs. Applied here rather than in Inferer
	// so implementations of that interface stay thin model wrappers.
	melScale  = 10.0
	melOffset = 2.0

	// FeatDim is the embedding model's output width.
	FeatDim = 96
	// FeatWindow is how many embeddings the classifier consumes.
	FeatWindow = 16
	// FeatBufMax caps the embedding ring at ~10s of history.
	FeatBufMax = 120

	// rawBufMax caps the raw audio ring at 10s, matching upstream's deque.
	rawBufMax = SampleRate * 10
)

// ScoreSpan is how much audio one score looks at: FeatWindow embeddings
// MelStep mel frames apart, the last covering MelWindow frames of 10ms each
// (1.96s). A sound stays in the classifier's view this long after it ends.
const ScoreSpan = time.Duration((FeatWindow-1)*MelStep+MelWindow) * 10 * time.Millisecond

// Inferer is the boundary between buffering (this package, pure Go) and
// model execution (ONNX Runtime via cgo on device; a fixture in tests).
//
// All tensors are flat row-major float32, matching the ONNX layouts:
//
//	Melspec:  in  [samples]         -> out [frames*MelBins], frames returned
//	Embed:    in  [MelWindow*MelBins] -> out [FeatDim]
//	Classify: in  [FeatWindow*FeatDim] -> out scalar
type Inferer interface {
	Melspec(samples []float32) (mel []float32, frames int, err error)
	Embed(window []float32) ([]float32, error)
	Classify(feats []float32) (float32, error)
}

// Detector holds the streaming state for one audio source.
//
// Not safe for concurrent use: the device drives it from the single mic
// reader goroutine, which is also the only place ordering is meaningful.
type Detector struct {
	inf Inferer

	raw       []int16 // ring of raw samples, newest at the end
	remainder []int16 // samples not yet forming a whole ChunkSamples
	accum     int     // samples buffered since the last melspec run

	mel    []float32 // melFrames x MelBins, row-major
	feat   []float32 // featFrames x FeatDim, row-major
	scratch []float32 // reused Embed input, avoids a per-chunk allocation
}

// New returns a Detector with the mel ring pre-filled to MelWindow frames of
// 1.0 — upstream's `np.ones((76, 32))`. Starting empty (or at zero) changes
// the first second of embeddings, so this is load-bearing, not cosmetic.
func New(inf Inferer) *Detector {
	if inf == nil {
		panic("wakeword: nil Inferer")
	}
	mel := make([]float32, MelWindow*MelBins)
	for i := range mel {
		mel[i] = 1.0
	}
	return &Detector{
		inf:     inf,
		mel:     mel,
		scratch: make([]float32, MelWindow*MelBins),
	}
}

// Ready reports whether enough real embeddings have accumulated for the
// classifier to run. False for roughly the first 1.28s of audio.
func (d *Detector) Ready() bool { return len(d.feat)/FeatDim >= FeatWindow }

// Reset clears all streaming state, as if the Detector were new. Used when
// the mic stream restarts, so audio from before the gap cannot contribute
// to a score after it.
func (d *Detector) Reset() {
	d.raw = d.raw[:0]
	d.remainder = nil
	d.accum = 0
	d.feat = nil
	d.mel = make([]float32, MelWindow*MelBins)
	for i := range d.mel {
		d.mel[i] = 1.0
	}
}

// ErrNotReady is returned by Score before FeatWindow embeddings exist.
var ErrNotReady = errors.New("wakeword: fewer than 16 embeddings buffered")

// Push feeds PCM samples through the pipeline.
//
// Samples need not be a whole number of chunks; a partial tail is held and
// prepended to the next call, matching upstream's raw_data_remainder. n is
// the number of ChunkSamples-sized chunks processed, i.e. how many new
// embeddings were produced (0 if the input was shorter than a chunk).
//
// Push does NOT run the classifier — call Score for that. Separating them
// lets a caller push audio it does not intend to score (warm-up, or a
// stretch where the ring only needs to stay current).
func (d *Detector) Push(samples []int16) (n int, err error) {
	if len(d.remainder) > 0 {
		samples = append(append([]int16(nil), d.remainder...), samples...)
		d.remainder = nil
	}

	// Buffer only whole chunks; hold the tail. Upstream computes the
	// remainder against accum+len, not len alone, because accum may already
	// hold a partial chunk's worth from earlier calls.
	if d.accum+len(samples) >= ChunkSamples {
		if rem := (d.accum + len(samples)) % ChunkSamples; rem != 0 {
			d.remainder = append([]int16(nil), samples[len(samples)-rem:]...)
			samples = samples[:len(samples)-rem]
		}
	}
	d.bufferRaw(samples)
	d.accum += len(samples)

	if d.accum < ChunkSamples || d.accum%ChunkSamples != 0 {
		return 0, nil
	}

	chunks := d.accum / ChunkSamples
	if err := d.melspec(d.accum); err != nil {
		return 0, err
	}
	// One embedding per new chunk, oldest first. ndx walks backwards from
	// the newest mel frame in MelStep strides so window i ends MelStep*i
	// frames before the end — upstream's `ndx = -8*i`.
	melFrames := len(d.mel) / MelBins
	for i := chunks - 1; i >= 0; i-- {
		end := melFrames - MelStep*i
		start := end - MelWindow
		if start < 0 || end > melFrames {
			// Not enough history yet; upstream skips these too.
			continue
		}
		copy(d.scratch, d.mel[start*MelBins:end*MelBins])
		emb, err := d.inf.Embed(d.scratch)
		if err != nil {
			return 0, fmt.Errorf("wakeword: embed: %w", err)
		}
		if len(emb) != FeatDim {
			return 0, fmt.Errorf("wakeword: embed returned %d values, want %d", len(emb), FeatDim)
		}
		d.feat = append(d.feat, emb...)
		n++
	}
	if max := FeatBufMax * FeatDim; len(d.feat) > max {
		d.feat = append(d.feat[:0], d.feat[len(d.feat)-max:]...)
	}
	d.accum = 0
	return n, nil
}

// Score runs the classifier over the most recent FeatWindow embeddings.
// Returns ErrNotReady until enough have accumulated.
func (d *Detector) Score() (float32, error) {
	if !d.Ready() {
		return 0, ErrNotReady
	}
	feats := d.feat[len(d.feat)-FeatWindow*FeatDim:]
	s, err := d.inf.Classify(feats)
	if err != nil {
		return 0, fmt.Errorf("wakeword: classify: %w", err)
	}
	return s, nil
}

// bufferRaw appends to the raw ring, trimming from the front at rawBufMax.
func (d *Detector) bufferRaw(samples []int16) {
	d.raw = append(d.raw, samples...)
	if len(d.raw) > rawBufMax {
		d.raw = append(d.raw[:0], d.raw[len(d.raw)-rawBufMax:]...)
	}
}

// melspec computes the mel frames for the newest nSamples and appends them
// to the mel ring.
//
// The model is fed nSamples PLUS MelContextSamples of preceding audio, or
// the whole buffer when it holds less than that — which is exactly what
// happens on the very first chunk, where only 1280 samples exist and the
// model returns 5 frames instead of 8. Both cases are covered by the
// fixture, and getting the short case wrong only shows up as a one-off skew
// in the first second.
func (d *Detector) melspec(nSamples int) error {
	want := nSamples + MelContextSamples
	if want > len(d.raw) {
		want = len(d.raw)
	}
	src := d.raw[len(d.raw)-want:]

	in := make([]float32, len(src))
	for i, s := range src {
		// Raw int16 magnitudes, NOT normalised to ±1 — the melspectrogram
		// model was exported expecting int16-scale input, and normalising
		// here silently shifts every downstream score.
		in[i] = float32(s)
	}

	out, frames, err := d.inf.Melspec(in)
	if err != nil {
		return fmt.Errorf("wakeword: melspec: %w", err)
	}
	if frames*MelBins != len(out) {
		return fmt.Errorf("wakeword: melspec returned %d values for %d frames", len(out), frames)
	}
	for _, v := range out {
		d.mel = append(d.mel, v/melScale+melOffset)
	}
	if max := MelBufMax * MelBins; len(d.mel) > max {
		d.mel = append(d.mel[:0], d.mel[len(d.mel)-max:]...)
	}
	return nil
}
