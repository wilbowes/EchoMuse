package outchain

import (
	"bytes"
	"encoding/binary"
	"math"
	"testing"
)

// Per-channel processing (#273). Every chain here is built with stock sound
// OFF and stereo permitted, as the speaker runs it with a plug in, unless the
// test is about exactly those two.

func stereoChain(p Params) *Chain {
	c := New(48000)
	c.SetActive(true)
	p.StockSound = false
	c.SetParams(p)
	c.SetStereo(true)
	return c
}

// shaped is a chain doing real work on every stage: boosted EQ (so the
// sections carry state), the bass guard engaged by a loud low band, and a
// limiter low enough to reduce.
func shaped() *Chain {
	p := DefaultParams()
	p.Bands = [NumBands]float64{9, 6, 0, -3, 0, 3, 6, 9}
	p.GuardEnabled, p.GuardDb = true, -12
	p.LimiterEnabled, p.LimiterThresholdDb = true, -6
	return stereoChain(p)
}

// interleave builds a stereo S16_LE period from two channel generators.
func interleave(frames, start int, l, r func(i int) float64) []byte {
	b := make([]byte, frames*4)
	for i := 0; i < frames; i++ {
		binary.LittleEndian.PutUint16(b[i*4:], uint16(int16(l(start+i))))
		binary.LittleEndian.PutUint16(b[i*4+2:], uint16(int16(r(start+i))))
	}
	return b
}

// interleaveFloat is interleave as floats, for ProcessFloat.
func interleaveFloat(frames, start int, l, r func(i int) float64) []float64 {
	b := make([]float64, frames*2)
	for i := 0; i < frames; i++ {
		b[i*2], b[i*2+1] = float64(int16(l(start+i))), float64(int16(r(start+i)))
	}
	return b
}

func tone(amp, hz float64) func(int) float64 {
	return func(i int) float64 { return amp * math.Sin(2*math.Pi*hz*float64(i)/48000) }
}

func silence(int) float64 { return 0 }

func sum(a, b func(int) float64) func(int) float64 {
	return func(i int) float64 { return a(i) + b(i) }
}

func channel(b []byte, ch int) []int16 {
	out := make([]int16, len(b)/4)
	for i := range out {
		out[i] = int16(binary.LittleEndian.Uint16(b[i*4+ch*2:]))
	}
	return out
}

func requireLEqualsR(t *testing.T, what string, b []byte) {
	t.Helper()
	l, r := channel(b, 0), channel(b, 1)
	for i := range l {
		if l[i] != r[i] {
			t.Fatalf("%s: frame %d L=%d R=%d", what, i, l[i], r[i])
		}
	}
}

func requireLEqualsRFloat(t *testing.T, what string, b []float64) {
	t.Helper()
	for i := 0; i+1 < len(b); i += 2 {
		if b[i] != b[i+1] {
			t.Fatalf("%s: frame %d L=%v R=%v", what, i/2, b[i], b[i+1])
		}
	}
}

// The per-channel path on dual-mono audio computes exactly what the mono
// path does, so a chain that goes stereo lazily, one that was stereo from
// the first sample, and one never permitted agree bit for bit; and the switch
// itself, mid-stream with every filter carrying state, is the same as having
// been stereo all along.
func TestStereoPathOnDualMonoIsBitExact(t *testing.T) {
	lazy, forced, never := shaped(), shaped(), shaped()
	never.SetStereo(false)
	// One silent period each, so activation's reset is behind them; then the
	// second goes stereo from zero state, which is what a split copies.
	for _, c := range []*Chain{lazy, forced, never} {
		c.Process(make([]byte, 2048*4))
	}
	forced.engage()

	bass := sum(tone(20000, 60), tone(6000, 3000))
	for p := 0; p < 6; p++ {
		a := interleave(2048, (p+1)*2048, bass, bass)
		b := append([]byte(nil), a...)
		n := append([]byte(nil), a...)
		lazy.Process(a)
		forced.Process(b)
		never.Process(n)
		if lazy.stereo || !forced.stereo {
			t.Fatalf("period %d: lazy stereo=%v forced stereo=%v", p, lazy.stereo, forced.stereo)
		}
		if !bytes.Equal(a, b) || !bytes.Equal(a, n) {
			t.Fatalf("period %d: the stereo path differs from the mono path on L == R", p)
		}
	}

	a := interleave(2048, 7*2048, bass, tone(9000, 1000))
	b := append([]byte(nil), a...)
	lazy.Process(a)
	forced.Process(b)
	if !lazy.stereo {
		t.Fatal("did not go stereo on the first permitted period with L != R")
	}
	if !bytes.Equal(a, b) {
		t.Fatal("going stereo mid-stream is not the same as having been stereo all along")
	}
}

