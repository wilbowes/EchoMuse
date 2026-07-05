// Package processor implements the per-period audio processing pipeline
// for the EchoMuse mic stream.
//
// Pipeline (each stage independently bypassable):
//
//	mono S16_LE → NS (RNNoise) → AGC → mono S16_LE
//
// RNNoise processes 480-sample frames; our periods are 512 samples.
// A ring buffer handles the size mismatch transparently.
//
// RNNoise returns a speech probability (0–1) on each frame. This is used
// to refine AGC gating: if RNNoise says "not speech" even when RMS is above
// threshold (e.g. loud TV or HVAC), AGC release is frozen rather than
// amplifying the background noise. The stream VAD gate remains RMS-only.
package processor

import (
	"math"

	"github.com/wilbowes/EchoMuse/internal/rnnoise"
)

const (
	sampleRate = 16000
	periodSize = 512

	// AGC parameters
	agcTargetRMS = 0.08  // target RMS (~-22dBFS)
	agcMaxGain   = 20.0
	agcMinGain   = 0.5
	agcAttack    = 0.05  // fast attack — prevents clipping
	agcRelease   = 0.005 // slow release — avoids pumping

	// RNNoise VAD probability threshold for AGC gating.
	// When RNNoise confidence is below this, AGC release is frozen even if
	// RMS is above the stream VAD threshold. Tunable if needed.
	vadProbThreshold = float32(0.5)
)

// Processor holds inter-period state for the audio pipeline.
type Processor struct {
	// NS state
	ns      *rnnoise.State
	nsBuf   []float32 // ring buffer for 480-sample frame alignment
	nsOut   []float32 // output ring buffer

	// RNNoise VAD probability from the most recently completed frame.
	// Used to gate AGC release independently of the stream VAD.
	// vadHasData is false until at least one RNNoise frame has been processed
	// — during startup latency the probability is meaningless (0.0).
	vadProb    float32
	vadHasData bool

	// AGC state
	agcGain float64
}

// New returns a Processor with sensible initial state.
func New() *Processor {
	return &Processor{
		ns:      rnnoise.New(),
		nsBuf:   make([]float32, 0, rnnoise.FrameSize*2),
		nsOut:   make([]float32, 0, rnnoise.FrameSize*2),
		agcGain: 1.0,
	}
}

// Destroy frees resources held by the Processor. Call when done.
func (p *Processor) Destroy() {
	if p.ns != nil {
		p.ns.Destroy()
		p.ns = nil
	}
}

// Process applies the audio pipeline to one period of mono S16_LE audio.
// nsEnabled gates RNNoise noise suppression.
// agcEnabled gates automatic gain control — when false, audio passes through
// at unity gain (agcGain state is preserved so re-enabling is smooth).
// speech should be true when VAD has detected speech — AGC release is
// frozen during silence to prevent noise floor amplification.
func (p *Processor) Process(mono []byte, nsEnabled bool, agcEnabled bool, speech bool) []byte {
	if len(mono) == 0 {
		return mono
	}

	n := len(mono) / 2
	samples := make([]float32, n)
	for i := 0; i < n; i++ {
		s := int16(uint16(mono[i*2]) | uint16(mono[i*2+1])<<8)
		samples[i] = float32(s) / 32768.0
	}

	if nsEnabled {
		samples = p.noiseSuppress(samples)
	}

	// AGC gating: stream VAD (RMS) is the primary gate for what gets sent
	// to the controller. RNNoise probability refines AGC release only —
	// if RNNoise has warmed up and says "not speech", freeze AGC release
	// even if RMS is above threshold. Prevents gain pumping on loud
	// non-speech sources (TV, HVAC) that fool the RMS threshold.
	if agcEnabled {
		agcSpeech := speech
		if nsEnabled && p.vadHasData && p.vadProb < vadProbThreshold {
			agcSpeech = false
		}
		samples = p.agc(samples, agcSpeech)
	}

	out := make([]byte, len(mono))
	for i, s := range samples {
		if s > 1.0 {
			s = 1.0
		} else if s < -1.0 {
			s = -1.0
		}
		v := int16(s * 32767)
		out[i*2] = byte(v)
		out[i*2+1] = byte(v >> 8)
	}
	return out
}

// noiseSuppress runs RNNoise on the samples.
// Handles the 512→480 sample size mismatch via a ring buffer.
// RNNoise operates on float32 in the range [-32768, 32767] (not [-1, 1]).
func (p *Processor) noiseSuppress(samples []float32) []float32 {
	// RNNoise expects samples scaled to int16 range
	scaled := make([]float32, len(samples))
	for i, s := range samples {
		scaled[i] = s * 32768.0
	}

	p.nsBuf = append(p.nsBuf, scaled...)

	frameIn  := make([]float32, rnnoise.FrameSize)
	frameOut := make([]float32, rnnoise.FrameSize)

	for len(p.nsBuf) >= rnnoise.FrameSize {
		copy(frameIn, p.nsBuf[:rnnoise.FrameSize])
		p.nsBuf = p.nsBuf[rnnoise.FrameSize:]
		// ProcessFrame returns speech probability 0–1. Previously discarded;
		// now stored for AGC gating. Only the most recent frame's probability
		// is kept — for our 512-sample period, usually one frame completes,
		// occasionally two. The latest is always the most relevant.
		p.vadProb = p.ns.ProcessFrame(frameOut, frameIn)
		p.vadHasData = true
		p.nsOut = append(p.nsOut, frameOut...)
	}

	// Drain output buffer — return as many samples as we have,
	// pad with zeros if we don't have enough yet (startup latency)
	out := make([]float32, len(samples))
	if len(p.nsOut) >= len(samples) {
		copy(out, p.nsOut[:len(samples)])
		p.nsOut = p.nsOut[len(samples):]
	} else {
		copy(out, p.nsOut)
		p.nsOut = p.nsOut[:0]
	}

	// Scale back to [-1, 1]
	for i, s := range out {
		out[i] = s / 32768.0
	}
	return out
}

// agc applies automatic gain control targeting agcTargetRMS.
func (p *Processor) agc(samples []float32, speech bool) []float32 {
	var sum float64
	for _, s := range samples {
		sum += float64(s) * float64(s)
	}
	rms := math.Sqrt(sum / float64(len(samples)))

	if rms > 1e-6 {
		target := agcTargetRMS / rms
		if target < p.agcGain {
			p.agcGain += agcAttack * (target - p.agcGain)
		} else if speech {
			p.agcGain += agcRelease * (target - p.agcGain)
		}
	}

	if p.agcGain > agcMaxGain {
		p.agcGain = agcMaxGain
	} else if p.agcGain < agcMinGain {
		p.agcGain = agcMinGain
	}

	out := make([]float32, len(samples))
	for i, s := range samples {
		out[i] = s * float32(p.agcGain)
	}
	return out
}
