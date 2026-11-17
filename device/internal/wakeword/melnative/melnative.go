// Package melnative computes openWakeWord's melspectrogram without ONNX
// Runtime: a 512-point real FFT (pffft, NEON on armv7) per 10ms frame and
// the model's own 32-bin mel table, held to the ORT model by the golden
// fixture. It replaces only Inferer.Melspec; embedding and classification
// stay on ORT.
package melnative

/*
#cgo CFLAGS: -I${SRCDIR}/../../aec/src/pffft -O2
#cgo arm CFLAGS: -mfpu=neon
#cgo LDFLAGS: -lm
#include "pffft_rename.h"
#include "pffft.c"
#include "melnative.c"
*/
import "C"

import (
	"errors"
	"fmt"
	"math"
	"unsafe"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/fixture"
)

// Mel is one melspectrogram engine. Not safe for concurrent use.
type Mel struct {
	m   *C.em_mel
	out []float32
}

func New() (*Mel, error) {
	m := C.em_mel_new()
	if m == nil {
		return nil, errors.New("melnative: out of memory")
	}
	return &Mel{m: m}, nil
}

func (m *Mel) Close() {
	if m.m != nil {
		C.em_mel_free(m.m)
		m.m = nil
	}
}

// Frames is how many mel frames n samples produce.
func Frames(n int) int { return int(C.em_mel_frames(C.int(n))) }

// Melspec computes the raw melspectrogram (before the pipeline's /10+2), as
// ort.Inferer.Melspec does. The slice is reused by the next call.
func (m *Mel) Melspec(samples []float32) ([]float32, int, error) {
	if m.m == nil {
		return nil, 0, errors.New("melnative: closed")
	}
	frames := Frames(len(samples))
	if frames == 0 {
		return nil, 0, errors.New("melnative: fewer than 512 samples")
	}
	need := frames * wakeword.MelBins
	if cap(m.out) < need {
		m.out = make([]float32, need)
	}
	m.out = m.out[:need]
	n := C.em_mel_run(m.m, (*C.float)(unsafe.Pointer(&samples[0])), C.int(len(samples)),
		(*C.float)(unsafe.Pointer(&m.out[0])))
	return m.out, int(n), nil
}

// Hybrid is an Inferer that takes the melspectrogram from this package and
// everything else from inner.
type Hybrid struct {
	wakeword.Inferer
	mel *Mel
}

func NewHybrid(inner wakeword.Inferer) (*Hybrid, error) {
	m, err := New()
	if err != nil {
		return nil, err
	}
	return &Hybrid{Inferer: inner, mel: m}, nil
}

func (h *Hybrid) Melspec(samples []float32) ([]float32, int, error) { return h.mel.Melspec(samples) }

// The streaming embedder passes through when inner has one.
func (h *Hybrid) Streaming() bool {
	s, ok := h.Inferer.(wakeword.StreamEmbedder)
	return ok && s.Streaming()
}
func (h *Hybrid) EmbedStep(frames []float32) ([]float32, error) {
	s, ok := h.Inferer.(wakeword.StreamEmbedder)
	if !ok {
		return nil, errors.New("melnative: inner inferer does not stream")
	}
	return s.EmbedStep(frames)
}
func (h *Hybrid) ResetStream() {
	if s, ok := h.Inferer.(wakeword.StreamEmbedder); ok {
		s.ResetStream()
	}
}

// Close releases the native melspectrogram and the inner Inferer, when it
// owns resources (ORT's sessions).
func (h *Hybrid) Close() error {
	h.mel.Close()
	if c, ok := h.Inferer.(interface{ Close() error }); ok {
		return c.Close()
	}
	return nil
}

// Checked returns inner with this package's melspectrogram, once that agrees
// with inner's own on a fixed signal under the fixture's tolerance policy;
// otherwise nil and why, and the caller keeps inner as it is. Run once at
// load, on the device's own ORT and CPU: the host test holds the arithmetic
// to the golden fixture, this holds it to the model actually installed.
func Checked(inner wakeword.Inferer) (*Hybrid, string) {
	h, err := NewHybrid(inner)
	if err != nil {
		return nil, err.Error()
	}
	in := make([]float32, 1760) // one chunk's melspectrogram input
	for i := range in {
		// A tone over noise, then near-silence: both ends of the -80dB clip.
		v := float32((i*7919)%20001-10000) * 0.05
		if i < 1200 {
			v += 8000 * float32(math.Sin(float64(i)*0.07))
		}
		in[i] = v
	}
	want, wn, err := inner.Melspec(in)
	if err != nil {
		h.mel.Close()
		return nil, "the model's melspectrogram failed: " + err.Error()
	}
	want = append([]float32(nil), want...)
	got, gn, err := h.mel.Melspec(in)
	if err != nil || gn != wn {
		h.mel.Close()
		return nil, fmt.Sprintf("native melspectrogram: %d frames against %d, %v", gn, wn, err)
	}
	if d := fixture.Compare(got, want); !d.Ok() {
		h.mel.Close()
		return nil, fmt.Sprintf("native melspectrogram disagrees with the model: %d values outside tolerance, worst %.3g", d.N, d.Worst)
	}
	return h, ""
}
