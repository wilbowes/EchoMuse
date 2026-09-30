package sendspin

import "time"

// OutputClock smooths the speaker's estimate of when each period reaches the
// DAC.
//
// The estimate is measured per period as read time + frames queued in ALSA /
// rate. A late wake does not move it — a late reader finds fewer frames
// queued — so what is left is the DMA pointer's granularity and the gap
// between reading the clock and reading the status. The DAC itself is steady,
// one period per period on its crystal, so a phase-locked loop recovers its
// position to well inside the scheduler's ~100µs deadband. Fed the raw
// measurement, the scheduler corrected on 147 of 148 periods in the interop
// test, each a dropped or repeated frame chasing noise.
//
// An alpha-beta tracker (phase and rate), so a DAC running off nominal is
// followed with no standing error. The gains start at the values that make it
// a least-squares line fit over the periods seen so far, so the first second
// converges as fast as the data allows, and narrow to fixed floor gains
// (time constant ~128 periods, 5.5s; damping 0.7) that it keeps. A jump
// beyond 20ms is not noise — an xrun or a restart — and resets the loop.
type OutputClock struct {
	nominal float64 // ns per period
	per     float64 // current estimate, ns
	last    time.Time
	n       int
	valid   bool
}

const (
	outClockPhaseFloor = 1.0 / 128
	outClockRateFloor  = 1.0 / 32768
	outClockReset      = 20 * time.Millisecond
	outClockMaxPPM     = 2000
)

// NewOutputClock takes the nominal duration of one period.
func NewOutputClock(period time.Duration) *OutputClock {
	return &OutputClock{nominal: float64(period), per: float64(period)}
}

// Observe takes this period's measured DAC time and returns the smoothed one.
func (c *OutputClock) Observe(measured time.Time) time.Time {
	if !c.valid {
		c.last, c.per, c.valid, c.n = measured, c.nominal, true, 1
		return measured
	}
	pred := c.last.Add(time.Duration(c.per))
	err := float64(measured.Sub(pred))
	if err > float64(outClockReset) || err < -float64(outClockReset) {
		c.last, c.per, c.n = measured, c.nominal, 1
		return measured
	}
	c.n++
	n := float64(c.n)
	alpha := max(2*(2*n-1)/(n*(n+1)), outClockPhaseFloor)
	beta := max(6/(n*(n+1)), outClockRateFloor)
	c.last = pred.Add(time.Duration(alpha * err))
	c.per += beta * err
	lim := c.nominal * outClockMaxPPM / 1e6
	if c.per > c.nominal+lim {
		c.per = c.nominal + lim
	} else if c.per < c.nominal-lim {
		c.per = c.nominal - lim
	}
	return c.last
}

// Reset forgets the loop, for when the output restarts.
func (c *OutputClock) Reset() { c.valid = false }
