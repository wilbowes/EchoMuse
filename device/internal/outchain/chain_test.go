package outchain

import (
	"math"
	"testing"
)

const testRate = 48000

func sine(n int, amp, freq float64) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = amp * math.Sin(2*math.Pi*freq*float64(i)/testRate)
	}
	return out
}

func flatBands() []float64 { return make([]float64, NumBands) }

func boostBands(db float64) []float64 {
	b := make([]float64, NumBands)
	for i := range b {
		b[i] = db
	}
	return b
}

// worstStep is the largest sample-to-sample jump in a signal. A click IS a
// discontinuity, so this is the measurement that decides whether one happened
// — not a listening test, and not an eyeball on a plot.
func worstStep(x []float64) (float64, int) {
	worst, at := 0.0, 0
	for i := 1; i < len(x); i++ {
		if d := math.Abs(x[i] - x[i-1]); d > worst {
			worst, at = d, i
		}
	}
	return worst, at
}

// THE TESTS THIS WHOLE FILE EXISTS FOR (R4, section 8.3).
//
// FIRST, WHAT ACTUALLY CLICKS — measured 2026-08-22, because the answer was
// not what I assumed and half the obvious probes detect nothing:
//
//	signal        change                       raw step   fade step   peak
//	60Hz @20000   loudness on (state reset)      53351        6144    72863
//	60Hz @20000   all bands 0 -> +12             61469        7759   113938
//	60Hz @20000   low shelf +12 -> -12            1065         578    80607
//	1kHz @8000    any of the above              at or below the signal's own slope
//
// Two things follow. A parameter change is only audible as a discontinuity
// when the FILTER STATE holds real energy — a low-frequency signal at level —
// and it is worst where the reference DROPS that state, which it does on a
// section-count change (toggling loudness moves 8 sections to 9). At 53351
// against a peak of 72863 that is a step of 73% of the signal, which is not a
// subtle artefact.
//
// It also means the state-reset quirk this port deliberately reproduces from
// em_eq (see EQ.SetBands) is the single worst offender, and the crossfade is
// exactly what makes reproducing it safe.
//
// A 1kHz tone detects none of this, which is why the first version of these
// tests passed while measuring nothing.

// clickProbe is the signal and transition that reveal the artefact: enough
// low-frequency energy to load the biquad states, and a change that discards
// them.
func clickProbe() (src []float64, from, to Params, chunk, at int) {
	chunk, at = 2048, 6
	src = sine(chunk*12, 20000, 60)
	bands := make([]float64, NumBands)
	bands[0] = 12
	from = Params{Bands: bands}
	to = Params{Bands: bands, Loudness: true} // 8 -> 9 sections: state reset
	return
}

func runSource(src []float64, chunk int, step func(i int), proc func([]float64) []float64) []float64 {
	var out []float64
	for i := 0; i*chunk < len(src); i++ {
		step(i)
		buf := append([]float64(nil), src[i*chunk:min((i+1)*chunk, len(src))]...)
		out = append(out, proc(buf)...)
	}
	return out
}

// The premise, asserted rather than assumed: without smoothing, this change
// IS a click. If the stages ever stop producing one, the crossfade has stopped
// being necessary and should be deleted rather than kept for appearances.
func TestStateResetClicksWithoutTheCrossfade(t *testing.T) {
	src, from, to, chunk, at := clickProbe()
	raw := newStages(testRate, from)
	out := runSource(src, chunk, func(i int) {
		if i == at {
			raw.eq.SetBands(to.Bands, to.Loudness)
		}
	}, raw.process)

	lo, hi := chunk*(at-1), chunk*(at+3)
	step, where := worstStep(out[lo:min(hi, len(out))])
	peak := 0.0
	for _, v := range out[lo:min(hi, len(out))] {
		peak = math.Max(peak, math.Abs(v))
	}
	t.Logf("raw swap: worst step %.0f at %d, peak %.0f (%.0f%% of peak)",
		step, where+lo, peak, step/peak*100)
	if step < peak*0.4 {
		t.Errorf("raw coefficient swap produced only %.0f against a peak of %.0f — "+
			"the premise for the crossfade no longer holds", step, peak)
	}
}

