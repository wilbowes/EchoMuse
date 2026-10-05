// Package cue synthesises the device's own audible cues.
//
// The wake cue (#120) is the first sound this device has ever made of its own
// accord — everything else it plays came off the wire. It exists because the
// LED ring is the ONLY indication that the device is listening, and a ring is
// no use from the next room (@tvories' original report) and no use at all to a
// blind or low-vision user, which is the framing that makes it an
// accessibility setting rather than a nicety.
//
// GENERATED, NOT SHIPPED AS AN ASSET. A synthesised tone needs no distribution
// path, no md5 dance, no OTA payload and no second copy to keep in step with
// the firmware — compare `em_oww_assets`, which exists entirely to get 15MB of
// wake-word runtime onto a device. Two sine bursts cost a few hundred bytes of
// code and are exact on every device.
//
// Pure Go, no build tag, no hardware: this is arithmetic and the whole point is
// that it is testable on the host. The speaker binding mixes what it returns.
package cue

import "math"

// The wake cue: a rising two-tone. Lower note, then higher — "uh huh, I'm
// listening" (Wil, 2026-08-22).
//
// A RISING interval is the whole design. Falling reads as dismissal or error
// ("nope"), level reads as a notification; rising is the questioning,
// attentive shape, and it is what every assistant that has tested this on
// people converged on. 660Hz to 880Hz is a perfect fourth, the most
// unambiguous rise available in a short span.
//
// The pitches also sit where this hardware is good. The bass guard removes
// everything below 115Hz because the driver cannot deliver it, and stock's own
// correction boosts 150–250Hz by ~25dB (#247) — a small speaker's useful range
// is the midrange, and 660/880Hz is squarely in it. A cue pitched low would be
// the one thing on this device guaranteed to be inaudible across a room.
const (
	BingHz = 660.0 // E5
	BongHz = 880.0 // A5 — a perfect fourth above

	bingMS = 90.0
	bongMS = 130.0
	// A short gap makes it two notes rather than a glissando. Without it the
	// phase discontinuity at the pitch change is also audible as a tick.
	gapMS = 25.0

	// Raised-cosine edges. A sine burst that starts or stops at full
	// amplitude is a step, and a step is a click — the same discontinuity
	// the output chain's crossfade exists to avoid, arrived at from the
	// other direction. 4ms is inaudible as a fade and completely removes it.
	edgeMS = 4.0

	// The volume cue is one compact electronic beep rather than the wake cue's
	// bright rising interval: it confirms a level change without saying "I'm
	// listening". Its E4/F4 pitch is an octave below the wake cue. A short level
	// body establishes the beep, then the same note decays for a fraction of a
	// second — noticeable, but without a separate echo or second stage. The
	// fundamental deliberately dominates; the small-speaker output otherwise
	// makes upper harmonics sound much brighter than the same preview on a Mac.
	//
	// Its peak is scaled by the device volume passed to VolumeCue, so it is
	// useful as an audible preview of that level rather than a fixed alert.
	volumeHz        = 350.0
	volumeHoldMS    = 36.0
	volumeTailMS    = 125.0
	volumeSecondMix = 0.03
	volumeMS        = edgeMS + volumeHoldMS + volumeTailMS
	// The cue bypasses EQ/output processing so its timbre is invariant, but
	// follows the same VolumeGain curve as playback. This is the REAL rendered
	// peak after the beep envelope, not merely its oscillator drive. A pure
	// low tone has much higher sustained energy than typical programme audio;
	// 3dB of headroom keeps the amplifier and small driver clean at maximum.
	volumePeakDB = -3.0
)

// The wake sound's three levels, as set by `wakeSoundLevel`.
const (
	LevelQuiet  = "quiet"
	LevelMedium = "medium"
	LevelLoud   = "loud"
)

// Levels lists them quietest first.
var Levels = []string{LevelQuiet, LevelMedium, LevelLoud}

// LevelDBFS is a level's peak in dBFS; anything unrecognised is medium.
//
// ABSOLUTE levels: the cue is mixed after the software volume, with the DAC
// at unity, so these are what reaches the speaker whatever the volume. For
// scale, the first cue peaked at -10dBFS before a typical volume of 85
// (-21dB), about -31dBFS here, and was heard as understated (Wil,
// 2026-09-24). Starting points, 10dB apart, to be set by ear.
func LevelDBFS(level string) float64 {
	switch level {
	case LevelQuiet:
		return -30
	case LevelLoud:
		return -10
	}
	return -20
}

// WakeCue renders the cue at the given sample rate and peak level, in S16
// units (±32768) to match everything else on the speaker path.
func WakeCue(sampleRate int, peakDBFS float64) []float64 {
	fs := float64(sampleRate)
	amp := math.Pow(10, peakDBFS/20.0) * 32768.0

	bing := note(fs, BingHz, bingMS, amp)
	gap := int(fs * gapMS / 1000.0)
	bong := note(fs, BongHz, bongMS, amp)

	out := make([]float64, 0, len(bing)+gap+len(bong))
	out = append(out, bing...)
	out = append(out, make([]float64, gap)...)
	out = append(out, bong...)
	return out
}

