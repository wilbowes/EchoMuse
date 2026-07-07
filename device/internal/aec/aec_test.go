package aec

import (
	"encoding/binary"
	"math"
	"testing"
)

// synth generates deterministic voice-band-ish noise at 16kHz mono.
func synth(n int) []int16 {
	out := make([]int16, n)
	seed := uint32(0x12345678)
	var lp float64
	for i := range out {
		seed = seed*1664525 + 1013904223
		white := float64(int32(seed)) / float64(1<<31) // [-1,1)
		lp = 0.85*lp + 0.15*white                      // crude low-pass
		out[i] = int16(lp * 12000)
	}
	return out
}

// to48kStereo expands 16k mono to 48k stereo S16LE bytes by 3× sample
// repetition — the canceller's mean-of-3 decimator then reproduces the
// original exactly, so the test controls the post-decimation reference.
func to48kStereo(mono []int16) []byte {
	out := make([]byte, len(mono)*3*4)
	for i, s := range mono {
		for j := 0; j < 3; j++ {
			base := (i*3 + j) * 4
			binary.LittleEndian.PutUint16(out[base:], uint16(s))
			binary.LittleEndian.PutUint16(out[base+2:], uint16(s))
		}
	}
	return out
}

func rms(b []byte) float64 {
	var sum float64
	n := len(b) / 2
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(b[i*2:])))
		sum += v * v
	}
	return math.Sqrt(sum / float64(n))
}

// TestCancellerConvergesOnAlignedEcho drives the full path — WriteFar
// (downmix + decimation) → delay ring → Process — with an echo that is an
// exact delayed copy of the playback at the configured bulk delay. The
// adaptive filter must converge and the residual must drop well below the
// echo level.
func TestCancellerConvergesOnAlignedEcho(t *testing.T) {
	const delayMs = 100
	const frames = 120 // ~3.8s of audio
	delaySamples := delayMs * sampleRate / 1000

	c := New()
	c.SetParams(true, delayMs, 200)

	signal := synth(frames * FrameSize)

	// Mic hears the playback delayed by exactly the bulk delay.
	mic := make([]int16, len(signal))
	copy(mic[delaySamples:], signal[:len(signal)-delaySamples])

	var echoRMS, residRMS float64
	measured := 0
	for f := 0; f < frames; f++ {
		// Feed the far end one frame ahead of the near end.
		c.WriteFar(to48kStereo(signal[f*FrameSize : (f+1)*FrameSize]))

		micBytes := make([]byte, FrameSize*2)
		for i := 0; i < FrameSize; i++ {
			binary.LittleEndian.PutUint16(micBytes[i*2:], uint16(mic[f*FrameSize+i]))
		}
		out := c.Process(micBytes)

		if f >= frames-20 { // measure after convergence
			echoRMS += rms(micBytes)
			residRMS += rms(out)
			measured++
		}
	}
	echoRMS /= float64(measured)
	residRMS /= float64(measured)

	if c.underruns != 0 {
		t.Fatalf("reference ring underran %d times — alignment bug", c.underruns)
	}
	t.Logf("echo RMS %.0f → residual RMS %.0f (%.1f dB attenuation)",
		echoRMS, residRMS, 20*math.Log10(echoRMS/residRMS))
	if residRMS > echoRMS*0.25 { // require ≥ ~12dB of cancellation
		t.Fatalf("insufficient cancellation: echo RMS %.0f, residual RMS %.0f", echoRMS, residRMS)
	}
}

// TestDisabledPassthrough — a disabled canceller must return the input
// untouched (same backing content) and never touch the C state.
func TestDisabledPassthrough(t *testing.T) {
	c := New()
	in := make([]byte, FrameSize*2)
	for i := range in {
		in[i] = byte(i)
	}
	out := c.Process(in)
	if &out[0] != &in[0] {
		t.Fatalf("disabled Process copied/replaced the buffer")
	}
}

// TestParamClamps — out-of-range config must clamp, not crash or allocate
// absurd filter lengths.
func TestParamClamps(t *testing.T) {
	c := New()
	c.SetParams(true, 99999, 99999)
	if c.delayMs != maxDelayMs || c.tailMs != maxTailMs {
		t.Fatalf("clamp failed: delay=%d tail=%d", c.delayMs, c.tailMs)
	}
	c.SetParams(true, -5, 1)
	if c.delayMs != 0 || c.tailMs != minTailMs {
		t.Fatalf("clamp failed: delay=%d tail=%d", c.delayMs, c.tailMs)
	}
	c.SetParams(false, 0, minTailMs)
	if c.st != nil {
		t.Fatalf("disable did not free echo state")
	}
}
