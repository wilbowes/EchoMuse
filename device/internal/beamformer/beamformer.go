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
// Direction estimation always runs — smoothers update on every Process() call
// regardless of BeamformingEnabled. This keeps the baseline warm so Lock()
// gets a meaningful onset ratio the instant beamforming is turned on.
//
// Output channel is determined by lock state, not by the config flag:
//   - Unlocked: always ch6 (centre/omni). Covers OWW listening and any turn
//     where Lock() was a no-op (beamforming disabled).
//   - Locked: the perimeter mic selected at Lock() time, or the mic nearest
//     to BeamAngle if a fixed steering direction is configured.
//
// BeamformingEnabled only gates Lock() — if false, Lock() is a no-op and
// the device stays on ch6 for both OWW and voice turns.
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
// enabled is the BeamformingEnabled config flag. If false, Lock() is a no-op
// and the beamformer continues outputting ch6 (centre/omni). This means the
// config flag only gates directional selection — not whether smoothers run.
// Smoothers always run so the baseline is warm if beamforming is later enabled.
//
// Using onset ratio rather than absolute energy means a voice in a quiet
// direction beats a TV in a loud direction. Falls back to raw smooth energy
// if the baseline hasn't warmed up yet (~3s after start), since onset ratios
// are meaningless when energyBaseline is near zero.
func (b *Beamformer) Lock(enabled bool) {
	if !enabled {
		// Beamforming disabled — stay on ch6 (lockedChannel remains -1).
		// Smoothers are still running, so if beamforming is turned on later
		// the baseline will already be warmed up.
		log.Printf("[beam] Lock() called but beamforming disabled — staying on ch6 (omni)")
		return
	}
	if b.lockedChannel >= 0 {
		return
	}

	best := 0
	var bestScore float64
	if b.baselineReady >= 100 {
		// Baseline warmed up — use onset ratio (energy spike vs noise floor)
		bestScore = b.onsetRatio(0)
		for di := 1; di < nDirections; di++ {
			r := b.onsetRatio(di)
			if r > bestScore {
				bestScore = r
				best = di
			}
		}
		log.Printf("[beam] locked to ch%d (%.0f°) onset_ratio=%.2f",
			directionToChannel[best], candidateAngles[best], bestScore)
	} else {
		// Baseline not ready — use raw smooth energy to avoid picking a
		// direction based on a near-zero baseline inflating the ratio
		bestScore = b.energySmooth[0]
		for di := 1; di < nDirections; di++ {
			if b.energySmooth[di] > bestScore {
				bestScore = b.energySmooth[di]
				best = di
			}
		}
		log.Printf("[beam] locked to ch%d (%.0f°) energy=%.4f (baseline not ready)",
			directionToChannel[best], candidateAngles[best], bestScore)
	}

	b.lockedChannel = directionToChannel[best]
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
// The enabled flag (BeamformingEnabled) no longer gates smoother updates —
// direction estimation always runs so the baseline stays warm regardless of
// config state. The flag only affects Lock() behaviour (see Lock() docs).
//
// Output channel is determined by lock state alone:
//   - Unlocked (lockedChannel == -1): always ch6 (centre/omni). This covers
//     OWW listening and any voice turn where Lock() was a no-op (beamforming
//     disabled). ch6 is equidistant from all directions — no directional bias.
//   - Locked, steerAngle >= 0 (fixed-beam): mic nearest to steerAngle. Config-
//     driven direction, ignores the energy-based lock channel.
//   - Locked, steerAngle < 0 (auto): the perimeter mic selected at Lock() time.
//
// angle is the estimated dominant source direction (0–360°, clockwise from
// 12 o'clock), or -1 when unlocked.
func (b *Beamformer) Process(raw []byte, steerAngle float64) (mono []byte, angle float64) {
	if len(raw) < periodFrames*frameSize {
		return extractChannel(raw, centreCh), -1
	}

	// Always decode and update smoothers — direction estimation runs
	// continuously regardless of BeamformingEnabled. This keeps the baseline
	// warm so Lock() gets a good onset ratio the moment beamforming is enabled.
	channels := decodeChannels(raw)
	hfChannels := bandDiff(channels)

	for di := range candidateAngles {
		energy := hfEnergy(hfChannels, di)
		b.energySmooth[di] = smoothAlpha*b.energySmooth[di] + (1-smoothAlpha)*energy

		// Freeze baseline during voice turns (locked) so the speaker's own
		// voice doesn't corrupt the noise floor estimate.
		if b.lockedChannel < 0 {
			b.energyBaseline[di] = baselineAlpha*b.energyBaseline[di] + (1-baselineAlpha)*energy
		}
	}

	if b.baselineReady < 100 { // ~3s warmup at 32ms/period
		b.baselineReady++
	}

	// Unlocked: always ch6 (omni). Covers OWW listening and disabled-beamforming
	// voice turns. No directional bias, no channel splices.
	if b.lockedChannel < 0 {
		return extractChannel(raw, centreCh), -1
	}

	// Locked — select output channel and reported angle.
	bestDir := 0
	for di := 1; di < nDirections; di++ {
		if b.energySmooth[di] > b.energySmooth[bestDir] {
			bestDir = di
		}
	}

	var ch int
	if steerAngle >= 0 {
		// Fixed-beam: config-driven direction, ignores energy-based lock
		fixedDir := nearestDirection(steerAngle)
		ch = directionToChannel[fixedDir]
		angle = candidateAngles[fixedDir]
	} else {
		// Auto: use the channel selected at Lock() time
		ch = b.lockedChannel
		angle = candidateAngles[bestDir]
	}

	return extractChannel(raw, ch), angle
}

// hfEnergy returns the mean squared HF energy for direction di.
// hfChannels is indexed 0–5 by direction (matching decodeChannels output),
// not by ALSA channel number — directionToChannel maps direction→channel
// for audio extraction, but hfChannels uses direction as the index directly.
func hfEnergy(hfChannels [6][]float32, di int) float64 {
	n := len(hfChannels[0])
	var energy float64
	for _, v := range hfChannels[di] {
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
