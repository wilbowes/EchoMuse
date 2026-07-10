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
// cannot drift — but mic capture overruns (the mic ALSA ring is only 160ms
// deep; any longer stall of the reader loses whole batches) leave the ring
// with excess reference, which the occupancy governor in Process trims
// back to the nominal delay. The delay models write-to-ear latency (ALSA output
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
	"math"
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
	resyncs   uint64 // stale-reference trims (see governor in Process)

	// Attenuation telemetry (2026-07-08): live cancellation has measured
	// ≈0dB across every delay setting while the synthetic test shows 42dB —
	// log the actual numbers instead of inferring them controller-side.
	// Accumulated per Process call, reported ~1/s while the reference is
	// active (i.e. during playback), then reset.
	statFrames  int
	statInSum   float64 // Σ mic-frame rms (pre-AEC)
	statOutSum  float64 // Σ output-frame rms (post-AEC)
	statRefSum  float64 // Σ reference-frame rms

	// Far-end telemetry: what WriteFar actually receives and pushes,
	// counted in pushed (16kHz) samples. Logged ~1/s while the far end is
	// loud — pairing this with the Process-side line tells whether a dead
	// reference is a tap problem (no loud far lines during playback) or a
	// ring/consumer problem (loud far lines, quiet ref in Process).
	farSamples int
	farSumSq   float64

	sizeWarned bool // one-shot guard for the unsupported-buffer-size log
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
			s := int16(c.dsum / 3)
			c.pushLocked(s)
			c.farSumSq += float64(s) * float64(s)
			c.farSamples++
			c.dsum, c.dcnt = 0, 0
		}
	}
	// ~1/s while the far end carries real audio (playback): what the tap
	// is actually delivering, and where the ring sits.
	if c.farSamples >= sampleRate {
		rms := math.Sqrt(c.farSumSq / float64(c.farSamples))
		if rms > 100 {
			log.Printf("[aec] far: rms=%.0f pushed=%d ring=%d", rms, c.farSamples, c.count)
		}
		c.farSamples, c.farSumSq = 0, 0
	}
}

// Process runs echo cancellation on one mic buffer: 16kHz mono S16LE, any
// multiple of FrameSize samples. The mic ALSA reader does NOT deliver single
// 512-sample periods — GoTinyAlsa's GetAudioStream reads pcm_get_buffer_size
// per chunk (PeriodSize × PeriodCount = 2560 frames = 160ms), so the buffer
// arriving here is 5 speex frames long. The pre-2026-07-08 version of this
// guard required exactly one frame and silently passed everything through —
// AEC had therefore never processed a single sample on hardware (ring pegged
// at ringCap, 0dB cancellation at every delay setting, zero underruns to give
// it away) while the unit tests, which feed single frames, showed 42dB.
// Hence: any size this function cannot handle is LOGGED, never silently
// bypassed. Called from the mic goroutine.
func (c *Canceller) Process(mono []byte) []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled || c.st == nil {
		return mono
	}
	if len(mono) == 0 || len(mono)%(FrameSize*2) != 0 {
		if !c.sizeWarned {
			c.sizeWarned = true
			log.Printf(
				"[aec] mic buffer %db is not a multiple of the %db speex frame — AEC BYPASSED",
				len(mono), FrameSize*2,
			)
		}
		return mono
	}

	out := make([]byte, len(mono))
	mic := unsafe.Slice((*int16)(unsafe.Pointer(c.micBuf)), FrameSize)
	ref := unsafe.Slice((*int16)(unsafe.Pointer(c.refBuf)), FrameSize)
	res := unsafe.Slice((*int16)(unsafe.Pointer(c.outBuf)), FrameSize)

	for off := 0; off < len(mono); off += FrameSize * 2 {
		sub := mono[off : off+FrameSize*2]
		for i := 0; i < FrameSize; i++ {
			mic[i] = int16(binary.LittleEndian.Uint16(sub[i*2:]))
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
		for i := 0; i < FrameSize; i++ {
			binary.LittleEndian.PutUint16(out[off+i*2:], uint16(res[i]))
		}

		// Attenuation telemetry: fires while the speaker is playing
		// (reference above the silence floor) — and also when the mic is
		// loud with a quiet reference, the broken state this telemetry was
		// built to catch. ~1 line/s of active audio.
		refRMS := frameRMS(ref)
		micRMS := frameRMS(mic)
		if refRMS > 100 || micRMS > 500 { // int16 units; idle floor is well below both
			c.statFrames++
			c.statInSum += micRMS
			c.statOutSum += frameRMS(res)
			c.statRefSum += refRMS
			if c.statFrames == 32 { // 32 × 32ms ≈ 1s
				inAvg, outAvg, refAvg := c.statInSum/32, c.statOutSum/32, c.statRefSum/32
				att := 0.0
				if outAvg > 0 {
					att = 20 * math.Log10(inAvg/outAvg)
				}
				log.Printf("[aec] att=%.1fdB mic=%.0f out=%.0f ref=%.0f ring=%d (delay=%dms)",
					att, inAvg, outAvg, refAvg, c.count, c.delayMs)
				c.statFrames, c.statInSum, c.statOutSum, c.statRefSum = 0, 0, 0, 0
			}
		}
	}

	// Occupancy governor: the ring must sit at ~delaySamples. WriteFar fills
	// it continuously (every speaker period, silence included — that's what
	// keeps the reference clock advancing), but this consumer stops whenever
	// the mic stream does — and the mic stream is stopped/restarted around
	// every voice turn. Each ~1s gap leaves ~16k unconsumed samples behind;
	// production and consumption rates are identical, so the backlog never
	// drains on its own — it compounds per turn until the ring pegs at
	// ringCap and the reference runs a full 3s behind the echo. Trimming
	// back to the nominal delay makes every gap self-heal within one call.
	// Runs AFTER the consume loop (the low-water point): the mic delivers
	// bursty 160ms batches, so occupancy measured before consuming swings
	// by a whole batch and would need slack so wide it re-opens the stale
	// window. Slack of 4 speex frames (128ms) clears producer/consumer
	// phase jitter (~±1 speaker period ≈ 43ms). The filter state is KEPT
	// across the trim: trimming restores the nominal delaySamples alignment
	// — the same alignment the filter converged against — and the physical
	// echo path hasn't changed, so the learned filter is still valid.
	// (2026-07-10: the reset that used to live here was the barge-in
	// killer — mic capture overruns trip this governor every ~20s in
	// steady state, incl. mid-playback, and each reset threw away a
	// converged filter for a ≤43ms alignment shift speexdsp tracks fine.)
	delaySamples := c.delayMs * sampleRate / 1000
	if c.count > delaySamples+4*FrameSize {
		drop := c.count - delaySamples
		c.tail = (c.tail + drop) % ringCap
		c.count = delaySamples
		c.resyncs++
		log.Printf("[aec] reference resync: dropped %d stale samples, filter kept (resyncs=%d)", drop, c.resyncs)
	}

	return out
}

func frameRMS(s []int16) float64 {
	var sum float64
	for _, v := range s {
		f := float64(v)
		sum += f * f
	}
	return math.Sqrt(sum / float64(len(s)))
}
