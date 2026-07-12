// Package processor implements the per-period audio processing pipeline
// for the EchoMuse mic stream.
//
// Pipeline:
//
//	mono S16_LE → AGC → mono S16_LE
//
// RNNoise NS was removed 2026-07-12: it never ran correctly on-device
// (48kHz-native model fed 16kHz audio — P0-3) and noise suppression now
// lives controller-side (em_ns.py, DTLN) on the ASR-bound stream only,
// per the dumb-transducer architecture. With it went the speech-probability
// interlock that refined AGC release — that interlock was dead code
// whenever NS was disabled (the shipped state since v2.6.x), so AGC
// behaviour is unchanged in practice: release is gated on the stream's
// RMS speech flag alone.
package processor

import (
	"math"
)

const (
	// AGC parameters
	agcTargetRMS = 0.08 // target RMS (~-22dBFS)
	agcMaxGain   = 20.0
	agcMinGain   = 0.5
	agcAttack    = 0.05  // fast attack — prevents clipping
	agcRelease   = 0.005 // slow release — avoids pumping
)

// Processor holds inter-period state for the audio pipeline.
type Processor struct {
	// AGC state
	agcGain float64
}

// New returns a Processor with sensible initial state.
func New() *Processor {
	return &Processor{
		agcGain: 1.0,
	}
}

// ResetAGC returns the AGC gain to unity. Called at mic stream start so a
// contaminated gain (e.g. loud TTS echo driving it to agcMinGain via the
// always-active attack path, which then takes many seconds of speech-gated
// release to recover) cannot carry across voice turns or survive a mic
// restart.
func (p *Processor) ResetAGC() {
	p.agcGain = 1.0
}

// Destroy frees resources held by the Processor. Retained as a no-op so the
// lifecycle contract survives the RNNoise removal.
func (p *Processor) Destroy() {}

// Process applies the audio pipeline to one period of mono S16_LE audio.
// agcEnabled gates automatic gain control — when false, audio passes through
// at unity gain (agcGain state is preserved so re-enabling is smooth).
// speech should be true when VAD has detected speech — AGC release is
// frozen during silence to prevent noise floor amplification.
func (p *Processor) Process(mono []byte, agcEnabled bool, speech bool) []byte {
	if len(mono) == 0 || !agcEnabled {
		return mono
	}

	n := len(mono) / 2
	samples := make([]float32, n)
	for i := 0; i < n; i++ {
		s := int16(uint16(mono[i*2]) | uint16(mono[i*2+1])<<8)
		samples[i] = float32(s) / 32768.0
	}

	samples = p.agc(samples, speech)

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
