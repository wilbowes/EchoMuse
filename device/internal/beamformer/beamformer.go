// Package beamformer implements directional mic selection for the
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
// Two modes:
//
//  1. Disabled (beamforming off): output ch6 (centre/omni). Used for wake
//     word detection — equidistant from all directions, no directional bias.
//
//  2. Enabled (voice turn): select the perimeter mic with the highest
//     sustained high-frequency energy (2–4kHz band, where the 76mm array
//     has genuine angular resolution). Lock that mic for the duration of
//     the turn. Unlock on gate close. No delays, no phase math, no
//     inter-period discontinuities.
//
// # Direction estimation
//
// bandDiff (stride-2 difference, peak at 4kHz) emphasises the frequency
// band where the array discriminates direction without grating lobes.
// Smoothed with α=0.9 (320ms time constant) to prevent jitter.
// Direction drives LED ring independently of mic selection.
//
// # Why not delay-and-sum?
//
// Delay-and-sum with incorrect direction estimates causes frequency-dependent
// constructive/destructive interference that varies period-to-period,
// producing choppy audio worse than single channel. Directional mic selection
// avoids all phase math and produces clean, consistent output from the best
// physically-positioned mic.
package beamformer

import (
	"log"
	"math"
)

const (
	// ALSA stream parameters — must match pcm_microphone.go
	nChannels   = 9
	sampleRate  = 16000
	byteSample  = 3 // S24_3LE
	frameSize   = nChannels * byteSample // 27 bytes per frame
	periodFrames = 512

	// Number of candidate steering directions — one per perimeter mic
	nDirections = 6

	// Centre mic channel — used for wake word detection (omnidirectional)
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
	0,   // ch6 — MK7 centre
}

// candidateAngles are the steering directions tested for direction estimation.
// One per perimeter mic, matching the mic positions exactly.
var candidateAngles = [nDirections]float64{30, 90, 150, 210, 270, 330}

// directionToChannel maps candidateAngles index → ALSA channel number for
// the perimeter mic at that direction.
//
//	candidateAngles[0]=30°  → ch0 (micAngles[0]=30°)
//	candidateAngles[1]=90°  → ch2 (micAngles[2]=90°)
//	candidateAngles[2]=150° → ch3 (micAngles[3]=150°)
//	candidateAngles[3]=210° → ch4 (micAngles[4]=210°)
//	candidateAngles[4]=270° → ch5 (micAngles[5]=270°)
//	candidateAngles[5]=330° → ch1 (micAngles[1]=330°)
var directionToChannel = [nDirections]int{0, 2, 3, 4, 5, 1}

// Beamformer holds direction estimation state and locked mic selection.
type Beamformer struct {
	// energySmooth holds an exponentially weighted moving average of the
	// per-direction HF band energy. α=0.9 gives ~320ms time constant.
	energySmooth [nDirections]float64

	// lockedChannel is the ALSA channel selected at gate open.
	// -1 means unlocked (use live best-direction selection).
	lockedChannel int
}

// New creates a Beamformer.
func New() *Beamformer {
	return &Beamformer{lockedChannel: -1}
}

// Lock selects the current best-direction mic and holds it until Unlock.
// Call when the VAD gate opens (voice turn starts).
// No-op if already locked — prevents mid-utterance mic changes from VAD
// oscillation causing the gate to briefly close and reopen.
func (b *Beamformer) Lock() {
	if b.lockedChannel >= 0 {
		return
	}
	best := 0
	for di := 1; di < nDirections; di++ {
		if b.energySmooth[di] > b.energySmooth[best] {
			best = di
		}
	}
	b.lockedChannel = directionToChannel[best]
	log.Printf("[beam] locked to ch%d (%.0f°)", b.lockedChannel, candidateAngles[best])
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

	// Update smoothed HF energy per direction
	const smoothAlpha = 0.9
	for di := range candidateAngles {
		energy := hfEnergy(hfChannels, di)
		b.energySmooth[di] = smoothAlpha*b.energySmooth[di] + (1-smoothAlpha)*energy
	}

	// Best direction from smoothed energies
	bestDir := 0
	for di := 1; di < nDirections; di++ {
		if b.energySmooth[di] > b.energySmooth[bestDir] {
			bestDir = di
		}
	}
	angle = candidateAngles[bestDir]

	// Select output channel
	ch := b.lockedChannel
	if ch < 0 {
		ch = directionToChannel[bestDir]
	}

	return extractChannel(raw, ch), angle
}

// hfEnergy computes delay-and-sum energy for direction di using HF channels.
// Used only for direction estimation, not for audio output.
func hfEnergy(hfChannels [6][]float32, di int) float64 {
	n := len(hfChannels[0])

	// For direction estimation we use simple channel energy comparison
	// rather than delay-and-sum — the mic at the candidate angle should
	// have the highest HF energy when that direction is the source.
	// This avoids the delay interpolation that caused choppy output.
	ch := directionToChannel[di]
	var energy float64
	for _, v := range hfChannels[ch] {
		energy += float64(v) * float64(v)
	}
	return energy / float64(n)
}

// bandDiff returns the stride-2 difference of each channel: out[i] = (in[i] - in[i-2]) / 2.
// Frequency response peaks at 4kHz (fs/4), zero at 0Hz and 8kHz.
// Targets the 2–4kHz band where the 76mm array has angular resolution
// without grating lobes.
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
