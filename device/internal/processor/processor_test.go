package processor

import (
	"math"
	"testing"
)

// The AGC is the only stage in the mic pipeline whose whole job is to be
// wrong in one direction and not the other. It may turn the output DOWN
// without asking; it may turn it UP only when the VAD says somebody is
// speaking. Everything below follows from that asymmetry, and it is why these
// tests are about the flag rather than about the gain value.

const (
	rate16k = 16000
	period  = 512 // samples — the device's 32ms period
)

// constTone builds a DC-ish constant sample value, which has an RMS exactly
// equal to its amplitude. Cheaper to reason about than a sine and it is what
// the clamp paths care about.
func constTone(amp float64, n int) []byte {
	out := make([]byte, n*2)
	v := int16(amp * 32767)
	for i := 0; i < n; i++ {
		out[i*2] = byte(v)
		out[i*2+1] = byte(v >> 8)
	}
	return out
}

func rmsOf(p []byte) float64 {
	n := len(p) / 2
	if n == 0 {
		return 0
	}
	var sum float64
	for i := 0; i < n; i++ {
		s := int16(uint16(p[i*2]) | uint16(p[i*2+1])<<8)
		f := float64(s) / 32768.0
		sum += f * f
	}
	return math.Sqrt(sum / float64(n))
}

func gain(p *Processor) float64 { return p.agcGain }

// ─── The silence rule ────────────────────────────────────────────────────────

// Noise-floor amplification is the failure this whole stage exists to prevent.
// If gain were free to rise during silence it would amplify the room, and the
// wake-word scorer sitting downstream would see a noise floor that tracks the
// AGC rather than the room.
func TestGainDoesNotRiseIntoSilence(t *testing.T) {
	p := New()
	// Long quiet period, VAD says nobody is speaking.
	for i := 0; i < 200; i++ {
		p.Process(constTone(0.0004, period), true, false)
	}
	if g := gain(p); g != 1.0 {
		t.Fatalf("gain rose to %.4f during silence; the noise floor gets amplified "+
			"and the scorer downstream measures the AGC rather than the room", g)
	}
}

// The control for the test above. Without it, a Process() that never touched
// agcGain at all would pass it.
func TestGainDoesRiseIntoSpeech(t *testing.T) {
	p := New()
	for i := 0; i < 200; i++ {
		p.Process(constTone(0.01, period), true, true)
	}
	if g := gain(p); g <= 1.0 {
		t.Fatalf("gain is %.4f after 200 periods of quiet speech at RMS 0.01; "+
			"the target is %.2f and it should be climbing toward it",
			g, agcTargetRMS/0.01)
	}
}

// Attack is NOT gated on the speech flag, and that is deliberate: it is the
// only thing standing between a loud noise and a clipped sample. Symmetry with
// the rule above would mean the AGC could not react to a noise loud enough to
// clip until the VAD had already been fooled by it.
func TestGainFallsOnLoudAudioEvenWithNoSpeech(t *testing.T) {
	p := New()
	for i := 0; i < 50; i++ {
		p.Process(constTone(0.6, period), true, false) // speech = false throughout
	}
	if g := gain(p); g >= 1.0 {
		t.Fatalf("gain is %.4f after 50 loud periods with speech=false; attack "+
			"must not wait for the VAD or a loud noise clips before it is caught", g)
	}
}

// ─── Attack and release are not the same speed ───────────────────────────────

// Both directions move, and the constants are what make them different. A
// release as fast as the attack pumps audibly on every pause between words.
func TestReleaseIsSlowerThanAttack(t *testing.T) {
	if agcRelease >= agcAttack {
		t.Fatalf("agcRelease (%g) must be slower than agcAttack (%g); equal "+
			"rates make the gain pump audibly between words", agcRelease, agcAttack)
	}
}

func TestAttackMovesMostOfTheWayInOnePeriod(t *testing.T) {
	p := New()
	// Attack is the DOWNWARD direction, so it needs a LOUD period: a loud input
	// asks for a low target, and the gap from unity is visible in one step. A
	// quiet input asks for a HIGH target, which is the release path — the two
	// are named for the direction the gain moves, not for how quiet it is.
	p.Process(constTone(0.9, period), true, false)
	want := agcTargetRMS / 0.9
	moved := gain(p) - 1.0

	if moved >= 0 {
		t.Fatalf("gain rose to %.4f on a loud period; that is the release "+
			"direction and this test is not reaching attack", gain(p))
	}
	fraction := moved / (want - 1.0)
	if math.Abs(fraction-agcAttack) > 0.01 {
		t.Fatalf("attack moved %.4f of the way in one period, want %.4f",
			fraction, agcAttack)
	}
}