// And the crossfade removes it.
func TestCrossfadeRemovesTheClick(t *testing.T) {
	src, from, to, chunk, at := clickProbe()

	raw := newStages(testRate, from)
	rawOut := runSource(src, chunk, func(i int) {
		if i == at {
			raw.eq.SetBands(to.Bands, to.Loudness)
		}
	}, raw.process)

	c := NewChain(testRate, from)
	fadeOut := runSource(src, chunk, func(i int) {
		if i == at {
			c.SetParams(to)
		}
	}, c.Process)

	lo, hi := chunk*(at-1), chunk*(at+3)
	rawStep, _ := worstStep(rawOut[lo:min(hi, len(rawOut))])
	fadeStep, fadeAt := worstStep(fadeOut[lo:min(hi, len(fadeOut))])
	peak := 0.0
	for _, v := range fadeOut[lo:min(hi, len(fadeOut))] {
		peak = math.Max(peak, math.Abs(v))
	}
	t.Logf("raw %.0f -> crossfaded %.0f at %d (%.1fx better, %.1f%% of peak %.0f)",
		rawStep, fadeStep, fadeAt+lo, rawStep/fadeStep, fadeStep/peak*100, peak)

	// Measured 8.7x at the time of writing. Four is a floor with room for
	// arithmetic to move without the test becoming a tripwire.
	if rawStep/fadeStep < 4 {
		t.Errorf("crossfade only %.1fx better than a raw swap (%.0f vs %.0f)",
			rawStep/fadeStep, fadeStep, rawStep)
	}
	// And in absolute terms it must be a small fraction of the signal.
	if fadeStep > peak*0.15 {
		t.Errorf("crossfaded step %.0f is %.0f%% of the %.0f peak — still audible",
			fadeStep, fadeStep/peak*100, peak)
	}
}

// A linear fade holds the level for CORRELATED sources; equal-power would
// bulge by up to 3dB in the middle. Measured in a settled window around the
// transition against an equally long settled window before it — the FIRST
// version of this test compared against the input amplitude and failed on the
// filters' cold-start transient, which has nothing to do with the fade.
func TestFadeDoesNotBulgeTheLevel(t *testing.T) {
	const (
		amp   = 8000.0
		freq  = 1000.0
		chunk = 512
		at    = 20
	)
	src := sine(chunk*40, amp, freq)
	// Audibly identical either side: only the release time moves, and the
	// limiter is off. Any level change through the fade is the fade's own.
	a := Params{Bands: flatBands(), LimiterReleaseMS: 150}
	b := Params{Bands: flatBands(), LimiterReleaseMS: 151}

	c := NewChain(testRate, a)
	out := runSource(src, chunk, func(i int) {
		if i == at {
			c.SetParams(b)
		}
	}, c.Process)

	peakIn := func(lo, hi int) float64 {
		p := 0.0
		for _, v := range out[lo:min(hi, len(out))] {
			p = math.Max(p, math.Abs(v))
		}
		return p
	}
	before := peakIn(chunk*(at-4), chunk*at)
	during := peakIn(chunk*at, chunk*at+c.fadeLen+chunk)

	t.Logf("peak before %.1f, through the fade %.1f (%.2fx)", before, during, during/before)
	if during > before*1.05 {
		t.Errorf("level rose %.2fx through the fade — an equal-power crossfade "+
			"does exactly this on correlated sources", during/before)
	}
}

// A clone must carry filter STATE, not just configuration. A cold clone fades
// in a startup transient, which is the artefact the crossfade exists to avoid,
// so this asserts the mechanism rather than the outcome.
func TestCloneCarriesFilterState(t *testing.T) {
	p := Params{Bands: boostBands(6), LimiterEnabled: true,
		LimiterThresholdDB: -3, LimiterReleaseMS: 120,
		GuardEnabled: true, GuardDB: -30}
	s := newStages(testRate, p)

	// Warm it up.
	buf := sine(4096, 12000, 60)
	s.process(buf)

	c := s.clone(p)
	if c.eq.sections[0].z0 != s.eq.sections[0].z0 || c.eq.sections[0].z1 != s.eq.sections[0].z1 {
		t.Error("EQ biquad state not carried into the clone")
	}
	if c.guard.bass.gainDB != s.guard.bass.gainDB {
		t.Error("bass guard gain state not carried into the clone")
	}
	if c.lim.gainDB != s.lim.gainDB {
		t.Error("limiter gain state not carried into the clone")
	}
	if len(c.lim.tail) != len(s.lim.tail) {
		t.Fatalf("limiter tail length %d, want %d", len(c.lim.tail), len(s.lim.tail))
	}
	for i := range c.lim.tail {
		if c.lim.tail[i] != s.lim.tail[i] {
			t.Errorf("limiter tail differs at %d", i)
			break
		}
	}
	// And it must be a COPY, not an alias — otherwise the two paths write
	// each other's state and the crossfade blends a path with itself.
	c.lim.tail[0] = 12345
	if s.lim.tail[0] == 12345 {
		t.Error("limiter tail is aliased between clone and original")
	}
	c.eq.sections[0].z0 = 999
	if s.eq.sections[0].z0 == 999 {
		t.Error("EQ sections are aliased between clone and original")
	}
	c.guard.bass.gainDB = -99
	if s.guard.bass.gainDB == -99 {
		t.Error("bass guard band state is aliased between clone and original")
	}
}

