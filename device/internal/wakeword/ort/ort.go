// Package ort implements wakeword.Inferer with ONNX Runtime, so the streaming
// pipeline in the parent package can actually score audio on the device.
//
// The runtime is dlopen'd at RUNTIME (see shim.h) rather than linked, so this
// package builds and the firmware boots with no libonnxruntime.so present —
// Open just returns an error and the caller keeps using controller-side wake
// word. Only ONNX Runtime's MIT-licensed C header is vendored, in include/;
// the 12.3MB library is distributed separately.
//
// Feeding it: the parent package owns all buffering and calls the three
// methods here in a fixed order per 80ms chunk. One Inferer belongs to one
// goroutine, matching wakeword.Detector's own contract. Inferers opened on the
// same Runtime with the same files share the ORT sessions, which hold no
// per-stream state (the streaming embedding's state is the Inferer's), so a
// scorer per microphone does not load the models again; each shared session
// runs one call at a time under its own mutex.
//
// Configuration is not a matter of taste. DefaultOptions encodes what was
// measured on an Echo Dot Gen 2 (Cortex-A7 @ ~1GHz, one core typically
// online): one thread, XNNPACK, spinning off — 36.2% of one core against
// 243% for ORT's own defaults. See Options.
package ort

/*
#cgo CFLAGS: -I${SRCDIR}/include -O2
#cgo LDFLAGS: -ldl

#include "shim.h"
*/
import "C"

import (
	"errors"
	"fmt"
	"math"
	"sync"
	"unsafe"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

// Tensor shapes, fixed by the openWakeWord model exports.
//
//	melspectrogram: [1, samples]        -> [1, 1, frames, 32]
//	embedding:      [1, 76, 32, 1]      -> [1, 1, 1, 96]
//	classifier:     [1, 16, 96]         -> [1, 1]
//
// Only the melspectrogram input is variable: the parent package feeds it the
// new chunk plus 480 samples of left context, or less on the very first chunk.

// Options controls session creation. The zero value is not useful — use
// DefaultOptions and adjust from there.
type Options struct {
	// Threads is the intra-op thread count. Keep at 1: the wake stream is
	// duty-cycled (29ms of work per 80ms frame), so extra threads cut latency
	// we do not need and raise total CPU, which we do (63% of a core at two
	// threads against 36% at one, measured).
	Threads int

	// XNNPACK requests the XNNPACK execution provider, a ~1.5x speedup with
	// bit-identical output. If the runtime lacks it the CPU provider is used
	// instead and XNNPACKActive reports false — a slower detector, not a
	// broken one.
	XNNPACK bool

	// AllowSpinning leaves ORT's thread-pool spin-wait enabled. It defaults
	// off and should stay off for anything duty-cycled: between inferences
	// the pool burns CPU waiting for work that arrives 60ms later.
	AllowSpinning bool
}

// DefaultOptions returns the configuration measured as optimal on the device.
func DefaultOptions() Options {
	return Options{Threads: 1, XNNPACK: true, AllowSpinning: false}
}

// Runtime is a dlopen'd ONNX Runtime library and the OrtEnv created against
// it, which owns the thread pools the sessions share.
type Runtime struct {
	rt      C.em_runtime
	version string

	mu      sync.Mutex
	shared  map[string]*model
	streams map[string]*sharedStream
}

// Open loads libonnxruntime.so from libPath (an absolute path, or a soname to
// be resolved by the dynamic linker) and creates its environment.
//
// Opening the SAME path twice returns the same Runtime rather than a second
// environment with a second set of thread pools — on a device with one core
// typically online, duplicating those is pure cost. Different paths are
// independent, which is what keeps a failure to load one library from being
// reported as a bookkeeping error about another.
func Open(libPath string) (*Runtime, error) {
	openMu.Lock()
	defer openMu.Unlock()

	if r := runtimes[libPath]; r != nil {
		return r, nil
	}

	cPath := C.CString(libPath)
	defer C.free(unsafe.Pointer(cPath))

	r := &Runtime{}
	if err := goErr(C.em_ort_open(cPath, &r.rt)); err != nil {
		return nil, fmt.Errorf("ort: open %s: %w", libPath, err)
	}
	r.version = C.GoString(C.em_ort_version(&r.rt))
	runtimes[libPath] = r
	return r, nil
}

var (
	openMu   sync.Mutex
	runtimes = map[string]*Runtime{}
)

// Version reports the loaded library's version string, e.g. "1.19.2".
func (r *Runtime) Version() string { return r.version }

// Models names the three ONNX files the pipeline needs. Melspec and Embedding
// are openWakeWord's shared feature models; Classifier is the per-wake-word
// head, so it is the only one that changes when the wake word does.
//
// EmbeddingStream is optional: the embedding model rewritten to take only the
// frames that are new (#893). Empty, missing or unusable, the stock model does
// the work; see Inferer.StreamStatus.
type Models struct {
	Melspec         string
	Embedding       string
	Classifier      string
	EmbeddingStream string
}

type model struct {
	m    C.em_model
	name string

	// For a session shared between Inferers: run holds mu, and the last
	// release frees it.
	mu   sync.Mutex
	key  string
	refs int
}

type sharedStream struct {
	mu   sync.Mutex
	s    C.em_stream
	key  string
	refs int
}

func shareKey(path string, o Options) string {
	return fmt.Sprintf("%s|%d|%v|%v", path, o.Threads, o.XNNPACK, o.AllowSpinning)
}

// acquire returns the session for path, loading it on first use.
func (r *Runtime) acquire(path, name string, o Options) (*model, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := shareKey(path, o)
	if m := r.shared[k]; m != nil {
		m.refs++
		return m, nil
	}
	m, err := r.load(path, name, o)
	if err != nil {
		return nil, err
	}
	m.key, m.refs = k, 1
	if r.shared == nil {
		r.shared = map[string]*model{}
	}
	r.shared[k] = m
	return m, nil
}

func (r *Runtime) release(m *model) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if m.refs--; m.refs > 0 {
		return
	}
	delete(r.shared, m.key)
	m.mu.Lock()
	C.em_model_free(&m.m)
	m.mu.Unlock()
}

