// Package aec provides acoustic echo cancellation for the mic pipeline
// using the speexdsp echo canceller (MDF/AUMDF), vendored from
// https://github.com/xiph/speexdsp tag SpeexDSP-1.2.1 (libspeexdsp/, BSD).
//
// The canceller consumes two streams: the near-end mic signal (16kHz mono
// S16, 512-sample periods — the beamformer's output, pre-NS/AGC) and a
// far-end reference of what the speaker is playing. The reference is tapped
// at the ALSA write in the speaker silence loop (48kHz stereo S16, every
// period *including silence*, so the reference clock advances in lockstep
// with playback), downmixed and 3:1 box-decimated to 16kHz mono here, and
// buffered in a ring the mic goroutine drains one period at a time.
//
// Alignment: both PCM devices sit on the same codec clock, so the streams
// cannot drift — ring occupancy is set once by the configured bulk delay
// and stays put. The delay models write-to-ear latency (ALSA output
// buffering ~340ms at 4×2048 frames / 48kHz, minus input-side buffering);
// the echo filter tail only has to absorb the residual mismatch plus room
// reverb, not the whole pipeline latency.
package aec

/*
#cgo CFLAGS: -I${SRCDIR}/include -I${SRCDIR}/src -DFLOATING_POINT -DUSE_KISS_FFT -DEXPORT= -O2
#cgo LDFLAGS: -lm

#include <stdlib.h>
#include "speex/speex_echo.h"
#include "src/fftwrap.c"
#include "src/kiss_fft.c"
#include "src/kiss_fftr.c"
#include "src/mdf.c"
*/
import "C"

import (
	"encoding/binary"
	"log"
	"sync"
	"unsafe"
)

const (
	sampleRate = 16000
	// FrameSize matches the mic pipeline period (512 samples = 32ms).
	FrameSize = 512

	// Reference ring capacity: max bulk delay (1s) plus 2s of slack.
	// 48k samples of int16 = 96KB.
	ringCap = 3 * sampleRate

	// Parameter clamps.
	maxDelayMs = 1000
	minTailMs  = 50
	maxTailMs  = 500
)

// Canceller is a single AEC instance shared by the speaker goroutine
// (WriteFar) and the mic goroutine (Process). One mutex guards everything —
// both call sites run at tens of hertz on multi-millisecond periods, so
// contention is irrelevant next to correctness.
type Canceller struct {
	mu      sync.Mutex
	enabled bool
	delayMs int
	tailMs  int

	st *C.SpeexEchoState

	// Far-end reference ring (16kHz mono), plus the 3:1 decimator carry.
	ring  [ringCap]int16
	head  int // next write index
	tail  int // next read index
	count int // samples buffered
	dsum  int32
	dcnt  int

	// C-side scratch buffers, allocated once per state init.
	micBuf *C.spx_int16_t
	refBuf *C.spx_int16_t
	outBuf *C.spx_int16_t

	underruns uint64 // ref ring empty while enabled (diagnostic)
}

// New returns a disabled Canceller. Call SetParams (config push) to arm it.
func New() *Canceller {
	return &Canceller{}
}