// What is in one channel stays in that channel.
func TestStereoKeepsTheChannelsApart(t *testing.T) {
	c := stereoChain(DefaultParams())
	for p := 0; p < 4; p++ {
		buf := interleave(2048, p*2048, tone(8000, 440), silence)
		c.Process(buf)
		var left float64
		for i, s := range channel(buf, 1) {
			if s != 0 {
				t.Fatalf("period %d frame %d: the right channel carries %d of the left", p, i, s)
			}
		}
		for _, s := range channel(buf, 0) {
			left = math.Max(left, math.Abs(float64(s)))
		}
		if p > 0 && left < 7000 {
			t.Fatalf("period %d: the left came out at %.0f, want ~8000", p, left)
		}
	}
}

// One gain for both sides: a peak on the left pulls the right down by the
// same amount, so a limited passage does not shift towards the quiet side.
func TestLimiterIsLinked(t *testing.T) {
	p := DefaultParams()
	p.GuardEnabled = false
	p.LimiterEnabled, p.LimiterThresholdDb = true, -12
	c := stereoChain(p)
	var l, r []int16
	for k := 0; k < 4; k++ {
		buf := interleave(2048, k*2048, func(int) float64 { return 30000 }, func(int) float64 { return 3000 })
		c.Process(buf)
		l, r = channel(buf, 0), channel(buf, 1)
	}
	last := len(l) - 1
	if l[last] >= 30000 {
		t.Fatalf("the left was not limited: %d", l[last])
	}
	ratioL := float64(l[last]) / 30000
	ratioR := float64(r[last]) / 3000
	if math.Abs(ratioL-ratioR) > 0.002 {
		t.Errorf("unlinked: left at %.4f of input, right at %.4f", ratioL, ratioR)
	}
}

// The guard's gain follows the louder low band and is applied to both: with
// the right a tenth of the left, it comes out a tenth of the left, where a
// guard per channel would have reduced the quieter side less.
func TestBassGuardIsLinked(t *testing.T) {
	p := DefaultParams()
	p.GuardEnabled, p.GuardDb = true, -30
	p.LimiterEnabled = false
	c := stereoChain(p)
	src := tone(20000, 60)
	quiet := func(i int) float64 { return src(i) / 10 }
	var buf []byte
	for k := 0; k < 8; k++ {
		buf = interleave(2048, k*2048, src, quiet)
		c.Process(buf)
	}
	if st := c.TakeStats(); st.GuardReductionDb < 10 {
		t.Fatalf("the guard reduced only %.1fdB: the test is not exercising it", st.GuardReductionDb)
	}
	var el, er float64
	for i, s := range channel(buf, 0) {
		el += float64(s) * float64(s)
		er += float64(channel(buf, 1)[i]) * float64(channel(buf, 1)[i])
	}
	if ratio := math.Sqrt(er / el); math.Abs(ratio-0.1) > 0.002 {
		t.Errorf("right/left %.4f, want 0.1: the guard is not linked", ratio)
	}
}

// Silence resets the chain, and with it the stereo state: what follows is
// processed as mono again until it shows otherwise.
func TestStereoEndsWithTheResetOnSilence(t *testing.T) {
	c := shaped()
	c.Process(interleave(2048, 0, tone(9000, 440), silence))
	if !c.stereo {
		t.Fatal("not stereo")
	}
	for n := 0; !c.Idle(); n++ {
		if n > 200 {
			t.Fatal("never went idle on silence")
		}
		c.Process(make([]byte, 2048*4))
	}
	if c.stereo {
		t.Error("still stereo after the reset")
	}
}

