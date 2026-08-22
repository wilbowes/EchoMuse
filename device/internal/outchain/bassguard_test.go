package outchain

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain/fixture"
)

// The filter DESIGN is checked before any audio is compared. A wrong
// bilinear-transform detail and a wrong gain law both present as "the guard
// disagrees", and they want completely different investigations — so the
// coefficients get their own assertion against the ones scipy actually
// produced.
//
// Not bit-exact by construction: this is the closed-form bilinear transform,
// scipy runs a zpk pipeline (buttap → lp2hp → bilinear → zpk2sos). Same
// filter, different arithmetic order, so the bar is floating-point noise
// rather than equality.
const coeffTol = 1e-12

func TestCrossoverMatchesScipy(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	if fx.CrossoverHz != CrossoverHz {
		t.Fatalf("crossover = %v, reference uses %v", CrossoverHz, fx.CrossoverHz)
	}

	for _, tc := range []struct {
		name     string
		want     [][6]float64
		highpass bool
	}{
		{"low", fx.CrossoverLow, false},
		{"high", fx.CrossoverHigh, true},
	} {
		got := lr4(CrossoverHz, float64(fx.SampleRate), tc.highpass)
		if len(got) != len(tc.want) {
			t.Errorf("%s: %d sections, scipy has %d", tc.name, len(got), len(tc.want))
			continue
		}
		for i, s := range got {
			// scipy rows are [b0 b1 b2 a0 a1 a2] with a0 == 1.
			w := tc.want[i]
			if w[3] != 1.0 {
				t.Errorf("%s section %d: reference a0 = %v, expected normalised", tc.name, i, w[3])
			}
			for j, pair := range []struct {
				name       string
				got, want_ float64
			}{
				{"b0", s.b0, w[0]}, {"b1", s.b1, w[1]}, {"b2", s.b2, w[2]},
				{"a1", s.a1, w[4]}, {"a2", s.a2, w[5]},
			} {
				if d := math.Abs(pair.got - pair.want_); d > coeffTol {
					t.Errorf("%s section %d %s (idx %d): %.17g vs scipy %.17g (diff %.3g)",
						tc.name, i, pair.name, j, pair.got, pair.want_, d)
				}
			}
		}
	}
}

// The guard's law constants come off stock hardware (#229). A port that
// quietly disagreed about them would be a different processor wearing the same
// name.
func TestGuardConstantsMatchReference(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	for _, tc := range []struct {
		name      string
		got, want float64
	}{
		{"ratio", bassRatio, fx.BassRatio},
		{"threshold dB", bassThresholdDB, fx.BassThresholdDB},
		{"release ms", bassReleaseMS, fx.BassReleaseMS},
		{"release reference dB", releaseReferenceDB, fx.ReleaseReferenceDB},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %v, reference uses %v", tc.name, tc.got, tc.want)
		}
	}
}

// LR4's defining property, and the reason the guard cannot colour a stream it
// is not compressing. Measured rather than asserted, as em_mbc does — the
// FIRST implementation of this crossover split subtractively, which is exact
// reconstruction by construction and looks obviously right, and produced a
// residual of 1.279 at 60Hz against an input of 1.0. Twenty dB of in-band
// reduction reached the output as 0.4dB.
func TestCrossoverSumsFlat(t *testing.T) {
	const fs = 48000.0
	worstDev := 0.0
	for _, f := range []float64{20, 40, 60, 80, 115, 150, 300, 1000, 5000, 15000} {
		lo := lr4(CrossoverHz, fs, false)
		hi := lr4(CrossoverHz, fs, true)

		// Feed a sine and measure |low + high| against the input, after
		// letting the filters settle.
		n := 48000
		var sumIn, sumOut float64
		lowBuf := make([]float64, n)
		highBuf := make([]float64, n)
		for i := 0; i < n; i++ {
			v := math.Sin(2 * math.Pi * f * float64(i) / fs)
			lowBuf[i], highBuf[i] = v, v
		}
		for i := range lo {
			lo[i].process(lowBuf)
		}
		for i := range hi {
			hi[i].process(highBuf)
		}
		// Skip the first half as settling time.
		for i := n / 2; i < n; i++ {
			v := math.Sin(2 * math.Pi * f * float64(i) / fs)
			sumIn += v * v
			s := lowBuf[i] + highBuf[i]
			sumOut += s * s
		}
		dev := 20 * math.Log10(math.Sqrt(sumOut/sumIn))
		if math.Abs(dev) > math.Abs(worstDev) {
			worstDev = dev
		}
		if math.Abs(dev) > 0.05 {
			t.Errorf("%.0fHz: |LP+HP| = %+.4f dB, want flat", f, dev)
		}
	}
	t.Logf("worst deviation across the sweep: %+.5f dB", worstDev)
}

// TestBassGuardAgainstReference replays the guard-only case. It is the only
// case with the guard attached and no limiter, so it is the one that isolates
// this stage.
func TestBassGuardAgainstReference(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}

	ran := 0
	for _, cs := range fx.Cases {
		if !cs.HasGuard || cs.HasLimiter {
			continue
		}
		ran++
		t.Run(cs.Name, func(t *testing.T) {
			first := cs.Steps[0].Params
			eq := NewEQ(fx.SampleRate, toF64(first.Bands), first.Loudness)
			guard := NewBassGuard(fx.SampleRate, float64(first.GuardDB), first.GuardEnabled)

			var worst fixture.Result
			haveWorst := false
			total := 0
			for i, st := range cs.Steps {
				eq.SetBands(toF64(st.Params.Bands), st.Params.Loudness)
				guard.SetParams(float64(st.Params.GuardDB), st.Params.GuardEnabled)

				buf := make([]float64, len(st.In))
				for j, v := range st.In {
					buf[j] = float64(v)
				}
				// Chain order is load-bearing: EQ, then guard, then limiter.
				eq.Process(buf)
				guard.Process(buf)

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
				total += r.N
			}
			if total == 0 {
				t.Fatal("compared 0 samples")
			}
			t.Logf("%d samples compared, worst step: %v; max reduction %.2f dB",
				total, worst, guard.MaxReductionDB())
			// The case exists to exercise the law. If the guard never
			// engaged, it passed without testing the thing it is named for.
			if guard.MaxReductionDB() <= 0 {
				t.Error("guard never engaged — the case tests only the crossover")
			}
		})
	}
	if ran == 0 {
		t.Fatal("no guard-only case in the fixture")
	}
}

// Bypassed must still filter and sum. Returning the input unchanged would step
// the phase at the toggle; the sum of LR4's halves is an allpass, not the
// identity.
func TestBypassedGuardIsNotAPassthrough(t *testing.T) {
	g := NewBassGuard(48000, DefaultBassGuardDB, false)
	n := 4096
	in := make([]float64, n)
	for i := range in {
		in[i] = 8000 * math.Sin(2*math.Pi*60*float64(i)/48000)
	}
	out := append([]float64(nil), in...)
	g.Process(out)

	same := true
	for i := range in {
		if math.Abs(in[i]-out[i]) > 1e-9 {
			same = false
			break
		}
	}
	if same {
		t.Error("bypassed guard returned its input unchanged — the crossover " +
			"sum is an allpass and must not be simplified away")
	}
}