func (r *Runtime) acquireStream(path string, o Options) (*sharedStream, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := shareKey(path, o)
	if s := r.streams[k]; s != nil {
		s.refs++
		return s, nil
	}
	cPath := C.CString(path)
	defer C.free(unsafe.Pointer(cPath))
	s := &sharedStream{key: k, refs: 1}
	if err := goErr(C.em_stream_load(&r.rt, cPath, C.int(o.Threads), cBool(o.XNNPACK),
		cBool(o.AllowSpinning), &s.s)); err != nil {
		return nil, err
	}
	if r.streams == nil {
		r.streams = map[string]*sharedStream{}
	}
	r.streams[k] = s
	return s, nil
}

func (r *Runtime) releaseStream(s *sharedStream) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if s.refs--; s.refs > 0 {
		return
	}
	delete(r.streams, s.key)
	s.mu.Lock()
	C.em_stream_free(&s.s)
	s.mu.Unlock()
}

func (r *Runtime) load(path, name string, o Options) (*model, error) {
	if path == "" {
		return nil, fmt.Errorf("ort: no path for the %s model", name)
	}
	cPath := C.CString(path)
	defer C.free(unsafe.Pointer(cPath))

	md := &model{name: name}
	err := goErr(C.em_model_load(&r.rt, cPath, C.int(o.Threads), cBool(o.XNNPACK),
		cBool(o.AllowSpinning), &md.m))
	if err != nil {
		return nil, fmt.Errorf("ort: load %s model %s: %w", name, path, err)
	}
	return md, nil
}

// Inferer runs the three models. It satisfies wakeword.Inferer.
type Inferer struct {
	rt            *Runtime
	mel, emb, cls *model

	// Output buffers, reused across calls: inference runs 12.5 times a second
	// forever, so the steady state must not allocate per frame. One per model,
	// which is what lets Melspec's result stay valid across an Embed call —
	// the parent package interleaves them.
	melOut, embOut, clsOut []float32

	xnnpack bool

	// The streaming embedding, nil when it is not in use, and why not.
	stream    *streamModel
	streamOff string
}

type streamModel struct {
	sh    *sharedStream
	state []float32
	emb   []float32
}

// NewInferer opens the three sessions, or shares them with an Inferer already
// open on the same files. The caller owns the result and must Close it; a
// partially-constructed Inferer is torn down on error rather than leaking
// sessions.
func (r *Runtime) NewInferer(m Models, o Options) (*Inferer, error) {
	if o.Threads < 1 {
		return nil, fmt.Errorf("ort: Threads must be at least 1, got %d", o.Threads)
	}
	inf := &Inferer{rt: r}
	var err error
	if inf.mel, err = r.acquire(m.Melspec, "melspectrogram", o); err != nil {
		return nil, err
	}
	// XNNPACK availability is a property of the runtime build, so whether it
	// attached to the first session settles it for all three.
	inf.xnnpack = inf.mel.m.xnnpack != 0
	if inf.emb, err = r.acquire(m.Embedding, "embedding", o); err != nil {
		inf.Close()
		return nil, err
	}
	if inf.cls, err = r.acquire(m.Classifier, "classifier", o); err != nil {
		inf.Close()
		return nil, err
	}
	inf.openStream(r, m.EmbeddingStream, o)
	return inf, nil
}

