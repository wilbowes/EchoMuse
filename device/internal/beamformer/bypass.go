package beamformer

import "math"

// Processor is the behaviour the audio path needs from a beamformer, so a
// device without a steerable array can substitute a passthrough.
type Processor interface {
	Lock(enabled bool)
	Unlock()
	Process(raw []byte, steerAngle float64, gain float64) (mono []byte, angle float64)
	ClippedSamples() uint64
}

var (
	_ Processor = (*Beamformer)(nil)
	_ Processor = (*Bypass)(nil)
)

// Bypass extracts a single channel instead of steering a beam.
//
// The Beamformer above is specific to biscuit's 7-mic array: its geometry
// table, channel map and frameSize all assume 9 interleaved channels. The Echo
// Show 5 has two mics fed by one stereo ADC, which is not an array worth
// steering: there is no useful directional gain to extract from a single
// closely-spaced pair, and the measured inter-mic correlation of 0.92 confirms
// the two channels see substantially the same field.
//
// Gain handling is bit-identical to Beamformer.extractChannel so downstream
// levels, clipping behaviour and telemetry are unchanged.
type Bypass struct {
	channels int
	ch       int
	mix      []int

	clippedSamples uint64
}

// NewBypass returns a passthrough that emits channel ch of an interleaved
// S24_3LE stream with the given channel count.
func NewBypass(channels, ch int) *Bypass {
	return &Bypass{channels: channels, ch: ch}
}

// NewBypassMix averages several channels instead of picking one. Slightly
// better SNR than a single mic when the sources are coherent, at the cost of
// comb filtering for off-axis sources.
func NewBypassMix(channels int, chans []int) *Bypass {
	return &Bypass{channels: channels, ch: chans[0], mix: chans}
}

// Lock is a no-op: there is no beam to steer.
func (b *Bypass) Lock(bool) {}

// Unlock is a no-op.
func (b *Bypass) Unlock() {}

// ClippedSamples reports samples clamped during gain application.
func (b *Bypass) ClippedSamples() uint64 { return b.clippedSamples }

// Process returns the configured channel as S16_LE mono. The angle is always
// -1, meaning "no direction estimate", which the LED and control paths already
// handle for the unlocked case.
func (b *Bypass) Process(raw []byte, _ float64, gain float64) ([]byte, float64) {
	frameBytes := b.channels * 3
	frames := len(raw) / frameBytes
	out := make([]byte, frames*2)

	// Q12 fixed point: the >>20 combines the Q12 descale with the 24->16 bit
	// reduction (>>8), so gain 1.0 reproduces the upper-2-bytes behaviour
	// bit-exactly.
	gainQ := int64(gain*4096.0 + 0.5)

	for i := 0; i < frames; i++ {
		var val int64
		if len(b.mix) > 1 {
			var sum int64
			for _, c := range b.mix {
				sum += int64(sample24(raw, i*frameBytes+c*3))
			}
			val = sum / int64(len(b.mix))
		} else {
			val = int64(sample24(raw, i*frameBytes+b.ch*3))
		}

		v := (val * gainQ) >> 20
		if v > math.MaxInt16 {
			v = math.MaxInt16
			b.clippedSamples++
		} else if v < math.MinInt16 {
			v = math.MinInt16
			b.clippedSamples++
		}
		out[i*2] = byte(uint16(v))
		out[i*2+1] = byte(uint16(v) >> 8)
	}
	return out, -1
}

// sample24 sign-extends one little-endian 24-bit sample at offset o.
func sample24(raw []byte, o int) int32 {
	v := int32(raw[o]) | int32(raw[o+1])<<8 | int32(raw[o+2])<<16
	if v&0x800000 != 0 {
		v -= 1 << 24
	}
	return v
}
