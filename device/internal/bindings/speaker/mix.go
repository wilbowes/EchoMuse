package speaker

import "math"

// Ducking and mixing for the two playback streams.
//
// No build tag and no tinyalsa import, deliberately: this is the arithmetic
// the ALSA write path runs on every period, and it is the part worth testing
// on the host. pcm_speaker.go is `//go:build server` and cannot be compiled
// or tested anywhere but the device.
//
// WHY THE DEVICE MIXES AT ALL. Barge-in used to pause the music, answer, and
// resume. Every assistant people compare us to ducks instead, and pausing
// carried real bugs with it: a Music Assistant flow stream cannot be seeked,
// so a 28-second turn cost 28 seconds of the song, and the media_player
// entity had to report PLAYING while internally paused (#62) or Home
// Assistant would decline the user's own pause.
//
// It has to happen HERE rather than in the controller because of `LEAD_S`:
// the music feed runs 4 seconds ahead of realtime, so when a wake word fires
// the next 4 seconds of music are already in this device's buffer. Audio
// that has left the controller cannot be ducked by the controller. Doing it
// on the device means the music keeps its full ~5.5s of link-stall
// protection AND ducking is instant, because the gain is applied to audio we
// are already holding.

// Q15 fixed point: 32768 == unity. Integer maths on a 32-bit A53 with no
// FPU pressure on the audio path, and exact at unity — a float multiply
// would leave a stream that is nominally not ducked very slightly altered.
const unityGain int32 = 1 << 15

// duckRampPeriods — periods taken to traverse the FULL gain range. A period
// is ~42.7ms, so 4 gives a ~170ms ramp for a duck from unity to silence, and
// proportionally less for a shallower one. Fast enough that the duck lands
// with the wake word, slow enough to be a fade rather than a step: stepping
// the gain at a period boundary is an audible click, landing on exactly the
// transition the user is listening to.
//
// A CONSTANT SLEW, not a proportional one. `(target-gain)/n` per period is
// an exponential approach: 4 is then a time constant, not a duration, and
// the gain crawls the last few percent for over a second — measured at 31
// periods (1.3s) to settle, against the 170ms this comment used to claim.
const duckRampPeriods = 4

// rampStep is the most the gain may move in one period.
const rampStep = unityGain / duckRampPeriods

// Mixer combines the voice and music streams for one output period.
//
// Single-consumer by contract: only the ALSA write goroutine touches it, so
// nothing here is synchronised. The gain TARGET is set from elsewhere and is
// the one field that needs atomicity — it lives in PcmSpeaker, not here.
type Mixer struct {
	gain int32 // current, Q15, ramps toward the target
}

// DuckGain converts decibels of attenuation to Q15.
//
// 0 dB is returned as exactly unity rather than a rounded conversion, so
// "not ducked" is bit-identical to the input.
func DuckGain(db float64) int32 {
	if db >= 0 {
		return unityGain
	}
	g := math.Pow(10, db/20.0) * float64(unityGain)
	if g < 0 {
		return 0
	}
	return int32(g + 0.5)
}

// Gain reports the current (ramped) gain, for tests and logging.
func (m *Mixer) Gain() int32 { return m.gain }

// SetGainImmediate jumps the ramp to a value. Used at stream start, where
// there is no audio to click.
func (m *Mixer) SetGainImmediate(g int32) { m.gain = g }

// Mix produces one output period from whichever streams have audio.
//
// Both buffers are owned by the caller once dequeued, so mixing happens IN
// PLACE and the function allocates nothing — this runs ~23 times a second
// for as long as anything is playing.
//
// Returns the buffer to write, or nil when there is nothing to play (the
// caller pumps silence, which is what paces this loop).
func (m *Mixer) Mix(voice, music []byte, target int32) []byte {
	switch {
	case voice == nil && music == nil:
		// Still settle the ramp: a duck requested while nothing is playing
		// must not be waiting, half-applied, for the next period of audio.
		m.stepGain(target)
		return nil

	case music == nil:
		// Voice alone is never ducked — it is the thing being listened to.
		m.stepGain(target)
		return voice

	case voice == nil:
		m.applyGain(music, target)
		return music

	default:
		m.applyGain(music, target)
		mixInto(voice, music)
		return voice
	}
}

