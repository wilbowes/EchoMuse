package outchain

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain/fixture"
)

func TestLimiterGeometryMatchesReference(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	if DefaultLookaheadMS != fx.LookaheadMS {
		t.Errorf("lookahead = %v ms, reference uses %v", DefaultLookaheadMS, fx.LookaheadMS)
	}
	l := NewLimiter(fx.SampleRate, -1.0, 150, true)
	// The tail length IS the stream's added latency, and the fixture records
	// it independently as the flush tail — so these must agree or the port
	// has a different latency from the reference.
	wantTail := l.lookahead - 1
	for _, cs := range fx.Cases {
		if !cs.HasLimiter {
			continue
		}
		if len(cs.Tail) != wantTail {
			t.Errorf("%s: reference flush tail %d samples, port holds %d",
				cs.Name, len(cs.Tail), wantTail)
		}
	}
}

// TestLimiterAgainstReference replays every case with a limiter attached,
// building whichever stages that case attached and running them in the chain's
// order: EQ, then guard, then limiter.
func TestLimiterAgainstReference(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}

	ran := 0
	for _, cs := range fx.Cases {
		if !cs.HasLimiter {
			continue
		}
		ran++
		t.Run(cs.Name, func(t *testing.T) {
			first := cs.Steps[0].Params
			eq := NewEQ(fx.SampleRate, toF64(first.Bands), first.Loudness)
			lim := NewLimiter(fx.SampleRate,
				float64(first.LimiterThresholdDB),
				float64(first.LimiterReleaseMS),
				first.LimiterEnabled)
			var guard *BassGuard
			if cs.HasGuard {
				guard = NewBassGuard(fx.SampleRate,
					float64(first.GuardDB), first.GuardEnabled)
			}

			var worst fixture.Result
			haveWorst := false
			total := 0
			for i, st := range cs.Steps {
				eq.SetBands(toF64(st.Params.Bands), st.Params.Loudness)
				lim.SetParams(float64(st.Params.LimiterThresholdDB),
					float64(st.Params.LimiterReleaseMS),
					st.Params.LimiterEnabled)
				if guard != nil {
					guard.SetParams(float64(st.Params.GuardDB), st.Params.GuardEnabled)
				}

				buf := make([]float64, len(st.In))
				for j, v := range st.In {
					buf[j] = float64(v)
				}
				eq.Process(buf)
				if guard != nil {
					guard.Process(buf)
				}
				emitted := lim.Process(buf)

				got := make([]int16, len(emitted))
				for j, v := range emitted {
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

			// The flush tail is part of the stream, not an afterthought: skip
			// it and the last 5ms of every response is silently dropped.
			tail := lim.Flush()
			gotTail := make([]int16, len(tail))
			for j, v := range tail {
				gotTail[j] = clamp16(v)
			}
			r, err := fixture.Compare(gotTail, cs.Tail)
			if err != nil {
				t.Fatalf("flush: %v", err)
			}
			if !r.OK() {
				t.Errorf("flush tail disagrees: %v", r)
			}
			total += r.N

			if total == 0 {
				t.Fatal("compared 0 samples")
			}
			t.Logf("%d samples compared, worst step: %v; max reduction %.2f dB, clipped %d",
				total, worst, lim.MaxReductionDB, lim.Clipped)

			// A limiter that was enabled throughout must never let anything
			// past its own ceiling — that is the module's entire reason to
			// exist, so it is asserted rather than reported.
			//
			// Gated on the limiter having been enabled for EVERY step,
			// because a bypassed limiter is supposed to do nothing: a boosted
			// EQ ahead of it legitimately pushes samples past the ceiling for
			// the final clip to catch. Python does exactly the same, and this
			// assertion caught the reference's docstring overstating its own
			// instrument rather than catching a port bug.
			if alwaysLimiting(cs) && lim.Clipped != 0 {
				t.Errorf("limiter emitted %d samples above the ceiling while enabled",
					lim.Clipped)
			}
		})
	}
	if ran == 0 {
		t.Fatal("no limiter cases in the fixture")
	}
}

func alwaysLimiting(cs fixture.Case) bool {
	for _, st := range cs.Steps {
		if !st.Params.LimiterEnabled {
			return false
		}
	}
	return true
}

// The bypassed limiter's clip count is not a fault, and this pins the
// behaviour so a later "fix" to the counter is a deliberate choice rather than
// an accident. It is also the evidence for the docstring correction.
func TestBypassedLimiterCanLegitimatelyClip(t *testing.T) {
	l := NewLimiter(48000, -1.0, 150, false)
	n := 4096
	in := make([]float64, n)
	for i := range in {
		in[i] = 40000 * math.Sin(float64(i)*0.01) // beyond the ceiling
	}
	l.Process(in)
	if l.Clipped == 0 {
		t.Fatal("test premise wrong: nothing exceeded the ceiling")
	}
	t.Logf("bypassed limiter counted %d clipped samples — expected, not a bug",
		l.Clipped)
}

// The hot cases must actually drive the limiter, or they pass while testing
// only its bypass path.
func TestLimiterEngagesOnHotMaterial(t *testing.T) {
	fx, err := fixture.Load(fixturePath)
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	for _, cs := range fx.Cases {
		if cs.Name != "limiter_hard" {
			continue
		}
		first := cs.Steps[0].Params
		eq := NewEQ(fx.SampleRate, toF64(first.Bands), first.Loudness)
		lim := NewLimiter(fx.SampleRate,
			float64(first.LimiterThresholdDB),
			float64(first.LimiterReleaseMS), true)
		for _, st := range cs.Steps {
			buf := make([]float64, len(st.In))
			for j, v := range st.In {
				buf[j] = float64(v)
			}
			eq.Process(buf)
			lim.Process(buf)
		}
		if lim.MaxReductionDB < 1.0 {
			t.Errorf("limiter_hard produced only %.2f dB of reduction — "+
				"the case is not exercising the gain law", lim.MaxReductionDB)
		}
		return
	}
	t.Fatal("limiter_hard case missing")
}

// A bypassed limiter keeps the stream's latency. Dropping the delay on toggle
// would shift the audio by 5ms, which is a click.
func TestBypassedLimiterKeepsLatency(t *testing.T) {
	l := NewLimiter(48000, -1.0, 150, false)
	in := make([]float64, 2048)
	for i := range in {
		in[i] = 1000 * math.Sin(float64(i)*0.01)
	}
	out := l.Process(in)
	if len(out) != len(in) {
		t.Fatalf("emitted %d samples for %d in — a bypassed limiter must stay 1:1",
			len(out), len(in))
	}
	if len(l.tail) != l.lookahead-1 {
		t.Errorf("tail = %d samples, want %d — the delay was dropped",
			len(l.tail), l.lookahead-1)
	}
	// And the samples are delayed, not passed straight through.
	if out[l.lookahead] == in[l.lookahead] && out[0] == in[0] {
		t.Error("bypassed limiter emitted its input undelayed")
	}
}

// The threshold is taken against 32767, not 32768. A 0dBFS threshold against
// 32768 produces a sample that wraps to full-scale negative on the int16 cast
// — the worst artefact available, and precisely what this module prevents.
func TestThresholdIsTakenAgainstTheAsymmetricCeiling(t *testing.T) {
	l := NewLimiter(48000, 0.0, 150, true)
	if l.thresh != ceiling {
		t.Errorf("0dBFS threshold = %v, want %v (32767, not 32768)", l.thresh, ceiling)
	}
	if ceiling != 32767.0 {
		t.Fatalf("ceiling = %v, want 32767", ceiling)
	}

	// Drive it hard and check nothing survives above the ceiling.
	n := 8192
	in := make([]float64, n)
	for i := range in {
		in[i] = 32760 * math.Sin(float64(i)*0.02)
	}
	out := l.Process(in)
	for i, v := range out {
		if math.Abs(v) > ceiling {
			t.Fatalf("sample %d = %v exceeds the ceiling", i, v)
		}
	}
}

// A threshold above 0dBFS would ask the limiter to permit clipping.
func TestThresholdCannotBeSetAboveUnity(t *testing.T) {
	l := NewLimiter(48000, 6.0, 150, true)
	if l.thresholdDB != 0.0 {
		t.Errorf("threshold clamped to %v, want 0", l.thresholdDB)
	}
}