// SetParams applies config. Any change to delay or tail rebuilds the echo
// state and re-seeds the ring — adaptive filter state is worthless across a
// timing change anyway. Called from the control goroutine on config push.
func (c *Canceller) SetParams(enabled bool, delayMs, tailMs int) {
	if delayMs < 0 {
		delayMs = 0
	}
	if delayMs > maxDelayMs {
		delayMs = maxDelayMs
	}
	if tailMs < minTailMs {
		tailMs = minTailMs
	}
	if tailMs > maxTailMs {
		tailMs = maxTailMs
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if enabled == c.enabled && delayMs == c.delayMs && tailMs == c.tailMs {
		return
	}
	c.freeLocked()
	c.enabled = enabled
	c.delayMs = delayMs
	c.tailMs = tailMs
	if !enabled {
		log.Printf("[aec] disabled")
		return
	}

	tailSamples := C.int(tailMs * sampleRate / 1000)
	c.st = C.speex_echo_state_init(C.int(FrameSize), tailSamples)
	rate := C.spx_int32_t(sampleRate)
	C.speex_echo_ctl(c.st, C.SPEEX_ECHO_SET_SAMPLING_RATE, unsafe.Pointer(&rate))

	c.micBuf = (*C.spx_int16_t)(C.malloc(FrameSize * 2))
	c.refBuf = (*C.spx_int16_t)(C.malloc(FrameSize * 2))
	c.outBuf = (*C.spx_int16_t)(C.malloc(FrameSize * 2))

	// Seed the ring with the bulk delay as silence: the mic goroutine then
	// reads reference samples delayMs behind their ALSA write, aligning
	// them with when the sound actually reaches the mics.
	c.head, c.tail, c.count = 0, 0, 0
	c.dsum, c.dcnt = 0, 0
	delaySamples := delayMs * sampleRate / 1000
	for i := 0; i < delaySamples; i++ {
		c.pushLocked(0)
	}
	log.Printf("[aec] enabled: frame=%d tail=%dms delay=%dms", FrameSize, tailMs, delayMs)
}

func (c *Canceller) freeLocked() {
	if c.st != nil {
		C.speex_echo_state_destroy(c.st)
		c.st = nil
		C.free(unsafe.Pointer(c.micBuf))
		C.free(unsafe.Pointer(c.refBuf))
		C.free(unsafe.Pointer(c.outBuf))
		c.micBuf, c.refBuf, c.outBuf = nil, nil, nil
	}
}

func (c *Canceller) pushLocked(s int16) {
	c.ring[c.head] = s
	c.head = (c.head + 1) % ringCap
	if c.count < ringCap {
		c.count++
	} else {
		c.tail = (c.tail + 1) % ringCap // overwrite oldest
	}
}

// WriteFar feeds one speaker period (48kHz stereo S16LE — audio or silence)
// into the reference ring. Called from the speaker ALSA goroutine for every
// period pumped. Downmix: (L+R)/2; decimate: mean of 3 (box low-pass —
// crude, but the echo content is voice-band and the canceller adapts to
// the filter's response like any other part of the echo path).
func (c *Canceller) WriteFar(period []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled {
		return
	}
	n := len(period) / 4 // frames (2ch × 2 bytes)
	for i := 0; i < n; i++ {
		l := int16(binary.LittleEndian.Uint16(period[i*4:]))
		r := int16(binary.LittleEndian.Uint16(period[i*4+2:]))
		c.dsum += (int32(l) + int32(r)) / 2
		c.dcnt++
		if c.dcnt == 3 {
			c.pushLocked(int16(c.dsum / 3))
			c.dsum, c.dcnt = 0, 0
		}
	}
}

// Process runs echo cancellation on one mic period (16kHz mono S16LE,
// FrameSize samples). Returns the input unchanged when disabled or on a
// size mismatch. Called from the mic goroutine.
func (c *Canceller) Process(mono []byte) []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled || c.st == nil || len(mono) != FrameSize*2 {
		return mono
	}

	mic := unsafe.Slice((*int16)(unsafe.Pointer(c.micBuf)), FrameSize)
	ref := unsafe.Slice((*int16)(unsafe.Pointer(c.refBuf)), FrameSize)
	for i := 0; i < FrameSize; i++ {
		mic[i] = int16(binary.LittleEndian.Uint16(mono[i*2:]))
	}
	short := 0
	for i := 0; i < FrameSize; i++ {
		if c.count > 0 {
			ref[i] = c.ring[c.tail]
			c.tail = (c.tail + 1) % ringCap
			c.count--
		} else {
			ref[i] = 0
			short++
		}
	}
	if short > 0 {
		c.underruns++
		if c.underruns == 1 || c.underruns%256 == 0 {
			log.Printf("[aec] reference underrun (%d samples short, total underruns=%d)", short, c.underruns)
		}
	}

	C.speex_echo_cancellation(c.st, c.micBuf, c.refBuf, c.outBuf)

	out := make([]byte, FrameSize*2)
	res := unsafe.Slice((*int16)(unsafe.Pointer(c.outBuf)), FrameSize)
	for i := 0; i < FrameSize; i++ {
		binary.LittleEndian.PutUint16(out[i*2:], uint16(res[i]))
	}
	return out
}