// Not permitted, an ACTIVE chain writes L == R whatever comes in, through
// every entry point, including one that was stereo the period before; and
// ProcessWide is mono even when stereo is permitted.
func TestStereoNotPermittedAlwaysWritesLEqualsR(t *testing.T) {
	l, r := sum(tone(12000, 440), tone(3000, 80)), tone(-9000, 1000)

	c := shaped()
	c.SetStereo(false)
	for k := 0; k < 3; k++ {
		buf := interleave(2048, k*2048, l, r)
		c.Process(buf)
		requireLEqualsR(t, "Process", buf)
		fb := interleaveFloat(2048, k*2048, l, r)
		scales := make([]float64, 2048)
		for i := range scales {
			scales[i] = 2
		}
		c.ProcessFloat(fb, scales)
		requireLEqualsRFloat(t, "ProcessFloat", fb)
		out := make([]byte, 2048*4)
		c.ProcessWide(interleaveFloat(2048, k*2048, l, r), out)
		requireLEqualsR(t, "ProcessWide", out)
		if c.stereo {
			t.Fatal("went stereo without permission")
		}
	}

	// Permitted and stereo, then withdrawn: the very next period is L == R.
	c = shaped()
	c.Process(interleave(2048, 0, l, r))
	if !c.stereo {
		t.Fatal("not stereo with permission")
	}
	c.SetStereo(false)
	buf := interleave(2048, 2048, l, r)
	c.Process(buf)
	requireLEqualsR(t, "Process after the permission went", buf)

	// The same through ProcessFloat, under a boost.
	c = shaped()
	c.ProcessFloat(interleaveFloat(2048, 0, l, r), boostScales(2048, 0))
	if !c.stereo {
		t.Fatal("ProcessFloat: not stereo with permission")
	}
	c.SetStereo(false)
	fb := interleaveFloat(2048, 2048, l, r)
	c.ProcessFloat(fb, boostScales(2048, 1))
	requireLEqualsRFloat(t, "ProcessFloat after the permission went", fb)

	// ProcessWide has no per-channel path.
	c = shaped()
	out := make([]byte, 2048*4)
	c.ProcessWide(interleaveFloat(2048, 0, l, r), out)
	requireLEqualsR(t, "ProcessWide, permitted", out)
}

// Stock sound is the internal driver's voicing, and its order is mono: a
// permission to go stereo does not reach it.
func TestStockSoundIgnoresStereo(t *testing.T) {
	c := New(48000)
	c.SetActive(true)
	p := DefaultParams()
	p.StockSound = true
	c.SetParams(p)
	c.SetStereo(true)
	l, r := tone(12000, 440), tone(-9000, 1000)
	for k := 0; k < 3; k++ {
		buf := interleave(2048, k*2048, l, r)
		c.Process(buf)
		requireLEqualsR(t, "Process", buf)
		fb := interleaveFloat(2048, k*2048, l, r)
		c.ProcessFloat(fb, nil)
		requireLEqualsRFloat(t, "ProcessFloat", fb)
		if c.stereo || c.guard.upper == nil {
			t.Fatalf("stereo=%v upper bands=%v: not the stock-sound chain", c.stereo, c.guard.upper != nil)
		}
	}
}