// openStream loads the streaming embedding model and proves it against the
// stock one before it is used. Nothing here can fail NewInferer: a device
// without the file, or with one that does not reproduce the stock model,
// scores exactly as it did before, for more CPU, and StreamStatus says why.
func (i *Inferer) openStream(r *Runtime, path string, o Options) {
	if path == "" {
		i.streamOff = "no streaming model given"
		return
	}
	sh, err := r.acquireStream(path, o)
	if err != nil {
		i.streamOff = "load: " + err.Error()
		return
	}
	sm := &streamModel{sh: sh, emb: make([]float32, wakeword.FeatDim)}
	sm.state = make([]float32, int(sh.s.total))
	i.stream = sm
	if err := i.verifyStream(); err != nil {
		r.releaseStream(sh)
		i.stream = nil
		i.streamOff = "self-check: " + err.Error()
		return
	}
	i.ResetStream()
}

// streamCheckSteps is how many steps the self-check feeds: enough to prime
// the state and compare three windows after it.
const streamCheckSteps = 12

// verifyStream feeds one fixed sequence of mel frames to both embedding
// models and requires the same answer. A streaming model made from a
// different embedding model, or by a rewrite that is wrong, loads and runs
// and would otherwise produce scores that are plausible and wrong, which
// nothing on the device or the controller would report. It costs three stock
// embeddings and twelve steps, once per scorer.
func (i *Inferer) verifyStream() error {
	const step = wakeword.MelStep * wakeword.MelBins
	mel := make([]float32, streamCheckSteps*step)
	seed := uint64(0x9E3779B97F4A7C15)
	for k := range mel {
		seed = seed*6364136223846793005 + 1442695040888963407
		// Mel values as the ring holds them: mostly 0..6, some well below.
		mel[k] = float32((seed>>33)%2000000)/1000000*6 - 6
	}
	i.ResetStream()
	compared := 0
	for j := 0; j < streamCheckSteps; j++ {
		got, err := i.EmbedStep(mel[j*step : (j+1)*step])
		if err != nil {
			return err
		}
		end := (j + 1) * wakeword.MelStep
		if end < wakeword.MelWindow || j < streamCheckSteps-3 {
			continue
		}
		window := mel[(end-wakeword.MelWindow)*wakeword.MelBins : end*wakeword.MelBins]
		want, err := i.Embed(window)
		if err != nil {
			return err
		}
		var worst, scale float64
		for k := range want {
			if d := math.Abs(float64(got[k]) - float64(want[k])); d > worst {
				worst = d
			}
			if a := math.Abs(float64(want[k])); a > scale {
				scale = a
			}
		}
		// The two providers agree to 5e-5 on values up to 66 (VVV,
		// 2026-10-13); a wrong model differs by the size of the values.
		if worst > 1e-3*math.Max(scale, 1) {
			return fmt.Errorf("differs from the stock model by %.3g on values up to %.3g", worst, scale)
		}
		compared++
	}
	if compared == 0 {
		return errors.New("compared nothing")
	}
	return nil
}

// Streaming reports whether the streaming embedding is in use.
func (i *Inferer) Streaming() bool { return i.stream != nil }

// StreamStatus is "on", or why the stock embedding is doing the work.
func (i *Inferer) StreamStatus() string {
	if i.stream != nil {
		return "on"
	}
	return i.streamOff
}

// ResetStream forgets the frames the streaming model has been fed.
func (i *Inferer) ResetStream() {
	if i.stream == nil {
		return
	}
	for k := range i.stream.state {
		i.stream.state[k] = 0
	}
}

// EmbedStep advances the streaming model by MelStep frames and returns the
// embedding of the MelWindow frames ending with them (wakeword.StreamEmbedder
// has the conditions). The slice is reused by the next call.
func (i *Inferer) EmbedStep(frames []float32) ([]float32, error) {
	sm := i.stream
	if sm == nil {
		return nil, errors.New("ort: no streaming embedding model")
	}
	if want := wakeword.MelStep * wakeword.MelBins; len(frames) != want {
		return nil, fmt.Errorf("ort: embed step wants %d values, got %d", want, len(frames))
	}
	sm.sh.mu.Lock()
	err := goErr(C.em_stream_run(&sm.sh.s, (*C.float)(unsafe.Pointer(&frames[0])),
		(*C.float)(unsafe.Pointer(&sm.state[0])), (*C.float)(unsafe.Pointer(&sm.emb[0]))))
	sm.sh.mu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("ort: run streaming embedding: %w", err)
	}
	return sm.emb, nil
}

// XNNPACKActive reports whether the XNNPACK provider was attached. False means
// the CPU provider is in use: same numbers, roughly 1.5x the CPU.
func (i *Inferer) XNNPACKActive() bool { return i.xnnpack }

