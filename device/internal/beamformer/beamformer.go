// Package beamformer implements delay-and-sum beamforming for the
// Echo Dot Gen 2 (biscuit) 7-microphone array.
//
// # Mic geometry (confirmed empirically, 2026-05)
//
// 6 perimeter mics at r=38.2mm, 60° intervals, 30° offset from 12 o'clock.
// 1 centre mic. Ch7 and Ch8 are unconnected.
//
//	Ch0 → MK2 →  30°  (1 o'clock)
//	Ch1 → MK1 → 330°  (11 o'clock)
//	Ch2 → MK3 →  90°  (3 o'clock)
//	Ch3 → MK4 → 150°  (5 o'clock)
//	Ch4 → MK5 → 210°  (7 o'clock)
//	Ch5 → MK6 → 270°  (9 o'clock)
//	Ch6 → MK7 → centre (omnidirectional)
//
// # Algorithm
//
// Delay-and-sum: for a target angle θ, each perimeter mic receives the
// wavefront at a different time. We compensate by applying a fractional
// sample delay per channel, then sum all 7 channels. Sound from θ adds
// constructively; sound from other directions partially cancels.
//
// Delays are pre-computed at startup — per-period cost is a linear scan
// with interpolation, well within the 32ms period budget.
//
// # Direction estimation
//
// We run delay-and-sum for each of the 6 candidate angles (mic positions)
// and pick the one with highest output energy. This gives a coarse but
// reliable estimate of the dominant sound source direction, updated every
// period (32ms).
package beamformer

import (
	"math"
)

const (
	// ALSA stream parameters — must match pcm_microphone.go
	nChannels   = 9
	sampleRate  = 16000
	byteSample  = 3    // S24_3LE
	frameSize   = nChannels * byteSample // 27 bytes per frame
	periodFrames = 512

	// Physical array parameters
	micRadius   = 0.0382 // metres — confirmed from PCB photo
	speedSound  = 343.0  // m/s at ~20°C

	// Number of candidate steering directions — one per perimeter mic
	nDirections = 6

	// Centre mic channel
	centreCh = 6
)

// micAngles defines the physical angle (degrees, clockwise from 12 o'clock)
// for each ALSA channel. Index = channel number.
var micAngles = [7]float64{
	30,  // ch0 — MK2
	330, // ch1 — MK1
	90,  // ch2 — MK3
	150, // ch3 — MK4
	210, // ch4 — MK5
	270, // ch5 — MK6
	0,   // ch6 — MK7 centre (angle unused for delay calc)
}

// candidateAngles are the steering directions we test for direction estimation.
// One per perimeter mic — coarse but fast.
var candidateAngles = [nDirections]float64{30, 90, 150, 210, 270, 330}

// Beamformer holds pre-computed delay tables and processes mic periods.
type Beamformer struct {
	// delays[dirIdx][chIdx] = fractional sample delay for channel chIdx
	// when steering toward candidateAngles[dirIdx].
	// Positive = channel hears source earlier than array centre → delay it.
	delays [nDirections][6]float64

	// intDelays and fracDelays split the above for fast interpolation
	intDelays  [nDirections][6]int
	fracDelays [nDirections][6]float64
}

// New creates a Beamformer with pre-computed delay tables.
func New() *Beamformer {
	b := &Beamformer{}
	b.precompute()
	return b
}

// precompute fills the delay tables for all candidate steering directions.
// Called once at startup.
func (b *Beamformer) precompute() {
	for di, steerDeg := range candidateAngles {
		steerRad := steerDeg * math.Pi / 180.0
		// Unit vector pointing toward the source
		sx := math.Sin(steerRad)
		sy := math.Cos(steerRad)

		for ci := 0; ci < 6; ci++ {
			micRad := micAngles[ci] * math.Pi / 180.0
			// Mic position in metres
			mx := micRadius * math.Sin(micRad)
			my := micRadius * math.Cos(micRad)
			// Projection of mic position onto steering vector.
			// Positive = mic is closer to source than array centre.
			proj := mx*sx + my*sy
			// Delay in samples: positive = delay this channel (it heard
			// source earlier, we push it back to align with later mics)
			delaySamples := proj * sampleRate / speedSound
			b.delays[di][ci] = delaySamples
			b.intDelays[di][ci] = int(math.Floor(delaySamples))
			b.fracDelays[di][ci] = delaySamples - math.Floor(delaySamples)
		}
	}
}

// Process takes one raw period of 9-channel S24_3LE interleaved PCM,
// applies delay-and-sum beamforming, and returns:
//
//   - mono: beamformed mono audio as S16_LE (512 frames × 2 bytes = 1024 bytes)
//   - angle: estimated dominant source direction in degrees (0–360, clockwise
//     from 12 o'clock), or -1 if estimation is unreliable
//
// The steerAngle parameter fixes the output beam direction. Pass -1 to use
// the auto-detected dominant direction (same as angle return value).
//
// Output format matches vadExtractMono — drop-in replacement.
func (b *Beamformer) Process(raw []byte, steerAngle float64, enabled bool) (mono []byte, angle float64) {
	if !enabled {
		return extractChannel(raw, 0), -1
	}

	if len(raw) < periodFrames*frameSize {
		// Undersized period — fall back to ch0
		return extractChannel(raw, 0), -1
	}

	// Decode all 6 perimeter channels into float32 arrays.
	// Centre mic (ch6) is summed in separately at the end.
	channels := decodeChannels(raw)

	// Run delay-and-sum for each candidate direction, track energy.
	var bestEnergy float64
	bestDir := 0

	energies := [nDirections]float64{}
	for di := range candidateAngles {
		energies[di] = b.beamEnergy(channels, di)
		if energies[di] > bestEnergy {
			bestEnergy = energies[di]
			bestDir = di
		}
	}
	angle = candidateAngles[bestDir]

	// Choose which direction to steer the output beam.
	outputDir := bestDir
	if steerAngle >= 0 {
		outputDir = nearestDirection(steerAngle)
	}

	// Generate beamformed output for the chosen direction.
	beamed := b.beamOutput(channels, outputDir)

	// Mix in centre mic at reduced weight — omnidirectional, boosts level
	// without hurting directionality.
	centreSamples := decodeOneCh(raw, centreCh)
	const centreWeight = 0.3
	for i, v := range centreSamples {
		beamed[i] += float64(v) * centreWeight
	}

	// No normalisation — toS16LE clips to [-1,1] which is the correct
	// behaviour for delay-and-sum. Averaging would reduce SNR benefit.

	mono = toS16LE(beamed)
	return mono, angle
}

