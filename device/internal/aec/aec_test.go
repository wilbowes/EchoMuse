package aec

import (
	"bytes"
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

// TestGovernorRecoversFromMicGap — regression for the stale-reference bug
// (2026-07-08): WriteFar runs continuously (speaker silence loop) but
// Process stops with the mic stream, which is restarted around every voice
// turn. Each gap leaves unconsumed reference behind; rates are identical so
// the backlog never drains, compounding until the ring pegs at ringCap and
// the reference is 3s stale — beyond any tail, cancelling nothing, with no
// underruns to give it away. The occupancy governor in Process must trim
// the backlog on the first period after a gap and re-converge.
func TestGovernorRecoversFromMicGap(t *testing.T) {
	const delayMs = 100
	const preGap, gap, postGap = 60, 31, 60 // frames; gap ≈ 1s of mic downtime
	delaySamples := delayMs * sampleRate / 1000

	c := New()
	c.SetParams(true, delayMs, 200)

	signal := synth((preGap + gap + postGap) * FrameSize)
	mic := make([]int16, len(signal))
	copy(mic[delaySamples:], signal[:len(signal)-delaySamples])

	processFrame := func(f int) []byte {
		micBytes := make([]byte, FrameSize*2)
		for i := 0; i < FrameSize; i++ {
			binary.LittleEndian.PutUint16(micBytes[i*2:], uint16(mic[f*FrameSize+i]))
		}
		return c.Process(micBytes)
	}

	// Phase 1: normal operation, converges.
	for f := 0; f < preGap; f++ {
		c.WriteFar(to48kStereo(signal[f*FrameSize : (f+1)*FrameSize]))
		processFrame(f)
	}
	// Phase 2: mic stream down — speaker keeps feeding the ring.
	for f := preGap; f < preGap+gap; f++ {
		c.WriteFar(to48kStereo(signal[f*FrameSize : (f+1)*FrameSize]))
	}
	// Phase 3: mic stream back. Governor must trim, then re-converge.
	var echoRMS, residRMS float64
	measured := 0
	for f := preGap + gap; f < preGap+gap+postGap; f++ {
		c.WriteFar(to48kStereo(signal[f*FrameSize : (f+1)*FrameSize]))
		micBytes := make([]byte, FrameSize*2)
		for i := 0; i < FrameSize; i++ {
			binary.LittleEndian.PutUint16(micBytes[i*2:], uint16(mic[f*FrameSize+i]))
		}
		out := c.Process(micBytes)
		if f >= preGap+gap+postGap-20 {
			echoRMS += rms(micBytes)
			residRMS += rms(out)
			measured++
		}
	}
	echoRMS /= float64(measured)
	residRMS /= float64(measured)

	if c.resyncs != 1 {
		t.Fatalf("expected exactly 1 reference resync after the gap, got %d", c.resyncs)
	}
	if c.underruns != 0 {
		t.Fatalf("reference ring underran %d times after resync", c.underruns)
	}
	t.Logf("post-gap: echo RMS %.0f → residual RMS %.0f (%.1f dB attenuation)",
		echoRMS, residRMS, 20*math.Log10(echoRMS/residRMS))
	if residRMS > echoRMS*0.25 { // require ≥ ~12dB of cancellation post-recovery
		t.Fatalf("cancellation did not recover after gap: echo RMS %.0f, residual RMS %.0f", echoRMS, residRMS)
	}
}

// TestHardwareShapedBuffers — regression for the silent-bypass bug
// (2026-07-08): the mic ALSA reader delivers 2560-sample batches (GoTinyAlsa
// GetAudioStream reads the whole 5-period buffer per chunk), not single
// 512-sample frames. Process()'s old exact-size guard passed those through
// untouched, so AEC never ran on hardware while single-frame unit tests
// showed 42dB. This test drives Process with hardware-shaped buffers and
// requires real cancellation.
func TestHardwareShapedBuffers(t *testing.T) {
	const batch = 5 * FrameSize // 2560 samples = 160ms, as delivered on hardware
	const delayMs = 0
	const batches = 40 // ~6.4s

	c := New()
	c.SetParams(true, delayMs, 300)

	signal := synth(batches * batch)
	// delay=0: echo aligned with the reference as written (the residual
	// true-path delay is the filter tail's job on hardware; zero here).
	mic := signal

	var echoRMS, residRMS float64
	measured := 0
	for f := 0; f < batches; f++ {
		c.WriteFar(to48kStereo(signal[f*batch : (f+1)*batch]))

		micBytes := make([]byte, batch*2)
		for i := 0; i < batch; i++ {
			binary.LittleEndian.PutUint16(micBytes[i*2:], uint16(mic[f*batch+i]))
		}
		out := c.Process(micBytes)
		if len(out) != len(micBytes) {
			t.Fatalf("Process returned %db for %db input", len(out), len(micBytes))
		}
		if f >= batches-8 {
			echoRMS += rms(micBytes)
			residRMS += rms(out)
			measured++
		}
	}
	echoRMS /= float64(measured)
	residRMS /= float64(measured)

	if c.sizeWarned {
		t.Fatalf("Process rejected the hardware buffer size (%d samples)", batch)
	}
	t.Logf("hardware-shaped: echo RMS %.0f → residual RMS %.0f (%.1f dB attenuation)",
		echoRMS, residRMS, 20*math.Log10(echoRMS/residRMS))
	if residRMS > echoRMS*0.25 { // require ≥ ~12dB of cancellation
		t.Fatalf("insufficient cancellation on hardware-shaped buffers: echo %.0f, residual %.0f", echoRMS, residRMS)
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

// ─── Hardware far-end reference (#385) ────────────────────────────────────────

// toBytes renders 16kHz mono S16 samples as the wire format both Process and
// ProcessWithRef take.
func toBytes(s []int16) []byte {
	b := make([]byte, len(s)*2)
	for i, v := range s {
		binary.LittleEndian.PutUint16(b[i*2:], uint16(v))
	}
	return b
}

// TestHardwareRefCancelsWithoutRingOrDelay is the point of #385: the
// reference arrives in the same frame as the near end, so there is no ring,
// no aecDelayMs and no governor — and cancellation must still converge.
//
// The echo is delayed by 33 samples, the offset measured on hardware
// (2.06ms of converter and acoustic delay), and inverted, because the
// measured mic-to-reference correlation was negative. Both are things the
// adaptive filter has to absorb rather than things the caller aligns.
func TestHardwareRefCancelsWithoutRingOrDelay(t *testing.T) {
	const frames = 120
	const echoDelay = 33 // samples, measured on biscuit

	c := New()
	c.SetParams(true, 0, 200)
	c.SetHardwareRef(true)

	signal := synth(frames * FrameSize)
	mic := make([]int16, len(signal))
	for i := echoDelay; i < len(signal); i++ {
		mic[i] = -signal[i-echoDelay] // inverted, as measured
	}

	var echoRMS, residRMS float64
	measured := 0
	for f := 0; f < frames; f++ {
		lo, hi := f*FrameSize, (f+1)*FrameSize
		micBytes := toBytes(mic[lo:hi])
		refBytes := toBytes(signal[lo:hi])
		out := c.ProcessWithRef(micBytes, refBytes)
		if f >= frames-20 {
			echoRMS += rms(micBytes)
			residRMS += rms(out)
			measured++
		}
	}
	echoRMS /= float64(measured)
	residRMS /= float64(measured)

	t.Logf("echo RMS %.0f → residual RMS %.0f (%.1f dB attenuation)",
		echoRMS, residRMS, 20*math.Log10(echoRMS/residRMS))
	if residRMS > echoRMS*0.25 {
		t.Fatalf("insufficient cancellation: echo RMS %.0f, residual RMS %.0f",
			echoRMS, residRMS)
	}
	if c.underruns != 0 || c.resyncs != 0 {
		t.Fatalf("ring machinery ran on the hardware path: underruns=%d resyncs=%d",
			c.underruns, c.resyncs)
	}
	if c.hwFrames == 0 {
		t.Fatal("no frames took the hardware path")
	}
}

// TestHardwareRefIgnoresWriteFar pins that the ring is not being filled while
// the hardware path is live. Nothing drains it there, so a WriteFar that kept
// pushing would peg it at ringCap and leave the far-end telemetry describing a
// buffer no cancellation ever reads.
func TestHardwareRefIgnoresWriteFar(t *testing.T) {
	c := New()
	c.SetParams(true, 100, 200)
	c.SetHardwareRef(true)

	signal := synth(FrameSize)
	for i := 0; i < 50; i++ {
		c.WriteFar(to48kStereo(signal))
	}
	if c.count != 0 {
		t.Fatalf("WriteFar filled the ring on the hardware path: %d samples", c.count)
	}
}

// TestMissingHardwareRefPassesThrough — a caller that promises a hardware
// reference and supplies none must not have its audio cancelled against
// silence. That would be indistinguishable from a working AEC with nothing
// playing, which is exactly the failure this project has already paid for
// once (0dB cancellation, no underruns to give it away).
func TestMissingHardwareRefPassesThrough(t *testing.T) {
	c := New()
	c.SetParams(true, 0, 200)
	c.SetHardwareRef(true)

	mic := toBytes(synth(FrameSize))
	out := c.ProcessWithRef(mic, nil)
	if !bytes.Equal(out, mic) {
		t.Fatal("frame was altered despite having no reference")
	}
	if c.hwSilent == 0 {
		t.Fatal("missing reference was not counted")
	}

	short := make([]byte, FrameSize) // half a frame
	if out := c.ProcessWithRef(mic, short); !bytes.Equal(out, mic) {
		t.Fatal("mismatched reference length was not rejected")
	}
}

// TestSwitchingSourcesDropsStaleRing — the ring's contents are meaningless
// across a switch (never consumed on the way in, stale by however long the
// hardware path ran on the way out), and a stale reference is the one thing
// that silently produces zero cancellation.
func TestSwitchingSourcesDropsStaleRing(t *testing.T) {
	c := New()
	c.SetParams(true, 100, 200)

	signal := synth(FrameSize)
	for i := 0; i < 20; i++ {
		c.WriteFar(to48kStereo(signal))
	}
	if c.count == 0 {
		t.Fatal("precondition failed: ring should hold software-tap samples")
	}
	c.SetHardwareRef(true)
	if c.count != 0 {
		t.Fatalf("stale ring survived the switch: %d samples", c.count)
	}
}

// TestReferenceScalingSurvivesAVolumeChange is the regression for what the
// field log showed on 2026-08-29: the hardware reference is tapped upstream
// of the DAC volume control, so a volume change is a step in the echo path
// gain that the filter can only discover by re-converging — cancellation
// dropped to -1.7dB immediately after a change and took 3-4 seconds to climb
// back, over and over.
//
// The device SETS that volume, so it need not be guessed. Two identical runs,
// one told about the change and one not, and the told one must be better in
// exactly the frames after it.
func TestReferenceScalingSurvivesAVolumeChange(t *testing.T) {
	const frames = 160
	const changeAt = 100
	const echoDelay = 33
	// 87 is 40 steps below unity at 0.5dB each: exactly -20dB, so the echo
	// drops by a clean factor of 10.
	const newLevel = 87
	const newGain = 0.1

	run := func(tellIt bool) float64 {
		c := New()
		c.SetParams(true, 0, 200)
		c.SetHardwareRef(true)
		c.SetPlaybackLevel(127) // unity to begin with

		signal := synth(frames * FrameSize)
		var residual float64
		measured := 0
		for f := 0; f < frames; f++ {
			lo, hi := f*FrameSize, (f+1)*FrameSize
			gain := 1.0
			if f >= changeAt {
				gain = newGain
			}
			if f == changeAt && tellIt {
				c.SetPlaybackLevel(newLevel)
			}
			mic := make([]int16, FrameSize)
			for i := 0; i < FrameSize; i++ {
				src := lo + i - echoDelay
				if src >= 0 {
					mic[i] = int16(-float64(signal[src]) * gain)
				}
			}
			out := c.ProcessWithRef(toBytes(mic), toBytes(signal[lo:hi]))
			// The transient is what is being measured: the frames right
			// after the change, before any filter would have re-converged.
			if f >= changeAt && f < changeAt+15 {
				residual += rms(out)
				measured++
			}
		}
		return residual / float64(measured)
	}

	told, untold := run(true), run(false)
	t.Logf("post-change residual RMS: told %.1f, untold %.1f (%.1fdB better)",
		told, untold, 20*math.Log10(untold/math.Max(told, 1e-9)))
	if told >= untold {
		t.Fatalf("scaling the reference did not help across a volume change: "+
			"told %.1f, untold %.1f", told, untold)
	}
}

func TestPlaybackLevelScaleIsTheCodecLaw(t *testing.T) {
	c := New()
	// 0.5dB per step, unity at 127 — the control's own law, see Volume in
	// device/CLAUDE.md. Getting this wrong scales the reference to something
	// the speaker never played, which is worse than not scaling at all.
	for _, tc := range []struct {
		level int
		want  float64
	}{
		{127, 1.0},      // unity
		{87, 0.1},       // -20dB
		{47, 0.01},      // -40dB, the button floor
	} {
		c.SetPlaybackLevel(tc.level)
		if math.Abs(c.refScale-tc.want) > tc.want*0.001 {
			t.Errorf("level %d → scale %.6f, want %.6f", tc.level, c.refScale, tc.want)
		}
	}
}
