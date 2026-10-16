package outchain

import (
	"math"
	"math/rand"
	"testing"
)

// naiveFilter is the causal FIR filtering of x by h — y[n] = sum_k h[k]*x[n-k]
// with x treated as zero before index 0 — computed with no FFT at all, as
// an independent ground truth.
func naiveFilter(x, h []float64) []float64 {
	y := make([]float64, len(x))
	for n := range x {
		var s float64
		for k := 0; k < len(h) && k <= n; k++ {
			s += h[k] * x[n-k]
		}
		y[n] = s
	}
	return y
}

func randomSlice(rng *rand.Rand, n int, scale float64) []float64 {
	s := make([]float64, n)
	for i := range s {
		s[i] = rng.NormFloat64() * scale
	}
	return s
}

// The core correctness test: streaming eqFIR over many fixed-size periods
// must match ONE naive convolution of the whole concatenated signal — this
// is the test that would catch an overlap bookkeeping bug, since a wrong
// carry only shows up across a call boundary.
func TestEQFIRMatchesNaiveFilterAcrossManyPeriods(t *testing.T) {
	cases := []struct {
		name       string
		filterLen  int
		period     int
		numPeriods int
	}{
		{"filter_smaller_than_period", 17, 64, 20},
		{"filter_equal_to_period", 64, 64, 20},
		{"filter_larger_than_period", 2048, 256, 10},
		{"radar_shape", 2048, 2048, 8}, // the actual production sizing
		{"single_tap_period_one", 1, 1, 50},
		{"filter_len_two", 2, 1, 30},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rng := rand.New(rand.NewSource(42))
			h := randomSlice(rng, c.filterLen, 0.1)
			x := randomSlice(rng, c.period*c.numPeriods, 1.0)

			want := naiveFilter(x, h)

			fir := newEQFIR(h, c.period)
			got := make([]float64, 0, len(x))
			for i := 0; i < c.numPeriods; i++ {
				chunk := x[i*c.period : (i+1)*c.period]
				got = append(got, fir.process(chunk)...)
			}

			for i := range want {
				if math.Abs(got[i]-want[i]) > 1e-8 {
					t.Fatalf("%s: sample %d (period %d): got %v want %v",
						c.name, i, i/c.period, got[i], want[i])
				}
			}
		})
	}
}

func TestEQFIRReturnsNilForEmptyTaps(t *testing.T) {
	if newEQFIR(nil, 2048) != nil {
		t.Fatal("newEQFIR(nil, ...) must return nil, not a FIR that would panic on use")
	}
	if newEQFIR([]float64{}, 2048) != nil {
		t.Fatal("newEQFIR([]float64{}, ...) must return nil")
	}
}

func TestEQFIRPanicsOnWrongLength(t *testing.T) {
	fir := newEQFIR([]float64{1, 0, 0}, 8)
	defer func() {
		if recover() == nil {
			t.Fatal("expected a panic calling process with the wrong length")
		}
	}()
	fir.process(make([]float64, 7))
}

func TestEQFIRResetClearsHistory(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	h := randomSlice(rng, 64, 0.1)

	a := newEQFIR(h, 32)
	a.process(randomSlice(rng, 32, 1.0)) // prime history

	b := newEQFIR(h, 32)
	a.reset()

	y := randomSlice(rng, 32, 1.0)
	gotA := a.process(y)
	gotB := b.process(y)
	for i := range gotA {
		if math.Abs(gotA[i]-gotB[i]) > 1e-10 {
			t.Fatalf("index %d: after reset=%v fresh=%v", i, gotA[i], gotB[i])
		}
	}
}

func TestEQFIRZeroFilterIsSilence(t *testing.T) {
	fir := newEQFIR(make([]float64, 2048), 2048)
	x := make([]float64, 2048)
	for i := range x {
		x[i] = 12345.0
	}
	out := fir.process(x)
	for i, v := range out {
		if v != 0 {
			t.Fatalf("sample %d: got %v, want 0", i, v)
		}
	}
}