// Withdrawing the permission mid-stream folds each stage's two states to
// their mean, so the first mono sample carries on from the last stereo frame
// as the music itself does, with no step in it.
func TestPermissionDropMergesWithoutAStep(t *testing.T) {
	c := shaped()
	l := sum(tone(12000, 220), tone(4000, 90))
	r := sum(tone(6000, 220), tone(3000, 1500))
	var prev []byte
	for k := 0; k < 4; k++ {
		prev = interleave(2048, k*2048, l, r)
		c.Process(prev)
	}
	if !c.stereo {
		t.Fatal("not stereo")
	}
	c.SetStereo(false)
	next := interleave(2048, 4*2048, l, r)
	c.Process(next)
	if c.stereo {
		t.Fatal("still stereo after the permission went")
	}
	requireLEqualsR(t, "after the drop", next)

	pl, pr := channel(prev, 0), channel(prev, 1)
	avg := func(i int) float64 { return (float64(pl[i]) + float64(pr[i])) / 2 }
	var slope float64
	for i := 1; i < len(pl); i++ {
		slope = math.Max(slope, math.Abs(avg(i)-avg(i-1)))
	}
	step := math.Abs(float64(channel(next, 0)[0]) - avg(len(pl)-1))
	if step > 2*slope+2 {
		t.Errorf("a %.0f step where the music moves at most %.0f a sample", step, slope)
	}
	// The boundary frame leaves the limiter's look-ahead delay, so it shows
	// the limiter's merge alone. What the EQ's and the guard's merges did
	// comes out a delay line later: hold the whole period to the same bound.
	nl := channel(next, 0)
	for i := 1; i < len(nl); i++ {
		if d := math.Abs(float64(nl[i]) - float64(nl[i-1])); d > 2*slope+2 {
			t.Fatalf("frame %d after the drop: a %.0f step where the music moves at most %.0f a sample", i, d, slope)
		}
	}
}

// Parameters that change while the chain is per channel reach both sides:
// on dual-mono audio a chain forced per channel stays the mono chain bit for
// bit through a band change, a loudness toggle, flat and back.
func TestSetParamsWhileStereoKeepsDualMonoExact(t *testing.T) {
	mono, forced := shaped(), shaped()
	mono.SetStereo(false)
	mono.Process(make([]byte, 2048*4))
	forced.Process(make([]byte, 2048*4))
	forced.engage()

	base := DefaultParams()
	base.StockSound = false
	base.GuardEnabled, base.GuardDb = true, -12
	base.LimiterEnabled, base.LimiterThresholdDb = true, -6
	bands := base
	bands.Bands = [NumBands]float64{-6, 3, 0, 6, 0, -3, 9, 0}
	loud := bands
	loud.Loudness = !loud.Loudness
	flat := base
	thresh := loud
	thresh.LimiterThresholdDb = -3
	changes := map[int]Params{2: bands, 4: loud, 6: flat, 8: thresh}

	bass := sum(tone(20000, 60), tone(6000, 3000))
	for k := 0; k < 10; k++ {
		if p, ok := changes[k]; ok {
			mono.SetParams(p)
			forced.SetParams(p)
		}
		a := interleave(2048, (k+1)*2048, bass, bass)
		b := append([]byte(nil), a...)
		mono.Process(a)
		forced.Process(b)
		if !forced.stereo {
			t.Fatalf("period %d: no longer per channel", k)
		}
		if !bytes.Equal(a, b) {
			t.Fatalf("period %d: per channel differs from mono after a parameter change", k)
		}
	}
}

// The clip counts (#275) are per frame on the per-channel path, so on
// dual-mono audio a stereo chain reports what the mono path reports, with
// the limiter bypassed (the backstop doing its job on a boosted EQ) and on.
func TestStereoClipCountsMatchTheMonoPathOnDualMono(t *testing.T) {
	for _, limiting := range []bool{false, true} {
		p := DefaultParams()
		p.Bands = [NumBands]float64{12, 12, 12, 12, 12, 12, 12, 12}
		p.LimiterEnabled = limiting
		mono, st := stereoChain(p), stereoChain(p)
		mono.SetStereo(false)
		mono.Process(make([]byte, 2048*4))
		st.Process(make([]byte, 2048*4))
		st.engage()
		loud := tone(30000, 1000)
		for k := 1; k < 5; k++ {
			mono.Process(interleave(2048, k*2048, loud, loud))
			st.Process(interleave(2048, k*2048, loud, loud))
		}
		if !st.stereo {
			t.Fatal("not on the per-channel path")
		}
		ms, ss := mono.TakeStats(), st.TakeStats()
		if !limiting && ms.ClippedBypassed == 0 {
			t.Fatal("bypassed: nothing clipped, so the test proves nothing")
		}
		if ms != ss {
			t.Errorf("limiting=%v: mono %+v, stereo %+v", limiting, ms, ss)
		}
	}
}