// Close releases the sessions. Safe to call on a partially built Inferer and
// safe to call twice.
func (i *Inferer) Close() error {
	for _, m := range []*model{i.mel, i.emb, i.cls} {
		if m != nil {
			i.rt.release(m)
		}
	}
	i.mel, i.emb, i.cls = nil, nil, nil
	if i.stream != nil {
		i.rt.releaseStream(i.stream.sh)
		i.stream = nil
		i.streamOff = "closed"
	}
	return nil
}

// ErrClosed is returned by the inference methods after Close.
var ErrClosed = errors.New("ort: inferer is closed")

// Melspec computes mel frames for the given samples, which are raw int16
// magnitudes cast to float32 — NOT normalised to ±1. The parent package's
// melspec() is the only correct caller; see its comment for why.
//
// The returned slice aliases an internal buffer that the next Melspec call
// overwrites. That is safe for the parent package, which copies what it keeps
// into its mel ring before pushing more audio.
func (i *Inferer) Melspec(samples []float32) ([]float32, int, error) {
	if i.mel == nil {
		return nil, 0, ErrClosed
	}
	if len(samples) == 0 {
		return nil, 0, errors.New("ort: melspec called with no samples")
	}
	shape := [2]C.int64_t{1, C.int64_t(len(samples))}
	out, err := i.run(i.mel, &i.melOut, samples, shape[:])
	if err != nil {
		return nil, 0, err
	}
	if len(out)%wakeword.MelBins != 0 {
		return nil, 0, fmt.Errorf("ort: melspec produced %d values, not a multiple of %d bins",
			len(out), wakeword.MelBins)
	}
	return out, len(out) / wakeword.MelBins, nil
}

// Embed runs the embedding model over one 76x32 window.
func (i *Inferer) Embed(window []float32) ([]float32, error) {
	if i.emb == nil {
		return nil, ErrClosed
	}
	if want := wakeword.MelWindow * wakeword.MelBins; len(window) != want {
		return nil, fmt.Errorf("ort: embed wants %d values, got %d", want, len(window))
	}
	shape := [4]C.int64_t{1, wakeword.MelWindow, wakeword.MelBins, 1}
	out, err := i.run(i.emb, &i.embOut, window, shape[:])
	if err != nil {
		return nil, err
	}
	if len(out) != wakeword.FeatDim {
		return nil, fmt.Errorf("ort: embed produced %d values, want %d", len(out), wakeword.FeatDim)
	}
	return out, nil
}

// Classify runs the wake-word head over the 16x96 feature window and returns
// the score.
func (i *Inferer) Classify(feats []float32) (float32, error) {
	if i.cls == nil {
		return 0, ErrClosed
	}
	if want := wakeword.FeatWindow * wakeword.FeatDim; len(feats) != want {
		return 0, fmt.Errorf("ort: classify wants %d values, got %d", want, len(feats))
	}
	shape := [3]C.int64_t{1, wakeword.FeatWindow, wakeword.FeatDim}
	out, err := i.run(i.cls, &i.clsOut, feats, shape[:])
	if err != nil {
		return 0, err
	}
	if len(out) != 1 {
		return 0, fmt.Errorf("ort: classify produced %d values, want 1", len(out))
	}
	return out[0], nil
}

// run is the single path into C: it hands ORT a pointer to Go memory (read
// only for the duration of the call, which cgo permits for a slice of
// non-pointer data) and copies the malloc'd output into the caller's reusable
// buffer before returning, so no C allocation outlives this function and the
// steady state does not allocate.
func (i *Inferer) run(m *model, buf *[]float32, in []float32, shape []C.int64_t) ([]float32, error) {
	var (
		outPtr *C.float
		outN   C.size_t
	)
	m.mu.Lock()
	err := goErr(C.em_model_run(&m.m,
		(*C.float)(unsafe.Pointer(&in[0])), C.size_t(len(in)),
		&shape[0], C.size_t(len(shape)),
		&outPtr, &outN))
	m.mu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("ort: run %s: %w", m.name, err)
	}
	if outPtr == nil {
		return nil, fmt.Errorf("ort: run %s: no output", m.name)
	}
	defer C.free(unsafe.Pointer(outPtr))
	*buf = append((*buf)[:0], unsafe.Slice((*float32)(unsafe.Pointer(outPtr)), int(outN))...)
	return *buf, nil
}

// goErr converts the shim's malloc'd char* convention into a Go error, freeing
// the message. NULL means success.
func goErr(msg *C.char) error {
	if msg == nil {
		return nil
	}
	defer C.free(unsafe.Pointer(msg))
	return errors.New(C.GoString(msg))
}

func cBool(b bool) C.int {
	if b {
		return 1
	}
	return 0
}

// Compile-time proof that this package satisfies the interface the streaming
// pipeline is written against.
var _ wakeword.Inferer = (*Inferer)(nil)