// beamEnergy runs delay-and-sum for direction di and returns output energy.
// Used for direction estimation — doesn't produce output samples.
func (b *Beamformer) beamEnergy(channels [6][]float32, di int) float64 {
	n := len(channels[0])
	var energy float64
	for i := 0; i < n; i++ {
		var sum float64
		for ci := 0; ci < 6; ci++ {
			sum += float64(b.interpolate(channels[ci], i, b.intDelays[di][ci], b.fracDelays[di][ci]))
		}
		energy += sum * sum
	}
	return energy
}

// beamOutput runs delay-and-sum for direction di and returns float32 samples.
func (b *Beamformer) beamOutput(channels [6][]float32, di int) []float64 {
	n := len(channels[0])
	out := make([]float64, n)
	for i := 0; i < n; i++ {
		var sum float64
		for ci := 0; ci < 6; ci++ {
			sum += float64(b.interpolate(channels[ci], i, b.intDelays[di][ci], b.fracDelays[di][ci]))
		}
		out[i] = sum
	}
	return out
}

// interpolate applies a fractional sample delay to channel data at frame i.
// Uses linear interpolation between adjacent samples.
// intD and fracD are the integer and fractional parts of the delay.
func (b *Beamformer) interpolate(ch []float32, i, intD int, fracD float64) float32 {
	j := i - intD
	if j < 0 || j >= len(ch) {
		return 0
	}
	if fracD == 0 || j+1 >= len(ch) {
		return ch[j]
	}
	// Linear interpolation between ch[j] and ch[j+1]
	return ch[j] + float32(fracD)*(ch[j+1]-ch[j])
}

// decodeChannels decodes all 6 perimeter channels from a raw S24_3LE period
// into float32 arrays normalised to [-1, 1].
func decodeChannels(raw []byte) [6][]float32 {
	var out [6][]float32
	for ci := 0; ci < 6; ci++ {
		out[ci] = make([]float32, periodFrames)
	}
	for i := 0; i < periodFrames; i++ {
		base := i * frameSize
		for ci := 0; ci < 6; ci++ {
			offset := base + ci*byteSample
			out[ci][i] = decodeS24Sample(raw[offset], raw[offset+1], raw[offset+2])
		}
	}
	return out
}

// decodeOneCh decodes a single channel from a raw S24_3LE period.
func decodeOneCh(raw []byte, ch int) []float32 {
	out := make([]float32, periodFrames)
	offset0 := ch * byteSample
	for i := 0; i < periodFrames; i++ {
		offset := i*frameSize + offset0
		out[i] = decodeS24Sample(raw[offset], raw[offset+1], raw[offset+2])
	}
	return out
}

// decodeS24Sample decodes 3 bytes of S24_3LE to float32 in [-1, 1].
func decodeS24Sample(b0, b1, b2 byte) float32 {
	val := int32(b0) | int32(b1)<<8 | int32(b2)<<16
	if val&0x800000 != 0 {
		val |= ^int32(0xFFFFFF) // sign extend to 32-bit
	}
	return float32(val) / 8388608.0
}

// toS16LE converts float64 samples to S16_LE bytes with soft clipping.
func toS16LE(samples []float64) []byte {
	out := make([]byte, len(samples)*2)
	for i, s := range samples {
		// Soft clip
		if s > 1.0 {
			s = 1.0
		} else if s < -1.0 {
			s = -1.0
		}
		v := int16(s * 32767.0)
		out[i*2] = byte(v)
		out[i*2+1] = byte(v >> 8)
	}
	return out
}

// extractChannel extracts a single channel as S16_LE — used as fallback.
// Matches the existing vadExtractMono behaviour.
func extractChannel(raw []byte, ch int) []byte {
	n := len(raw) / frameSize
	out := make([]byte, n*2)
	offset0 := ch * byteSample
	for i := 0; i < n; i++ {
		base := i*frameSize + offset0
		// Take upper 2 bytes of 3-byte S24_3LE sample (drop LSB)
		out[i*2] = raw[base+1]
		out[i*2+1] = raw[base+2]
	}
	return out
}

// nearestDirection returns the index into candidateAngles closest to angleDeg.
func nearestDirection(angleDeg float64) int {
	best := 0
	bestDiff := math.Abs(angleDiff(angleDeg, candidateAngles[0]))
	for i := 1; i < nDirections; i++ {
		d := math.Abs(angleDiff(angleDeg, candidateAngles[i]))
		if d < bestDiff {
			bestDiff = d
			best = i
		}
	}
	return best
}

// angleDiff returns the signed angular difference a-b, wrapped to [-180, 180].
func angleDiff(a, b float64) float64 {
	d := math.Mod(a-b+360, 360)
	if d > 180 {
		d -= 360
	}
	return d
}

// CandidateAngles returns the steering angles used for direction estimation.
// Exposed for LED mapping in cmd/server.go.
func CandidateAngles() [nDirections]float64 {
	return candidateAngles
}
