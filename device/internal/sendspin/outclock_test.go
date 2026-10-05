package sendspin

import (
	"math/rand"
	"testing"
	"time"
)

// A DAC off nominal, measured through ±2ms of noise (pessimistic for DMA
// pointer granularity): the smoothed estimate settles to a few hundred µs,
// against a 1ms sync floor.
func TestOutputClockRecoversTheDACThroughJitter(t *testing.T) {
	period := time.Duration(2048) * time.Second / 48000
	for _, ppm := range []float64{-2000, -560, 0, 345, 2000} {
		c := NewOutputClock(period)
		rng := rand.New(rand.NewSource(89))
		truePer := float64(period) * (1 - ppm/1e6)
		t0 := epoch.Add(time.Hour)
		worst := time.Duration(0)
		for k := 0; k < 2000; k++ {
			truth := t0.Add(time.Duration(float64(k) * truePer))
			jitter := time.Duration((rng.Float64()*2 - 1) * float64(2*time.Millisecond))
			got := c.Observe(truth.Add(jitter))
			if k > 600 {
				if d := got.Sub(truth); d > worst {
					worst = d
				} else if -d > worst {
					worst = -d
				}
			}
		}
		if worst > 400*time.Microsecond {
			t.Errorf("%vppm: settled error up to %v", ppm, worst)
		}
	}
}

// An xrun or restart moves the DAC by far more than jitter: follow it at
// once rather than slewing for seconds.
func TestOutputClockFollowsAJump(t *testing.T) {
	period := 42 * time.Millisecond
	c := NewOutputClock(period)
	t0 := epoch.Add(time.Hour)
	for k := 0; k < 50; k++ {
		c.Observe(t0.Add(time.Duration(k) * period))
	}
	jumped := t0.Add(50*period + 100*time.Millisecond)
	if got := c.Observe(jumped); !got.Equal(jumped) {
		t.Errorf("after a 100ms jump, estimate is %v off", got.Sub(jumped))
	}
}

func TestOutDiagWindow(t *testing.T) {
	per := 42667 * time.Microsecond
	c := NewOutputClock(per)
	t0 := time.Unix(0, 0)
	for i := 0; i < 50; i++ {
		c.Observe(t0.Add(time.Duration(i) * per))
	}
	c.TakeDiag()                                     // converged; start a clean window
	c.Observe(t0.Add(50*per + 300*time.Microsecond)) // one measurement 300µs off
	c.Observe(t0.Add(51*per + 30*time.Millisecond))  // a jump: resets the loop
	d := c.TakeDiag()
	if d.N != 2 || d.BigResid != 2 || d.Resets != 1 || d.ResidMaxUs < 29000 || d.NudgeMaxUs == 0 {
		t.Fatalf("diag = %+v", d)
	}
	if again := c.TakeDiag(); again.N != 0 {
		t.Fatalf("window not reset: %+v", again)
	}
}

// The C95 failure (#707): one ALSA delay reading 18ms early, inside the 20ms
// reset bound. Before the gate it swung the rate estimate to -1022ppm and the
// scheduler chased the error for a minute; now the estimate must not move
// past the scheduler's ~100µs deadband at any point after it.
func TestOutputClockIgnoresASingleOutlier(t *testing.T) {
	period := time.Duration(2048) * time.Second / 48000
	for _, outlier := range []time.Duration{-18 * time.Millisecond, -5 * time.Millisecond, 10 * time.Millisecond} {
		c := NewOutputClock(period)
		rng := rand.New(rand.NewSource(707))
		t0 := epoch.Add(time.Hour)
		worst := time.Duration(0)
		for k := 0; k < 1500; k++ {
			truth := t0.Add(time.Duration(k) * period)
			meas := truth.Add(time.Duration((rng.Float64()*2 - 1) * float64(300*time.Microsecond)))
			if k == 800 {
				meas = truth.Add(outlier)
			}
			got := c.Observe(meas)
			if k >= 800 {
				d := got.Sub(truth)
				if d < 0 {
					d = -d
				}
				worst = max(worst, d)
			}
		}
		if worst > 104*time.Microsecond {
			t.Errorf("outlier %v: estimate moved up to %v after it", outlier, worst)
		}
		if d := c.TakeDiag(); d.Gated != 1 || d.Resets != 0 {
			t.Errorf("outlier %v: gated %d, resets %d; want 1 and 0", outlier, d.Gated, d.Resets)
		}
	}
}

// A shift that persists is the DAC really moving (an xrun shorter than the
// 20ms reset): follow it once it has held for outClockGateRun periods.
func TestOutputClockFollowsASustainedShift(t *testing.T) {
	period := 42 * time.Millisecond
	c := NewOutputClock(period)
	t0 := epoch.Add(time.Hour)
	for k := 0; k < 100; k++ {
		c.Observe(t0.Add(time.Duration(k) * period))
	}
	shift := 8 * time.Millisecond
	var got, want time.Time
	for k := 100; k < 100+outClockGateRun; k++ {
		want = t0.Add(time.Duration(k)*period + shift)
		got = c.Observe(want)
	}
	if !got.Equal(want) {
		t.Errorf("after %d shifted readings the estimate is %v off", outClockGateRun, got.Sub(want))
	}
}

// A reading too uncertain to learn from moves the estimate by one period and
// nothing else, however far off it is.
func TestOutputClockCoastsThroughAnUncertainReading(t *testing.T) {
	period := 42 * time.Millisecond
	c := NewOutputClock(period)
	t0 := epoch.Add(time.Hour)
	for k := 0; k < 100; k++ {
		c.Observe(t0.Add(time.Duration(k) * period))
	}
	want := t0.Add(100 * period)
	if got := c.Coast(want.Add(15 * time.Millisecond)); !got.Equal(want) {
		t.Errorf("coasted estimate is %v off the prediction", got.Sub(want))
	}
	if got := c.Observe(t0.Add(101 * period)); got.Sub(t0.Add(101*period)).Abs() > time.Microsecond {
		t.Errorf("after coasting, the next reading is %v off", got.Sub(t0.Add(101*period)))
	}
	if d := c.TakeDiag(); d.Coasted != 1 {
		t.Errorf("coasted = %d, want 1", d.Coasted)
	}
}
