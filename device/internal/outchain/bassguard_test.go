package outchain

import (
	"math"
	"testing"
)

// guardReduction plays a sine through a guard-only chain on band, with no
// voicing, and returns the deepest bass reduction it applied.
func guardReduction(band GuardBand, hz, dbfs float64) float64 {
	c := NewFor(48000, band, nil)
	p := DefaultParams()
	p.LimiterEnabled = false
	c.SetParams(p)
	c.SetActive(true)
	a := fullScale * math.Pow(10, dbfs/20)
	m := make([]int16, 2048)
	for k := 0; k < 24; k++ { // ~1s, past the release
		for i := range m {
			n := k*len(m) + i
			m[i] = int16(a * math.Sin(2*math.Pi*hz*float64(n)/48000))
		}
		c.Process(stereo(m))
	}
	return c.TakeStats().GuardReductionDb
}

// Radar's woofer plays the bass biscuit's driver cannot, and stock's own
// band for it says so: at a normal listening level biscuit's band takes the
// floor off 60Hz and 100Hz, radar's leaves 100Hz alone and only the deep
// band at full level is reduced.
func TestEachBoardsBandProtectsItsOwnDriver(t *testing.T) {
	radar := GuardBand{CrossoverHz: 70, ThresholdDb: -25}
	for _, c := range []struct {
		band        GuardBand
		hz, dbfs    float64
		atLeast, le float64
	}{
		{DefaultGuardBand, 60, -30, 15, 30},
		{DefaultGuardBand, 100, -30, 10, 30},
		{radar, 100, -20, 0, 0.5},
		{radar, 60, -30, 0, 0.5},
		{radar, 40, -6, 10, 30},
	} {
		got := guardReduction(c.band, c.hz, c.dbfs)
		if got < c.atLeast || got > c.le {
			t.Errorf("%+v: %gHz at %gdBFS reduced %.1fdB, want %g..%g", c.band, c.hz, c.dbfs, got, c.atLeast, c.le)
		}
	}
}

// A board with no voicing plays the plain chain whatever the params ask, and
// says so: the speaker's volume law is chosen from the params in force.
func TestStockSoundNeedsTheBoardsVoicing(t *testing.T) {
	c := NewFor(48000, GuardBand{CrossoverHz: 70, ThresholdDb: -25}, nil)
	p := DefaultParams()
	p.StockSound = true
	c.SetParams(p)
	c.SetActive(true)
	c.Process(stereo(make([]int16, 512)))
	if c.params.StockSound {
		t.Fatal("StockSound in force on a board with no voicing")
	}
	if c.guard.upper != nil || len(c.corr.s) != 0 {
		t.Fatal("upper bands or correction built with no voicing")
	}
}