// VolumeCue renders the physical-volume-button confirmation tone. volumeGain
// is the linear gain for the NEW device volume (speaker.VolumeGain at the call
// site). The cue mixer sits after software volume, so the gain is baked into
// these samples exactly once.
func VolumeCue(sampleRate int, volumeGain float64) []float64 {
	if volumeGain <= 0 {
		return nil
	}
	if volumeGain > 1 {
		volumeGain = 1
	}
	peak := math.Pow(10, volumePeakDB/20.0) * 32768.0 * volumeGain
	return decayBeep(float64(sampleRate), volumeHz, volumeHoldMS, volumeTailMS, volumeSecondMix, peak)
}

// VolumeButtonPreviewDue reports whether a physical button result merits a
// cue. Ordinary changes do; an unchanged Volume Up at the ceiling does too,
// so repeated presses still tell the person that the device is already at
// maximum. Volume Down at the floor remains silent because only the upper
// boundary was requested as an audible limit indication.
func VolumeButtonPreviewDue(direction string, changed, atMax bool) bool {
	return changed || (direction == "up" && atMax)
}

// decayBeep renders a single-stage electronic volume-button tone. Its short
// level body reads as a beep; the exponential release lets that same note hang
// in the air briefly without producing a separate reflection or second note.
func decayBeep(fs, freq, holdMS, tailMS, secondMix, amp float64) []float64 {
	n := int(fs * (edgeMS + holdMS + tailMS) / 1000.0)
	if n <= 0 {
		return nil
	}
	edge := int(fs * edgeMS / 1000.0)
	if edge*2 > n {
		edge = n / 2
	}
	holdEnd := edge + int(fs*holdMS/1000.0)

	out := make([]float64, n)
	for i := range out {
		t := float64(i) / fs
		phase := 2 * math.Pi * freq * t
		// A trace of the wake cue's second harmonic keeps its family resemblance,
		// but the 350Hz fundamental owns the sound on the Echo's raw speaker path.
		sample := math.Sin(phase) + secondMix*math.Sin(2*phase)

		env := 1.0
		if i < edge {
			env = 0.5 * (1 - math.Cos(math.Pi*float64(i)/float64(edge)))
		} else if i >= holdEnd {
			tailX := float64(i-holdEnd) / float64(n-holdEnd)
			env = math.Exp(-4.2 * tailX)
			if i >= n-edge {
				k := float64(n - 1 - i)
				env *= 0.5 * (1 - math.Cos(math.Pi*k/float64(edge)))
			}
		}
		out[i] = sample * env
	}

	// Normalise the rendered waveform, including its fullness components, to
	// the promised maximum-volume peak.
	rawPeak := 0.0
	for _, sample := range out {
		rawPeak = math.Max(rawPeak, math.Abs(sample))
	}
	if rawPeak > 0 {
		scale := amp / rawPeak
		for i := range out {
			out[i] *= scale
		}
	}
	return out
}

// note renders one tone with raised-cosine edges and a gentle decay.
//
// The decay is what makes it a "bing" rather than a "beep": a flat-topped
// burst reads as a machine alert, a decaying one reads as something struck.
// It is shallow — this has to stay audible for its whole length across a room.
func note(fs, freq, ms, amp float64) []float64 {
	n := int(fs * ms / 1000.0)
	if n <= 0 {
		return nil
	}
	edge := int(fs * edgeMS / 1000.0)
	if edge*2 > n {
		edge = n / 2
	}

	out := make([]float64, n)
	w := 2 * math.Pi * freq / fs
	for i := 0; i < n; i++ {
		t := float64(i)
		// A touch of second harmonic gives the note some body on a small
		// driver, where a pure sine can read as thin and synthetic.
		s := math.Sin(w*t) + 0.12*math.Sin(2*w*t)

		// Shallow exponential decay across the note.
		decay := math.Exp(-2.2 * t / float64(n))

		// Raised-cosine edges, applied last so they always win: whatever the
		// decay is doing, the first and last samples are zero.
		env := 1.0
		if i < edge {
			env = 0.5 * (1 - math.Cos(math.Pi*float64(i)/float64(edge)))
		} else if i >= n-edge {
			k := float64(n - 1 - i)
			env = 0.5 * (1 - math.Cos(math.Pi*k/float64(edge)))
		}

		out[i] = amp * s * decay * env / 1.12 // normalise for the harmonic
	}
	return out
}

// DurationMS is how long the whole cue lasts — needed by the mic path, which
// has to know which frames to exclude from the wake scorer and the ASR stream.
// The device is the one making the sound, so it can say exactly; that is the
// main reason the cue is generated here rather than sent by the controller.
func DurationMS() float64 { return bingMS + gapMS + bongMS }
