package ort

/*
#include "shim.h"
*/
import "C"

import (
	"fmt"
	"unsafe"
)

// VADChunk is the samples per Silero step. Two per 80ms frame; the frame's
// probability is their mean, which is how openwakeword's wrapper scores and
// what the controller's speech gate (em_speechgate) was measured with.
const VADChunk = 640

// vadState is the float count of each of h and c, [2,1,64]; EM_VAD_STATE in
// shim.h.
const vadState = 128

// VAD is a loaded Silero session. Safe for concurrent use: ORT sessions are,
// and the recurrent state lives in each VADStream.
type VAD struct {
	md *model
}

// VADStream is the recurrent state for one audio stream. Not safe for
// concurrent use.
type VADStream struct {
	v    *VAD
	h, c [vadState]float32
}

// NewVAD loads silero_vad.onnx. It must be the typed-field rewrite
// (controller/tools/silero_typed.py): as openwakeword ships it, ORT 1.19 on
// armv7 takes SIGBUS in CreateSession.
func (r *Runtime) NewVAD(path string, o Options) (*VAD, error) {
	md, err := r.load(path, "vad", o)
	if err != nil {
		return nil, err
	}
	return &VAD{md: md}, nil
}

// XNNPACKActive reports whether the XNNPACK provider attached.
func (v *VAD) XNNPACKActive() bool { return v.md.m.xnnpack != 0 }

// Stream starts a stream with clean state.
func (v *VAD) Stream() *VADStream { return &VADStream{v: v} }

// Prob scores samples (float, -1..1) and returns the mean speech probability
// over its VADChunk steps. A trailing partial chunk is ignored.
func (s *VADStream) Prob(samples []float32) (float32, error) {
	var sum float32
	n := 0
	for i := 0; i+VADChunk <= len(samples); i += VADChunk {
		var p C.float
		err := goErr(C.em_vad_run(&s.v.md.m, (*C.float)(unsafe.Pointer(&samples[i])), VADChunk,
			(*C.float)(unsafe.Pointer(&s.h[0])), (*C.float)(unsafe.Pointer(&s.c[0])), &p))
		if err != nil {
			return 0, fmt.Errorf("ort: run vad: %w", err)
		}
		sum += float32(p)
		n++
	}
	if n == 0 {
		return 0, nil
	}
	return sum / float32(n), nil
}

// Close releases the session. No stream may be used afterwards.
func (v *VAD) Close() { C.em_model_free(&v.md.m) }