// stepGain moves the current gain one period toward the target, by at most
// rampStep. Clamped rather than divided, so it always arrives exactly.
func (m *Mixer) stepGain(target int32) {
	switch {
	case m.gain < target:
		if m.gain += rampStep; m.gain > target {
			m.gain = target
		}
	case m.gain > target:
		if m.gain -= rampStep; m.gain < target {
			m.gain = target
		}
	}
}

// applyGain scales a period, ramping across it sample by sample.
//
// The ramp is per SAMPLE, not per period: a gain change applied at a period
// boundary is a step discontinuity, which is a click. Interpolating across
// the period turns the same change into a fade.
func (m *Mixer) applyGain(buf []byte, target int32) {
	start := m.gain
	m.stepGain(target)
	end := m.gain

	frames := len(buf) / 4 // stereo S16
	if frames == 0 {
		return
	}
	for i := 0; i < frames; i++ {
		g := start
		if end != start {
			g = start + (end-start)*int32(i)/int32(frames)
		}
		// L and R are duplicates on this hardware, but scale both rather
		// than assuming it — the assumption is one refactor away from being
		// silently wrong, and the cost is one multiply.
		for c := 0; c < 2; c++ {
			off := i*4 + c*2
			s := int32(int16(uint16(buf[off]) | uint16(buf[off+1])<<8))
			s = (s * g) >> 15
			buf[off] = byte(uint16(s) & 0xff)
			buf[off+1] = byte(uint16(s) >> 8)
		}
	}
}

// mixInto sums music into voice with saturation.
//
// Saturating rather than wrapping: an int16 overflow wraps a loud peak to
// full-scale opposite polarity, which is a far worse noise than the clipping
// it replaces. With music ducked ~18dB under a response that peaks well
// below full scale, the sum should not reach this often — but "should not"
// is not a reason to leave the wrap in.
func mixInto(dst, src []byte) {
	n := len(dst)
	if len(src) < n {
		n = len(src)
	}
	for i := 0; i+1 < n; i += 2 {
		a := int32(int16(uint16(dst[i]) | uint16(dst[i+1])<<8))
		b := int32(int16(uint16(src[i]) | uint16(src[i+1])<<8))
		s := a + b
		if s > math.MaxInt16 {
			s = math.MaxInt16
		} else if s < math.MinInt16 {
			s = math.MinInt16
		}
		dst[i] = byte(uint16(s) & 0xff)
		dst[i+1] = byte(uint16(s) >> 8)
	}
}

// toStereo converts a mono S16 wire period into the stereo frames the ALSA
// device requires. The stereo config is an I2S/codec-path constraint, not a
// wire one — shipping two identical channels would double bandwidth for
// nothing on links that are already marginal. Untagged: pure byte
// arithmetic, shared by every board's binding.
func toStereo(data []byte) []byte {
	n := len(data) / 2
	period := make([]byte, n*4)
	for i := 0; i < n; i++ {
		lo, hi := data[i*2], data[i*2+1]
		period[i*4+0], period[i*4+1] = lo, hi // L
		period[i*4+2], period[i*4+3] = lo, hi // R
	}
	return period
}

// periodRMS computes the RMS level of a stereo S16LE period, normalized to
// 0..1 of int16 full-scale. Left channel only, every 4th frame — the wire
// is mono duplicated L=R and the LED meter needs ~2 significant digits at
// ~23Hz, so 512 of 2048 frames is plenty at a quarter of the cost. Runs on
// the ALSA pump goroutine: no allocation, integer accumulate.
func periodRMS(period []byte) float64 {
	if len(period) < 4 {
		return 0
	}
	var sum uint64
	n := 0
	// Stereo frame = 4 bytes (L16+R16); step 4 frames = 16 bytes.
	for i := 0; i+1 < len(period); i += 16 {
		s := int64(int16(uint16(period[i]) | uint16(period[i+1])<<8))
		sum += uint64(s * s)
		n++
	}
	if n == 0 {
		return 0
	}
	return math.Sqrt(float64(sum)/float64(n)) / 32768.0
}
