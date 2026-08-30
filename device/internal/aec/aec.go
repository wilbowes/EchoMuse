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

	// DC mean and absolute peak over the same window.
	//
	// rms alone cannot tell audio from a constant offset — both read high —
	// and that ambiguity cost an evening on #117/#141, where the device was
	// writing rms≈4000 to the codec while an external speaker on the jack
	// stayed silent. A DC offset has rms with no audible content AND trips
	// the protection circuit in a powered speaker, which would explain the
	// silence and why it also swallows voice. mean≈±rms with a small
	// peak-to-peak says offset; mean≈0 with peak well above rms says real
	// audio, and the fault is downstream of us.
	farSum  float64
	farPeak int32

	sizeWarned bool // one-shot guard for the unsupported-buffer-size log

	// Hardware far-end reference (#385). When set, the reference comes from
	// ch8 of the mic capture — the device's own playback, looped back in the
	// SAME TDM frame as the near-end samples — and the ring, the decimator,
	// aecDelayMs and the occupancy governor are all bypassed, because every
	// one of them exists to answer "where in time is the far end", which
	// arriving in the same frame answers by construction.
	//
	// Owned here rather than decided at the call site so WriteFar and
	// ProcessWithRef cannot disagree about which source is live: a period
	// pushed into the ring while the hardware path is running would be
	// consumed by nothing and sit there aging.
	hwRef bool
	// Periods cancelled against a hardware reference, and periods where one
	// was expected and did not arrive (nil, or a length mismatch with the
	// near end). A rising hwMissing with cancellation on is an extraction
	// fault, not a filter one, and the attenuation line alone cannot tell
	// them apart — those frames pass through uncancelled and simply read as
	// att≈0dB.
	//
	// Deliberately NOT a silence counter. A reference that is present and
	// all-zero is the correct state whenever nothing is playing, so counting
	// it would be counting idle; the case worth catching — zero WHILE the
	// speaker plays — is already visible as ref=0 against a loud mic on the
	// once-a-second attenuation line.
	hwFrames  uint64
	hwMissing uint64

	// Playback gain the hardware reference has NOT been through, as a linear
	// scalar (1.0 = the codec's unity gain, index 127). Measured on hardware:
	// the loopback is tapped upstream of the DAC volume control, so it holds
	// full scale whatever the user's volume — 0.431574 at index 127 against
	// 0.431560 at index 60, across a commanded 33.5dB cut.
	//
	// Left uncorrected, every volume change is a step in the echo path gain
	// that the adaptive filter can only discover by re-converging, and the
	// field log shows exactly that: cancellation collapsed to -1.7dB
	// immediately after a change and took 3-4 seconds to climb back, over
	// and over (2026-08-29). We are not obliged to guess it — the device
	// SETS this volume, so it knows the scalar precisely.
	refScale    float64
	scaleWarned bool // one-shot guard for the unscaled-reference log
}

// SetPlaybackLevel tells the canceller the DAC volume index the reference has
// not been through, so the hardware reference can be scaled to match what the
// speaker is actually emitting.
//
// The control is 0.5dB per step with unity at 127 (see Volume in
// device/CLAUDE.md), so the scalar is 10^((level-127)/40).
//
// SOFTWARE-TAP FRAMES ARE DELIBERATELY LEFT ALONE. That tap is pre-volume
// too, but its ring holds audio written BEFORE the change, so scaling it by
// the current level would apply the correction to the wrong samples — and
// keeping it untouched preserves the baseline the hardware path is being
// compared against.
func (c *Canceller) SetPlaybackLevel(level int) {
	if level < 0 {
		level = 0
	}
	scale := math.Pow(10, float64(level-127)/40.0)
	c.mu.Lock()
	defer c.mu.Unlock()
	if scale == c.refScale {
		return
	}
	c.refScale = scale
	log.Printf("[aec] playback level %d → reference scale %.4f (%.1fdB)",
		level, scale, 20*math.Log10(math.Max(scale, 1e-9)))
}

// SetHardwareRef selects the far-end source. True takes it from ch8 of the
// mic capture; false uses the software tap at the speaker ALSA write.
//
// Switching drops any ring contents: on the way in they would never be
// consumed, and on the way out they are stale by however long the hardware
// path ran. The filter state is deliberately KEPT — the physical echo path
// has not changed, only our view of the signal driving it, and the two
// references are the same audio to within the converter delay the filter
// already absorbs.
func (c *Canceller) SetHardwareRef(on bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.hwRef == on {
		return
	}
	c.hwRef = on
	c.head, c.tail, c.count = 0, 0, 0
	c.dsum, c.dcnt = 0, 0
	log.Printf("[aec] far-end reference: %s",
		map[bool]string{true: "hardware (ch8, frame-aligned)",
			false: "software tap (ring + aecDelayMs)"}[on])
}

// Enabled reports whether cancellation is armed. Callers use it to skip
// preparing a far-end reference that would be discarded.
//
// Note this is NOT the rare case: aecEnabled defaults TRUE controller-side
// (em_db.DEFAULT_CONFIG), so a stock device runs the extraction on every
// period. It is one allocation and a 512-sample copy per 32ms on the mic
// goroutine, which is affordable — but it is the common path, not the
// exception, so treat it as part of the steady-state budget.
func (c *Canceller) Enabled() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.enabled
}

// HardwareRef reports the current far-end source.
func (c *Canceller) HardwareRef() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.hwRef
}

