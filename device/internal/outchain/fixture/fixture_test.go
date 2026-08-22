package fixture

import (
	"math"
	"path/filepath"
	"strings"
	"testing"
)

const fixturePath = "../testdata/chain_fixture.bin"

// The fixture is a build artefact of the controller's Python chain, and the Go
// implementation it exists to check does not exist yet (#243, #272). These
// tests therefore cover the two things that CAN be wrong today and that would
// silently weaken every future comparison: a parser that disagrees with the
// writer, and a tolerance policy that does not behave as documented.
//
// This is not ceremony. The wakeword fixture's value came from being strict
// about exactly these things BEFORE anything depended on it.

func load(t *testing.T) *Chain {
	t.Helper()
	c, err := Load(filepath.Clean(fixturePath))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	return c
}

func TestFixtureParses(t *testing.T) {
	c := load(t)
	if c.SampleRate != 48000 {
		t.Errorf("sample rate = %d, want 48000", c.SampleRate)
	}
	// The chunk size must match the device's ALSA period, or the fixture
	// exercises boundaries the implementation will never see.
	if c.ChunkSize != 2048 {
		t.Errorf("chunk size = %d, want 2048 (periodSize in pcm_speaker.go)", c.ChunkSize)
	}
	if len(c.Cases) == 0 {
		t.Fatal("no cases")
	}
	t.Logf("%d cases", len(c.Cases))
}

func TestCaseGeometry(t *testing.T) {
	c := load(t)
	for _, cs := range c.Cases {
		if len(cs.Steps) == 0 {
			t.Errorf("%s: no steps", cs.Name)
			continue
		}
		for i, st := range cs.Steps {
			if len(st.In) != c.ChunkSize {
				t.Errorf("%s step %d: input %d samples, want %d",
					cs.Name, i, len(st.In), c.ChunkSize)
			}
			// The chain is not allowed to change the sample count of a
			// chunk — its latency is absorbed by the look-ahead buffer and
			// released by flush(), never by emitting short chunks.
			if len(st.Want) != len(st.In) {
				t.Errorf("%s step %d: output %d samples, input %d",
					cs.Name, i, len(st.Want), len(st.In))
			}
			if len(st.Params.Bands) == 0 {
				t.Errorf("%s step %d: no EQ bands", cs.Name, i)
			}
		}
	}
}

// The transition cases are the point of the whole fixture (section 8.3): a
// port that rebuilds its filters on a parameter change passes every
// steady-state case and fails only these. If a regeneration ever drops them,
// the suite would keep passing while testing much less.
func TestTransitionCasesPresent(t *testing.T) {
	c := load(t)
	seen := map[string]bool{}
	for _, cs := range c.Cases {
		seen[cs.Name] = true
	}
	for _, want := range []string{
		"eq_switch_flat", "eq_switch_curve", "switch_limiter_on",
		"switch_limiter_thr", "switch_guard_on", "sweep_params",
	} {
		if !seen[want] {
			t.Errorf("missing transition case %q — regenerate the fixture", want)
		}
	}
}

// A transition case must actually transition. A generator bug that wrote the
// same params to every step would leave a case that looks like coverage and
// is not.
func TestTransitionCasesActuallyChange(t *testing.T) {
	c := load(t)
	for _, cs := range c.Cases {
		if !strings.Contains(cs.Name, "switch") && cs.Name != "sweep_params" {
			continue
		}
		changed := false
		for i := 1; i < len(cs.Steps); i++ {
			if !sameParams(cs.Steps[i-1].Params, cs.Steps[i].Params) {
				changed = true
				break
			}
		}
		if !changed {
			t.Errorf("%s: parameters never change — the case tests nothing", cs.Name)
		}
	}
}

func sameParams(a, b Params) bool {
	if len(a.Bands) != len(b.Bands) {
		return false
	}
	for i := range a.Bands {
		if a.Bands[i] != b.Bands[i] {
			return false
		}
	}
	return a.Loudness == b.Loudness &&
		a.LimiterEnabled == b.LimiterEnabled &&
		a.LimiterThresholdDB == b.LimiterThresholdDB &&
		a.LimiterReleaseMS == b.LimiterReleaseMS &&
		a.GuardEnabled == b.GuardEnabled &&
		a.GuardDB == b.GuardDB
}