// The control for the one above: the same quiet period moves by agcRelease,
// not agcAttack. Without this the two directions could swap and one test would
// still pass.
func TestReleaseMovesFarLessThanAttackInOnePeriod(t *testing.T) {
	p := New()
	p.Process(constTone(0.01, period), true, true)
	want := agcTargetRMS/0.01 - 1.0
	fraction := (gain(p) - 1.0) / want

	if math.Abs(fraction-agcRelease) > 0.001 {
		t.Fatalf("release moved %.4f of the way in one period, want %.4f",
			fraction, agcRelease)
	}
}

// ─── Clamps ──────────────────────────────────────────────────────────────────

// A very quiet input asks for a target of 320, which is 16x the ceiling. The
// clamp is what stops that being applied.
func TestGainIsClampedToTheCeiling(t *testing.T) {
	p := New()
	p.Process(constTone(0.0001, period), true, true)
	if g := gain(p); g > agcMaxGain {
		t.Fatalf("gain %.4f exceeds the %.1f ceiling", g, agcMaxGain)
	}
}

func TestGainIsClampedToTheFloor(t *testing.T) {
	p := New()
	for i := 0; i < 500; i++ {
		p.Process(constTone(0.999, period), true, true)
	}
	if g := gain(p); g < agcMinGain {
		t.Fatalf("gain %.4f is below the %.1f floor", g, agcMinGain)
	}
}

// Below the floor the AGC would be attenuating, which is not what an AGC is
// for — and on this hardware the signal is already 24dB up from micGainDb.
func TestGainNeverAttenuatesBelowUnityByDefault(t *testing.T) {
	p := New()
	for i := 0; i < 500; i++ {
		p.Process(constTone(0.95, period), true, true)
	}
	if g := gain(p); g < agcMinGain {
		t.Fatalf("gain %.4f fell below the %.1f floor on loud input", g, agcMinGain)
	}
}

// ─── Bypass preserves state ──────────────────────────────────────────────────

// Re-enabling mid-stream must not snap the gain to unity. The config push
// repeats every setting on every reconnect, so a device toggling agcEnabled
// sees the bypass come and go — and a jump from 0.5 to 1.0 is an audible step.
func TestDisabledPassesAudioThroughAndKeepsGain(t *testing.T) {
	p := New()
	for i := 0; i < 100; i++ {
		p.Process(constTone(0.01, period), true, true)
	}
	before := gain(p)
	if before == 1.0 {
		t.Fatal("control: the gain should have moved before the bypass is tested")
	}

	in := constTone(0.01, period)
	out := p.Process(in, false, true)

	if string(out) != string(in) {
		t.Error("AGC disabled must pass the period through byte-for-byte")
	}
	if after := gain(p); after != before {
		t.Errorf("gain moved %.6f -> %.6f across a bypass; re-enabling snaps "+
			"the level", before, after)
	}
}

func TestEmptyInputIsHandled(t *testing.T) {
	p := New()
	if got := p.Process(nil, true, true); len(got) != 0 {
		t.Errorf("empty input returned %d bytes", len(got))
	}
}

// A period that is not a whole number of samples must not produce a length the
// caller cannot use, and must not panic on the trailing odd byte.
func TestOddLengthInputDoesNotPanic(t *testing.T) {
	p := New()
	for _, n := range []int{1, 3, 511, 513} {
		in := make([]byte, n)
		out := p.Process(in, true, true)
		if len(out) != n {
			t.Errorf("len %d in, %d out", n, len(out))
		}
	}
}

// ─── Reset ───────────────────────────────────────────────────────────────────

// A gain contaminated by one turn's echo must not survive into the next. The
// attack path is always active, so a loud TTS echo drives the gain to the floor
// within a turn, and speech-gated release then needs seconds of real speech to
// climb back — so without a reset the next turn starts deaf.
func TestResetReturnsGainToUnity(t *testing.T) {
	p := New()
	for i := 0; i < 300; i++ {
		p.Process(constTone(0.999, period), true, true)
	}
	if g := gain(p); g == 1.0 {
		t.Fatal("control: the gain should be contaminated before the reset")
	}
	p.ResetAGC()
	if g := gain(p); g != 1.0 {
		t.Fatalf("gain is %.6f after ResetAGC, want unity", g)
	}
}