// RefSource names the far-end reference in use, for the stats report:
// "hw" once ch8 has proved itself the playback loopback, "sw" for the tap at
// the ALSA write, "off" while cancellation is disarmed.
//
// Always one of those three, never empty: the controller reads an ABSENT
// aecRef as "firmware too old to say", and an empty string arriving as a
// fourth value would collapse that distinction.
func (c *Canceller) RefSource() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case !c.enabled:
		return "off"
	case c.hwRef:
		return "hw"
	default:
		return "sw"
	}
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
	// A delay-only change is a no-op on the hardware reference. delayMs
	// never reaches speex — it only seeds the ring — so on a path with no
	// ring the rebuild below would discard a converged filter to apply a
	// number nothing reads. That is not hypothetical: the value still rides
	// every config push, so one fleet-wide edit would reset cancellation on
	// every device using the hardware reference.
	//
	// Stored anyway, so a later fall back to the software tap seeds its
	// ring with the operator's current setting rather than a stale one.
	if c.hwRef && c.st != nil && enabled == c.enabled && tailMs == c.tailMs {
		c.delayMs = delayMs
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
	if c.hwRef {
		// Nothing drains the ring on the hardware path, so filling it would
		// peg it at ringCap and leave the far-end telemetry describing a
		// buffer no cancellation ever reads.
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
			c.farSum += float64(s)
			if a := int32(s); a < 0 {
				if -a > c.farPeak {
					c.farPeak = -a
				}
			} else if a > c.farPeak {
				c.farPeak = a
			}
			c.farSamples++
			c.dsum, c.dcnt = 0, 0
		}
	}
	// ~1/s while the far end carries real audio (playback): what the tap
	// is actually delivering, and where the ring sits.
	if c.farSamples >= sampleRate {
		rms := math.Sqrt(c.farSumSq / float64(c.farSamples))
		mean := c.farSum / float64(c.farSamples)
		if rms > 100 {
			log.Printf("[aec] far: rms=%.0f mean=%.0f peak=%d pushed=%d ring=%d",
				rms, mean, c.farPeak, c.farSamples, c.count)
		}
		c.farSamples, c.farSumSq, c.farSum, c.farPeak = 0, 0, 0, 0
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
	return c.process(mono, nil)
}

// ProcessWithRef cancels using a far-end reference supplied by the CALLER,
// taken from ch8 of the same raw mic period (#385). Both streams therefore
// come off one ADC clock in one TDM frame, so alignment is structural rather
// than inferred: the residual is the +33-sample (2.06ms) converter and
// acoustic delay measured on hardware, which sits far inside the filter tail
// and is absorbed like any other room delay. The measured polarity inversion
// is likewise learned by the filter.
//
// hwRef must be set (SetHardwareRef) or this falls back to the ring, so that
// a caller and the canceller cannot disagree about which reference is live.
// A nil or short ref is treated as "no reference this period" and the frame
// passes through uncancelled rather than being cancelled against silence —
// which would be indistinguishable from a working AEC with nothing playing.
func (c *Canceller) ProcessWithRef(mono, ref []byte) []byte {
	return c.process(mono, ref)
}

func (c *Canceller) process(mono, hwref []byte) []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled || c.st == nil {
		return mono
	}
	// The hardware path is only taken when both sides agree it is live and
	// the caller actually supplied a matching period.
	useHW := c.hwRef && hwref != nil && len(hwref) == len(mono)
	if c.hwRef && !useHW {
		// Counted, not logged per period: at 31 periods/s a log line here
		// would bury the very telemetry used to diagnose it.
		c.hwMissing++
		if c.hwMissing == 1 || c.hwMissing%256 == 0 {
			log.Printf("[aec] hardware reference missing or mismatched "+
				"(mic %db, ref %db, occurrences=%d) — frames passed through",
				len(mono), len(hwref), c.hwMissing)
		}
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
		if useHW {
			// Same frame, same clock — a straight copy, no ring, no delay
			// bookkeeping. This is the whole point of #385.
			hsub := hwref[off : off+FrameSize*2]
			// Unity if nobody has told us the volume yet. That should not
			// happen — cmd/server.go seeds the level from the device's own
			// tinymix reading as it wires the callback, before the control
			// client dials — but unity is the least-wrong guess, since a
			// zero reference cancels nothing and looks identical to a
			// working AEC with nothing playing.
			//
			// It is warned about because it is not free: the device boots
			// at whatever level the previous run left in tinymix, and if
			// that is (say) index 60, an unscaled reference is 33dB hot and
			// cancellation collapses exactly as it did in round one.
			scale := c.refScale
			if scale <= 0 {
				scale = 1.0
				if !c.scaleWarned {
					c.scaleWarned = true
					log.Printf("[aec] no playback level yet — hardware " +
						"reference running unscaled; cancellation will be " +
						"poor at any volume below unity")
				}
			}
			for i := 0; i < FrameSize; i++ {
				v := float64(int16(binary.LittleEndian.Uint16(hsub[i*2:]))) * scale
				// The scalar only ever attenuates (level <= 127 by
				// DEVICE_VOLUME_MAX), so this cannot clip in practice —
				// clamped anyway because a future ceiling change must not
				// silently wrap the reference to full-scale opposite sign.
				if v > 32767 {
					v = 32767
				} else if v < -32768 {
					v = -32768
				}
				ref[i] = int16(v)
			}
			c.hwFrames++
		} else {
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
				if useHW {
					log.Printf("[aec] att=%.1fdB mic=%.0f out=%.0f ref=%.0f "+
						"src=hw(ch8) frames=%d",
						att, inAvg, outAvg, refAvg, c.hwFrames)
				} else {
					log.Printf("[aec] att=%.1fdB mic=%.0f out=%.0f ref=%.0f ring=%d (delay=%dms)",
						att, inAvg, outAvg, refAvg, c.count, c.delayMs)
				}
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
	// Not on the hardware path: there is no ring to trim, delayMs means
	// nothing there, and leaving this running would log resyncs about a
	// buffer nothing is filling.
	if useHW {
		return out
	}
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
