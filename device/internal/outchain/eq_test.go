package outchain

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain/fixture"
)

const fixturePath = "testdata/chain_fixture.bin"

// TestEQAgainstReference is the test this whole package exists for: replay the
// isolated EQ cases against the Go implementation and require agreement with
// what the controller's Python produced.
//
// Only the cases with NO limiter and NO guard attached are used. A disabled
// limiter still applies its 5ms look-ahead delay, so the other cases carry
// latency this stage does not produce, and comparing against them would fail
// for a reason that has nothing to do with the EQ.
func TestEQAgainstReference(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}

	ran := 0
	for _, cs := range fx.Cases {
		if cs.HasLimiter || cs.HasGuard {
			continue
		}
		ran++
		t.Run(cs.Name, func(t *testing.T) {
			first := cs.Steps[0].Params
			eq := NewEQ(fx.SampleRate, toF64(first.Bands), first.Loudness)

			// Tracked with an explicit flag rather than by seeding ErrorDB
			// to -Inf: a bit-identical port scores -Inf on every step, and
			// `>` never fires, so the "worst" reported would be the
			// zero-value Result — 0 samples, -Inf dB, which is exactly what
			// a vacuous pass looks like too. Counting samples is what tells
			// the two apart.
			var worst fixture.Result
			haveWorst := false
			totalSamples := 0
			for i, st := range cs.Steps {
				// One chain instance across every step, updated in place —
				// the reference does the same, and rebuilding here would
				// pass the steady-state cases and fail the transitions.
				eq.SetBands(toF64(st.Params.Bands), st.Params.Loudness)

				buf := make([]float64, len(st.In))
				for j, v := range st.In {
					buf[j] = float64(v)
				}
				eq.Process(buf)

				got := make([]int16, len(buf))
				for j, v := range buf {
					got[j] = clamp16(v)
				}

				r, err := fixture.Compare(got, st.Want)
				if err != nil {
					t.Fatalf("step %d: %v", i, err)
				}
				if !r.OK() {
					t.Errorf("step %d disagrees: %v", i, r)
				}
				if !haveWorst || r.ErrorDB > worst.ErrorDB {
					worst, haveWorst = r, true
				}
				totalSamples += r.N
			}
			if totalSamples == 0 {
				t.Fatal("compared 0 samples — the case is empty and this " +
					"test proved nothing")
			}
			// A shaped case must actually be shaped. Without this, an EQ
			// whose Process did nothing at all would pass every assertion
			// above, provided the reference also happened to leave the
			// signal alone — which is precisely what the flat case does.
			if shaped(cs.Steps[len(cs.Steps)-1].Params) &&
				identical(cs.Steps[len(cs.Steps)-1].In, cs.Steps[len(cs.Steps)-1].Want) {
				t.Error("reference output equals its input on a shaped step — " +
					"a do-nothing implementation would pass this case")
			}
			// Logged whether or not it passed: the first real port is
			// supposed to report its actual figures so the provisional
			// tolerances can be tightened to just above them.
			t.Logf("%d samples compared, worst step: %v", totalSamples, worst)
		})
	}
	if ran == 0 {
		t.Fatal("no isolated EQ cases in the fixture — nothing was tested")
	}
	t.Logf("%d isolated EQ cases", ran)
}

// clamp16 matches the reference's np.clip(...).astype(np.int16): saturate,
// never wrap. A wrap turns a loud peak into a full-scale opposite-polarity
// one, which is the same reason the mixer saturates.
func clamp16(v float64) int16 {
	switch {
	case v > 32767:
		return 32767
	case v < -32768:
		return -32768
	}
	return int16(v)
}

// shaped reports whether these params ask the EQ to do anything at all.
func shaped(p fixture.Params) bool {
	if p.Loudness {
		return true
	}
	for _, b := range p.Bands {
		if b != 0 {
			return true
		}
	}
	return false
}

func identical(a, b []int16) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func toF64(in []float32) []float64 {
	out := make([]float64, len(in))
	for i, v := range in {
		out[i] = float64(v)
	}
	return out
}

// Flat must be a real bypass rather than a chain of unity biquads. The
// reference returns the input untouched, and "mathematically equivalent" is
// not the same as "bit-identical" once floating point is involved.
func TestFlatIsPassthrough(t *testing.T) {
	eq := NewEQ(48000, make([]float64, NumBands), false)
	if !eq.Flat() {
		t.Fatal("all-zero bands with no loudness should be flat")
	}
	in := []float64{1, -2, 3, -4, 32767, -32768}
	want := append([]float64(nil), in...)
	eq.Process(in)
	for i := range in {
		if in[i] != want[i] {
			t.Errorf("sample %d: %v, want %v (flat must not touch the signal)",
				i, in[i], want[i])
		}
	}
}

// Loudness is a ninth section, not a modification of the eight.
func TestLoudnessAddsASection(t *testing.T) {
	bands := make([]float64, NumBands)
	bands[0] = 3.0
	eq := NewEQ(48000, bands, false)
	if got := eq.Sections(); got != NumBands {
		t.Errorf("sections = %d, want %d", got, NumBands)
	}
	eq.SetBands(bands, true)
	if got := eq.Sections(); got != NumBands+1 {
		t.Errorf("with loudness: sections = %d, want %d", got, NumBands+1)
	}
}

// A curve change at the same section count must KEEP the filter state — that
// is the no-click requirement (section 8.3) at the level this stage can
// enforce it. Asserted directly rather than only through the fixture, so the
// intent survives a fixture regeneration.
func TestSetBandsKeepsStateWhenCountUnchanged(t *testing.T) {
	bands := make([]float64, NumBands)
	bands[3] = 6.0
	eq := NewEQ(48000, bands, false)

	// Run some signal in so the states are non-zero.
	buf := make([]float64, 512)
	for i := range buf {
		buf[i] = 8000 * math.Sin(float64(i)*0.05)
	}
	eq.Process(buf)

	before := make([]float64, 0, 2*len(eq.sections))
	for _, s := range eq.sections {
		before = append(before, s.z0, s.z1)
	}

	bands[3] = -2.0
	eq.SetBands(bands, false)

	after := make([]float64, 0, 2*len(eq.sections))
	for _, s := range eq.sections {
		after = append(after, s.z0, s.z1)
	}

	same := len(before) == len(after)
	if same {
		for i := range before {
			if before[i] != after[i] {
				same = false
				break
			}
		}
	}
	if !same {
		t.Error("filter state was reset by a curve change at the same section " +
			"count — that is the click section 8.3 forbids")
	}
}

// ...and must DROP it when the count changes, because that is what the
// reference does. Reproducing the reference's edge rather than improving on it
// is deliberate: see the comment on SetBands.
func TestSetBandsDropsStateWhenCountChanges(t *testing.T) {
	bands := make([]float64, NumBands)
	bands[3] = 6.0
	eq := NewEQ(48000, bands, false)

	buf := make([]float64, 512)
	for i := range buf {
		buf[i] = 8000 * math.Sin(float64(i)*0.05)
	}
	eq.Process(buf)

	nonZero := false
	for _, s := range eq.sections {
		if s.z0 != 0 || s.z1 != 0 {
			nonZero = true
			break
		}
	}
	if !nonZero {
		t.Fatal("test premise wrong: no state to lose")
	}

	eq.SetBands(bands, true) // 8 -> 9 sections
	for i, s := range eq.sections {
		if s.z0 != 0 || s.z1 != 0 {
			t.Errorf("section %d kept state across a section-count change", i)
		}
	}
}