// ─── The output conversion ───────────────────────────────────────────────────

// The scale factors are asymmetric — /32768 in, *32767 out — and that is not an
// oversight. int16 is asymmetric: -32768 exists and +32767 is the largest
// positive value. Dividing by 32768 maps -32768 to exactly -1.0.
func TestFullScaleNegativeSaturatesRatherThanWrapping(t *testing.T) {
	p := New()
	// Raise the gain well above unity so the boosted peak has to be clamped.
	// (At unity the AGC would simply attenuate a full-scale input, which is
	// correct and is not the path where a wrap can happen.)
	for i := 0; i < 100; i++ {
		p.Process(constTone(0.01, period), true, true)
	}
	if g := gain(p); g < 2.0 {
		t.Fatalf("control: expected a gain above 2.0, got %.3f", g)
	}

	in := make([]byte, 2)
	in[0], in[1] = 0x00, 0x80 // -32768, the one value with no positive twin
	out := p.Process(in, true, false)
	got := int16(uint16(out[0]) | uint16(out[1])<<8)

	// -1.0 * gain clamps to -1.0, which is -32767 on the way out. The failure
	// this pins is the int16 WRAP that a missing clamp produces: +32767, the
	// loudest possible artefact, from the quietest possible input.
	if got != -32767 {
		t.Fatalf("full-scale negative became %d, want -32767; a wrap would "+
			"have produced +32767", got)
	}
}

// Gain is applied before the clamp, so a boosted peak must saturate rather than
// wrap. int16 wrap turns a loud peak into full-scale opposite polarity, which
// is far worse than the clipping it replaces.
func TestOutputNeverWrapsOnABoostedPeak(t *testing.T) {
	p := New()
	// Drive the gain to the ceiling first.
	for i := 0; i < 200; i++ {
		p.Process(constTone(0.02, period), true, true)
	}
	if g := gain(p); g < 2.0 {
		t.Fatalf("control: expected the gain well above unity, got %.3f", g)
	}

	for _, amp := range []float64{0.5, 0.9, 1.0} {
		in := make([]byte, 2)
		v := int16(amp * 32767)
		in[0], in[1] = byte(v), byte(v>>8)
		out := p.Process(in, true, false)
		got := int16(uint16(out[0]) | uint16(out[1])<<8)
		if got < 0 && amp > 0 {
			t.Fatalf("positive peak %v became %d — that is an int16 wrap", amp, got)
		}
		if got > 32767 || got < -32768 {
			t.Fatalf("amplitude %v produced out-of-range %d", amp, got)
		}
	}
}

// The floor guard. A period of true digital silence has RMS 0, so `rms > 1e-6`
// is false and no target is computed — the division by zero never happens.
func TestDigitalSilenceIsHandled(t *testing.T) {
	p := New()
	silence := make([]byte, period*2)
	for i := 0; i < 50; i++ {
		p.Process(silence, true, true)
	}
	if g := gain(p); g != 1.0 {
		t.Fatalf("digital silence moved the gain to %.6f", g)
	}
	if r := rmsOf(p.Process(silence, true, true)); r != 0 {
		t.Fatalf("silence came back at RMS %g", r)
	}
}

// The 1e-6 floor is a real threshold with a purpose: below it, raising the gain
// would amplify the noise floor rather than a voice.
func TestTheNearSilentFloorIsNotGainable(t *testing.T) {
	p := New()
	// RMS well under 1e-6 — roughly -120dBFS.
	p.Process(constTone(0.0000005, period), true, true)
	if g := gain(p); g != 1.0 {
		t.Fatalf("gain moved to %.6f for input below the 1e-6 floor", g)
	}
}

// The gain is never zero, so a long run cannot divide by it or produce NaN
// downstream. This is the property that makes the whole stage safe to leave on.
func TestGainIsAlwaysFiniteAndPositive(t *testing.T) {
	p := New()
	levels := []float64{1e-7, 1e-5, 1e-3, 0.01, 0.1, 0.5, 0.999, 1.0}
	for round := 0; round < 20; round++ {
		for _, amp := range levels {
			for _, speech := range []bool{true, false} {
				p.Process(constTone(amp, period), true, speech)
				g := gain(p)
				if math.IsNaN(g) || math.IsInf(g, 0) {
					t.Fatalf("gain became %g at amp %v speech=%v", g, amp, speech)
				}
				if g <= 0 {
					t.Fatalf("gain became %g — a zero or negative gain "+
						"silences or inverts the stream", g)
				}
			}
		}
	}
}