func TestEQFIRImpulseFilterIsIdentity(t *testing.T) {
	h := make([]float64, 2048)
	h[0] = 1.0
	fir := newEQFIR(h, 512)
	rng := rand.New(rand.NewSource(9))
	x := randomSlice(rng, 512, 1.0)
	out := fir.process(x)
	for i := range x {
		if math.Abs(out[i]-x[i]) > 1e-10 {
			t.Fatalf("sample %d: got %v want %v (impulse filter must be identity)", i, out[i], x[i])
		}
	}
}

// Confirms the Echo's own files load and are the right shape — a bad
// file would otherwise only surface as the stock curve silently not
// running.
func TestLoadRadarEQBandsFromTheEcho(t *testing.T) {
	rt := radarTuningForTest(t)
	bands, bounds := rt.FIRBands, rt.FIRBounds
	if len(bands) == 0 || len(bands) != len(bounds) {
		t.Fatalf("got %d bands / %d boundaries", len(bands), len(bounds))
	}
	for i, b := range bands {
		if len(b) != len(bands[0]) {
			t.Errorf("band %d: %d taps, band 0 has %d", i, len(b), len(bands[0]))
		}
		if i > 0 && bounds[i] <= bounds[i-1] {
			t.Errorf("boundaries do not rise: %v", bounds)
		}
	}
}

// The volume value comes from stock's mixer table, and a level landing on a
// boundary takes that boundary's own file — the same table as
// em_eq's test, pinned on both sides.
func TestRadarEQBandByVolume(t *testing.T) {
	bounds := []float64{50, 60, 70, 80, 100}
	gain := func(level int) float64 {
		if level == 0 {
			return 0
		}
		return math.Pow(10, float64(level-127)/40)
	}
	for _, tc := range []struct{ level, value, band int }{
		{0, 0, 0}, {3, 1, 0}, {37, 10, 0}, {38, 11, 0}, {77, 50, 0}, {78, 51, 1},
		{87, 60, 1}, {88, 61, 2}, {97, 70, 2}, {98, 71, 3}, {107, 80, 3},
		{108, 81, 4}, {127, 100, 4},
	} {
		if got := stockVolumeValue(gain(tc.level)); got != tc.value {
			t.Errorf("level %d: value %d, want %d", tc.level, got, tc.value)
		}
		if got := radarEQBand(gain(tc.level), bounds); got != tc.band {
			t.Errorf("level %d: band %d, want %d", tc.level, got, tc.band)
		}
	}
}

// From value 11 up, stock's mixer level is value + 27, so the value of every
// level from 38 up is level - 27, with no off-by-one at either end.
func TestStockVolumeValueIsLevelMinus27(t *testing.T) {
	for level := 38; level <= 127; level++ {
		g := math.Pow(10, float64(level-127)/40)
		if v := stockVolumeValue(g); v != level-27 {
			t.Fatalf("level %d: value %d, want %d", level, v, level-27)
		}
	}
}

// A band switch crossfades: the period it happens in starts on the old
// curve's output and ends on the new one's, and the period after is the
// new curve alone — the same as a filter that was on it all along.
func TestEQFIRBandSwitchCrossfades(t *testing.T) {
	const n = 256
	a := make([]float64, 32)
	b := make([]float64, 32)
	a[0], b[0] = 1, 0.25 // two pure gains, so the outputs are easy to state
	f := newEQFIRBands([][]float64{a, b}, n)
	x := make([]float64, n)
	for i := range x {
		x[i] = 1000
	}
	f.process(x)
	f.setBand(1)
	y := append([]float64(nil), f.process(x)...)
	if math.Abs(y[0]-(1000*(1-1.0/n)+250/float64(n))) > 1e-6 {
		t.Errorf("first sample of the fade = %g", y[0])
	}
	if math.Abs(y[n-1]-250) > 1e-6 {
		t.Errorf("last sample of the fade = %g, want 250", y[n-1])
	}
	y = f.process(x)
	for i, v := range y {
		if math.Abs(v-250) > 1e-6 {
			t.Fatalf("after the fade, sample %d = %g, want 250", i, v)
		}
	}
}
