// Package beamformer implements directional mic selection for the
// Echo Dot Gen 2 (biscuit) 7-microphone array.
//
// # Mic geometry (confirmed empirically, 2026-05)
//
// 6 perimeter mics at r=36mm, 60° intervals, 30° offset from 12 o'clock.
// 1 centre mic. Ch7 and Ch8 are unconnected.
//
//	Ch0 → MK1 → 330°  (11 o'clock)  confirmed empirically 2026-05
//	Ch1 → MK2 →  30°  ( 1 o'clock)
//	Ch2 → MK3 →  90°  ( 3 o'clock)
//	Ch3 → MK4 → 150°  ( 5 o'clock)
//	Ch4 → MK5 → 210°  ( 7 o'clock)
//	Ch5 → MK6 → 270°  ( 9 o'clock)
//	Ch6 → MK7 → centre (omnidirectional)
//
// # Algorithm
//
// Two modes:
//
//  1. Disabled (beamforming off): output ch6 (centre/omni). Used for wake
//     word detection — equidistant from all directions, no directional bias.
//
//  2. Enabled (voice turn): select the perimeter mic with the highest
//     energy onset relative to its own noise floor baseline. Lock that mic
//     for the duration of the turn. Unlock on gate close.
//
// # Direction estimation
//
// Two parallel smoothers run continuously:
//
//   - energySmooth (α=0.9, ~320ms): fast, tracks speech onset
//   - energyBaseline (α=0.995, ~10s): slow, tracks steady background noise
//
// At Lock() time, the direction with the highest ratio of energySmooth to
// energyBaseline is selected. This picks the direction that just had a
// sudden energy increase (speech onset) rather than the direction with the
// highest absolute energy (TV, fan, etc.).
//
// Direction is also exposed for LED ring visualisation.
package beamformer

import (
	"log"
	"math"
)

const (
	// ALSA stream parameters — must match pcm_microphone.go
	nChannels    = 9
	sampleRate   = 16000
	byteSample   = 3 // S24_3LE
	frameSize    = nChannels * byteSample // 27 bytes per frame
	periodFrames = 512

	// Number of candidate steering directions — one per perimeter mic
	nDirections = 6

	// Centre mic channel — used for wake word detection (omnidirectional)
	centreCh = 6

	// Smoothing constants
	smoothAlpha   = 0.9    // fast smoother (~320ms time constant at 32ms/period)
	baselineAlpha = 0.995  // slow smoother (~10s time constant) — tracks background noise
)

// micAngles defines the physical angle (degrees, clockwise from 12 o'clock)
// for each ALSA channel. Index = channel number.
// Confirmed empirically 2026-05 via tone injection + analyse_capture.py.
var micAngles = [7]float64{
	330, // ch0 — MK1
	30,  // ch1 — MK2
	90,  // ch2 — MK3
	150, // ch3 — MK4
	210, // ch4 — MK5
	270, // ch5 — MK6
	0,   // ch6 — MK7 centre
}

// candidateAngles are the steering directions tested for direction estimation.
// One per perimeter mic, matching the mic positions exactly.
var candidateAngles = [nDirections]float64{330, 30, 90, 150, 210, 270}

// directionToChannel maps candidateAngles index → ALSA channel number for
// the perimeter mic at that direction.
//
//	candidateAngles[0]=330° → ch0 (MK1)
//	candidateAngles[1]=30°  → ch1 (MK2)
//	candidateAngles[2]=90°  → ch2 (MK3)
//	candidateAngles[3]=150° → ch3 (MK4)
//	candidateAngles[4]=210° → ch4 (MK5)
//	candidateAngles[5]=270° → ch5 (MK6)
var directionToChannel = [nDirections]int{0, 1, 2, 3, 4, 5}

// Beamformer holds direction estimation state and locked mic selection.
type Beamformer struct {
	// energySmooth: fast EWMA of per-direction HF energy (~320ms time constant).
	// Tracks speech onset.
	energySmooth [nDirections]float64

	// energyBaseline: slow EWMA of per-direction HF energy (~10s time constant).
	// Tracks steady background noise (TV, fan, etc.).
	energyBaseline [nDirections]float64

	// baselineReady counts periods until baseline is initialised (~3s warmup).
	baselineReady int

	// lockedChannel is the ALSA channel selected at gate open.
	// -1 means unlocked (use live best-direction selection).
	lockedChannel int
}

// New creates a Beamformer.
func New() *Beamformer {
	return &Beamformer{lockedChannel: -1}
}

