// Package rnnoise provides Go bindings for the RNNoise noise suppression library.
//
// Vendored from https://github.com/xiph/rnnoise tag v0.1
// Commit: cdf196b1e9de2f8ff1003328ebf9a4316477429d
//
// RNNoise processes audio in 480-sample frames at 16kHz (30ms per frame).
package rnnoise

/*
#cgo CFLAGS: -I${SRCDIR}/include -I${SRCDIR}/src -DOUTSIDE_SPEEX -DRANDOM_PREFIX=rnnoise -O2
#cgo LDFLAGS: -lm

#include "include/rnnoise.h"
#include "src/celt_lpc.c"
#include "src/denoise.c"
#include "src/kiss_fft.c"
#include "src/pitch.c"
#include "src/rnn.c"
#include "src/rnn_data.c"
*/
import "C"
import "unsafe"

// FrameSize is the number of samples RNNoise processes per call (480 at 16kHz = 30ms).
const FrameSize = 480

// State holds the RNNoise denoising state for a single channel.
type State struct {
	st *C.DenoiseState
}

// New allocates and initialises a new RNNoise state.
func New() *State {
	return &State{st: C.rnnoise_create()}
}

// Destroy frees the RNNoise state. Must be called when done.
func (s *State) Destroy() {
	if s.st != nil {
		C.rnnoise_destroy(s.st)
		s.st = nil
	}
}

// ProcessFrame denoises exactly FrameSize (480) float32 samples.
// Input samples should be scaled to int16 range [-32768, 32767].
// Returns the VAD probability (0.0–1.0) for this frame.
func (s *State) ProcessFrame(out, in []float32) float32 {
	return float32(C.rnnoise_process_frame(
		s.st,
		(*C.float)(unsafe.Pointer(&out[0])),
		(*C.float)(unsafe.Pointer(&in[0])),
	))
}