// flush() emits the limiter's held look-ahead, and does so even when the
// limiter is DISABLED — the delay is unconditional so that toggling it does
// not change stream latency and glitch. A port that skipped the tail would
// lose the last few milliseconds of every track.
//
// Which makes the tail an exact test of the isolation flags: a case with a
// limiter attached must have one whatever its enabled state, and a case
// without must have none. If those ever disagree, "stage isolation" is a
// comment rather than a property.
func TestTailMatchesLimiterAttachment(t *testing.T) {
	c := load(t)
	for _, cs := range c.Cases {
		switch {
		case cs.HasLimiter && len(cs.Tail) == 0:
			t.Errorf("%s: limiter attached but no flush tail", cs.Name)
		case !cs.HasLimiter && len(cs.Tail) != 0:
			t.Errorf("%s: no limiter attached but %d tail samples — "+
				"the stage is not isolated", cs.Name, len(cs.Tail))
		}
	}
}

// The EQ-only cases are what make a biquad failure localise to the biquads.
// Without them a wrong coefficient and a wrong limiter look identical: both
// just make "the chain" disagree.
func TestIsolatedEQCasesExist(t *testing.T) {
	c := load(t)
	n := 0
	for _, cs := range c.Cases {
		if !cs.HasLimiter && !cs.HasGuard {
			n++
		}
	}
	if n < 4 {
		t.Errorf("%d isolated EQ cases, want at least 4 — regenerate the fixture", n)
	}
}

func TestCompareIdentical(t *testing.T) {
	x := []int16{0, 100, -100, 32767, -32768}
	r, err := Compare(x, x)
	if err != nil {
		t.Fatalf("Compare: %v", err)
	}
	if !math.IsInf(r.ErrorDB, -1) || r.PeakDiff != 0 || !r.OK() {
		t.Errorf("identical input: %v, want -Inf dB / 0 LSB / OK", r)
	}
}

func TestCompareLengthMismatchIsAnError(t *testing.T) {
	// Not a comparison over the shorter slice: a chain emitting the wrong
	// number of samples has a real bug, and truncating would hide it.
	if _, err := Compare([]int16{1, 2}, []int16{1, 2, 3}); err == nil {
		t.Error("length mismatch accepted, want error")
	}
}

func TestCompareSilentReferenceIsNotDivideByZero(t *testing.T) {
	r, err := Compare([]int16{500, -500}, []int16{0, 0})
	if err != nil {
		t.Fatalf("Compare: %v", err)
	}
	if !math.IsInf(r.ErrorDB, 1) {
		t.Errorf("ErrorDB = %v, want +Inf (output where reference is silent)", r.ErrorDB)
	}
	if r.OK() {
		t.Error("audible output against a silent reference reported OK")
	}
}

// Peak and energy criteria bind at different lengths — that is why both exist.
//
// A single bad sample contributes e/√N to the error RMS, so over a long stream
// the energy criterion stops noticing it. Ten seconds at 48kHz is the scale a
// real listening session works at, and there a lone 200 LSB error — an audible
// click — sits comfortably inside −80dB. Peak is what rejects it.
//
// The first version of this test used one 4096-sample chunk and failed,
// because at that length energy is the STRICTER of the two (it rejects a lone
// error above ~36 LSB). Worth keeping the number in the test name's reasoning:
// the criteria do not differ in kind, they differ in how they scale.
func TestPeakCriterionBindsOnLongComparisons(t *testing.T) {
	n := 48000 * 10
	want := make([]int16, n)
	got := make([]int16, n)
	for i := range want {
		v := int16(8000 * math.Sin(float64(i)*0.01))
		want[i], got[i] = v, v
	}
	got[n/2] += 200 // one sample wrong: inaudible in the average, audible in the room

	r, err := Compare(got, want)
	if err != nil {
		t.Fatalf("Compare: %v", err)
	}
	if r.ErrorDB > MaxErrorDB {
		t.Fatalf("test premise wrong: energy already fails at %.1f dB — "+
			"pick a smaller error or a longer buffer", r.ErrorDB)
	}
	if r.OK() {
		t.Errorf("single-sample error passed both criteria: %v", r)
	}
}

// The converse, so the pair is pinned from both sides: a small error spread
// across every sample is inaudible per-sample but real in aggregate, and peak
// alone would wave it through.
func TestEnergyCriterionCatchesWhatPeakMisses(t *testing.T) {
	n := 4096
	want := make([]int16, n)
	got := make([]int16, n)
	for i := range want {
		v := int16(8000 * math.Sin(float64(i)*0.01))
		want[i] = v
		got[i] = v + 20 // well under MaxPeakDiff, on every single sample
	}
	r, err := Compare(got, want)
	if err != nil {
		t.Fatalf("Compare: %v", err)
	}
	if r.PeakDiff > MaxPeakDiff {
		t.Fatalf("test premise wrong: peak already fails at %d LSB", r.PeakDiff)
	}
	if r.OK() {
		t.Errorf("broadband offset passed both criteria: %v", r)
	}
}