// Dragging a slider produces a stream of changes. The current fade must run to
// completion and the newest request applied after it — restarting on every
// change means the fade never finishes, and snapping to each reintroduces the
// step the fade exists to remove.
func TestRapidChangesQueueTheLatest(t *testing.T) {
	c := NewChain(testRate, Params{Bands: flatBands()})
	c.SetParams(Params{Bands: boostBands(3)})
	if !c.Fading() {
		t.Fatal("first change did not start a fade")
	}
	// Three more while it runs; only the last should survive.
	c.SetParams(Params{Bands: boostBands(4)})
	c.SetParams(Params{Bands: boostBands(5)})
	c.SetParams(Params{Bands: boostBands(6)})

	// Drive past the end of the first fade plus the queued one.
	for i := 0; i < 20; i++ {
		buf := sine(512, 4000, 440)
		c.Process(buf)
	}
	if c.Fading() {
		t.Error("still fading after ample time — changes are restarting the fade")
	}
	if got := c.curP.Bands[0]; got != 6 {
		t.Errorf("settled on %v dB, want 6 (the latest request)", got)
	}
}

// A redundant push must cost a comparison, not a crossfade. The controller
// re-sends the whole config on every change and on every reconnect.
func TestIdenticalParamsDoNotFade(t *testing.T) {
	p := Params{Bands: boostBands(3), LimiterEnabled: true,
		LimiterThresholdDB: -1, LimiterReleaseMS: 150}
	c := NewChain(testRate, p)
	c.SetParams(p)
	if c.Fading() {
		t.Error("an identical parameter set started a fade")
	}
	// A copy with its own slice must also compare equal.
	q := clonedParams(p)
	c.SetParams(q)
	if c.Fading() {
		t.Error("an equal-but-not-aliased parameter set started a fade")
	}
}

// The chain must stay 1:1 in steady state and through a fade — the limiter's
// latency is absorbed by its tail, and a fade must not change the framing.
func TestChainIsSampleCountPreserving(t *testing.T) {
	c := NewChain(testRate, Params{Bands: flatBands(), LimiterEnabled: true,
		LimiterThresholdDB: -1, LimiterReleaseMS: 150})
	for i := 0; i < 10; i++ {
		if i == 3 {
			c.SetParams(Params{Bands: boostBands(6), LimiterEnabled: true,
				LimiterThresholdDB: -6, LimiterReleaseMS: 150})
		}
		in := sine(2048, 8000, 440)
		out := c.Process(in)
		if len(out) != 2048 {
			t.Fatalf("chunk %d: emitted %d samples for 2048 in (fading=%v)",
				i, len(out), c.Fading())
		}
	}
}

// Params.Equal decides whether anything happens at all, so its failure mode is
// silence: a field it forgets is a control that does nothing.
func TestParamsEqualCoversEveryField(t *testing.T) {
	base := Params{Bands: boostBands(2), Loudness: false,
		LimiterEnabled: true, LimiterThresholdDB: -3, LimiterReleaseMS: 120,
		GuardEnabled: true, GuardDB: -30}

	for _, tc := range []struct {
		name string
		mut  func(p *Params)
	}{
		{"bands", func(p *Params) { p.Bands = boostBands(3) }},
		{"loudness", func(p *Params) { p.Loudness = true }},
		{"limiter enabled", func(p *Params) { p.LimiterEnabled = false }},
		{"limiter threshold", func(p *Params) { p.LimiterThresholdDB = -6 }},
		{"limiter release", func(p *Params) { p.LimiterReleaseMS = 200 }},
		{"guard enabled", func(p *Params) { p.GuardEnabled = false }},
		{"guard db", func(p *Params) { p.GuardDB = -20 }},
	} {
		q := clonedParams(base)
		tc.mut(&q)
		if base.Equal(q) {
			t.Errorf("Params.Equal ignores %s — changing it would do nothing", tc.name)
		}
	}
	if !base.Equal(clonedParams(base)) {
		t.Error("Params.Equal says an identical copy differs")
	}
}