// Going per channel allocates nothing on the ALSA goroutine: the right EQ's
// sections are already there, designed with the left's.
func TestEngageDoesNotAllocate(t *testing.T) {
	c := shaped()
	c.Process(make([]byte, 2048*4))
	if allocs := testing.AllocsPerRun(20, func() {
		c.engage()
		c.merge()
	}); allocs != 0 {
		t.Errorf("%.0f allocations per engage", allocs)
	}
}

// ProcessFloat at unity is Process in float: per channel too.
func TestProcessFloatStereoMatchesProcessAtUnity(t *testing.T) {
	a, b := shaped(), shaped()
	l := sum(tone(20000, 60), tone(5000, 2500))
	r := tone(-15000, 700)
	for k := 0; k < 6; k++ {
		buf := interleave(2048, k*2048, l, r)
		fb := interleaveFloat(2048, k*2048, l, r)
		ones := make([]float64, 2048)
		for i := range ones {
			ones[i] = 1
		}
		a.Process(buf)
		b.ProcessFloat(fb, ones)
		if !a.stereo || !b.stereo {
			t.Fatalf("period %d: Process stereo=%v ProcessFloat stereo=%v", k, a.stereo, b.stereo)
		}
		for i := 0; i < 2048*2; i++ {
			got := int16(fb[i])
			want := int16(binary.LittleEndian.Uint16(buf[i*2:]))
			if got != want {
				t.Fatalf("period %d sample %d: ProcessFloat %d, Process %d", k, i, got, want)
			}
		}
	}
}

// boostScales is a response boost that moves within the period and between
// periods, the multipliers ProcessFloat is given for a reply above the music.
func boostScales(frames, period int) []float64 {
	s := make([]float64, frames)
	for i := range s {
		s[i] = 1 + 0.75*float64((i+period*37)%frames)/float64(frames)
	}
	return s
}

// A boosted reply over stereo music (ProcessFloat with multipliers above 1)
// keeps the sides apart: what is only in the left stays out of the right.
func TestProcessFloatBoostedKeepsTheChannelsApart(t *testing.T) {
	c := shaped()
	l := sum(tone(20000, 60), tone(5000, 2500))
	var peak float64
	for k := 0; k < 6; k++ {
		fb := interleaveFloat(2048, k*2048, l, silence)
		c.ProcessFloat(fb, boostScales(2048, k))
		if !c.stereo {
			t.Fatalf("period %d: not per channel on L != R", k)
		}
		for i := 0; i < 2048; i++ {
			if fb[i*2+1] != 0 {
				t.Fatalf("period %d frame %d: right=%v, want silence", k, i, fb[i*2+1])
			}
			peak = math.Max(peak, math.Abs(fb[i*2]))
		}
	}
	if peak < 1000 {
		t.Fatalf("left peak %v: the left channel did not come through", peak)
	}
}

// Under the same boost, dual-mono audio through the per-channel path is the
// mono path bit for bit, whether the chain never split, was split from the
// start, or was never permitted.
func TestProcessFloatBoostedDualMonoIsBitExact(t *testing.T) {
	lazy, forced, never := shaped(), shaped(), shaped()
	never.SetStereo(false)
	for _, c := range []*Chain{lazy, forced, never} {
		c.ProcessFloat(make([]float64, 2048*2), nil)
	}
	forced.engage()

	bass := sum(tone(20000, 60), tone(6000, 3000))
	for k := 0; k < 6; k++ {
		a := interleaveFloat(2048, (k+1)*2048, bass, bass)
		b := append([]float64(nil), a...)
		n := append([]float64(nil), a...)
		s := boostScales(2048, k)
		lazy.ProcessFloat(a, s)
		forced.ProcessFloat(b, s)
		never.ProcessFloat(n, s)
		if lazy.stereo || !forced.stereo {
			t.Fatalf("period %d: lazy stereo=%v forced stereo=%v", k, lazy.stereo, forced.stereo)
		}
		for i := range a {
			if a[i] != b[i] || a[i] != n[i] {
				t.Fatalf("period %d sample %d: lazy %v, per channel %v, mono %v", k, i, a[i], b[i], n[i])
			}
		}
	}
}