// Lock selects the mic with the highest energy onset relative to its noise
// floor baseline and holds it until Unlock.
//
// Using onset ratio rather than absolute energy means a voice in a quiet
// direction beats a TV in a loud direction.
func (b *Beamformer) Lock() {
	if b.lockedChannel >= 0 {
		return
	}

	best := 0
	bestRatio := b.onsetRatio(0)
	for di := 1; di < nDirections; di++ {
		r := b.onsetRatio(di)
		if r > bestRatio {
			bestRatio = r
			best = di
		}
	}
	b.lockedChannel = directionToChannel[best]
	log.Printf("[beam] locked to ch%d (%.0f°) onset_ratio=%.2f",
		b.lockedChannel, candidateAngles[best], bestRatio)
}

// onsetRatio returns energySmooth[di] / energyBaseline[di].
// High ratio = sudden energy increase = likely speech onset.
func (b *Beamformer) onsetRatio(di int) float64 {
	baseline := b.energyBaseline[di]
	if baseline < 1e-10 {
		return b.energySmooth[di]
	}
	return b.energySmooth[di] / baseline
}

// Unlock releases the locked mic selection.
// Call when the VAD gate closes (voice turn ends).
func (b *Beamformer) Unlock() {
	if b.lockedChannel >= 0 {
		log.Printf("[beam] unlocked from ch%d", b.lockedChannel)
	}
	b.lockedChannel = -1
}

// Process returns mono S16_LE audio and the estimated source angle.
//
// When disabled: returns ch6 (centre/omni) — for wake word detection.
// When enabled and locked: returns the locked perimeter mic.
// When enabled and unlocked: returns the current best-direction perimeter mic.
//
// angle is the estimated dominant source direction (0–360°, clockwise from
// 12 o'clock), or -1 if estimation is unreliable.
func (b *Beamformer) Process(raw []byte, steerAngle float64, enabled bool) (mono []byte, angle float64) {
	if len(raw) < periodFrames*frameSize {
		return extractChannel(raw, 0), -1
	}

	if !enabled {
		// OWW mode: centre mic, omnidirectional, no direction bias
		return extractChannel(raw, centreCh), -1
	}

	// Decode 6 perimeter channels for HF direction estimation
	channels := decodeChannels(raw)
	hfChannels := bandDiff(channels)

	// Update both smoothers
	for di := range candidateAngles {
		energy := hfEnergy(hfChannels, di)
		b.energySmooth[di] = smoothAlpha*b.energySmooth[di] + (1-smoothAlpha)*energy

		// Only update baseline when unlocked — freeze it during voice turns
		// so a loud voice doesn't corrupt the noise floor estimate
		if b.lockedChannel < 0 {
			b.energyBaseline[di] = baselineAlpha*b.energyBaseline[di] + (1-baselineAlpha)*energy
		}
	}

	if b.baselineReady < 100 { // ~3s warmup
		b.baselineReady++
	}

	// Best direction from fast smoother (for LED ring and unlocked mic selection)
	bestDir := 0
	for di := 1; di < nDirections; di++ {
		if b.energySmooth[di] > b.energySmooth[bestDir] {
			bestDir = di
		}
	}
	// Select output channel
	ch := b.lockedChannel
	if ch < 0 {
		ch = directionToChannel[bestDir]
	}

	// Only report direction when locked — direction arc shows during voice
	// turns only, not during idle wake word listening.
	if b.lockedChannel >= 0 {
		angle = candidateAngles[bestDir]
	} else {
		angle = -1
	}

	return extractChannel(raw, ch), angle
}

// hfEnergy returns the mean squared HF energy for direction di.
func hfEnergy(hfChannels [6][]float32, di int) float64 {
	n := len(hfChannels[0])
	ch := directionToChannel[di]
	var energy float64
	for _, v := range hfChannels[ch] {
		energy += float64(v) * float64(v)
	}
	return energy / float64(n)
}

// bandDiff returns the stride-2 difference of each channel: out[i] = (in[i] - in[i-2]) / 2.
// Frequency response peaks at 4kHz (fs/4), zero at 0Hz and 8kHz.
func bandDiff(channels [6][]float32) [6][]float32 {
	var out [6][]float32
	for ci := 0; ci < 6; ci++ {
		out[ci] = make([]float32, len(channels[ci]))
		for i := 2; i < len(channels[ci]); i++ {
			out[ci][i] = (channels[ci][i] - channels[ci][i-2]) * 0.5
		}
	}
	return out
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

// decodeS24Sample decodes 3 bytes of S24_3LE to float32 in [-1, 1].
func decodeS24Sample(b0, b1, b2 byte) float32 {
	val := int32(b0) | int32(b1)<<8 | int32(b2)<<16
	if val&0x800000 != 0 {
		val |= ^int32(0xFFFFFF)
	}
	return float32(val) / 8388608.0
}

// extractChannel extracts a single channel as S16_LE mono.
// Takes the upper 2 bytes of each 3-byte S24_3LE sample (drops LSB).
func extractChannel(raw []byte, ch int) []byte {
	n := len(raw) / frameSize
	out := make([]byte, n*2)
	offset0 := ch * byteSample
	for i := 0; i < n; i++ {
		base := i*frameSize + offset0
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
