package sendspin

import (
	"sync"
	"time"
)

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
//
// A single reading more than 3ms off is GATED: the loop holds its
// prediction and neither phase nor rate learns from it. Measured on C95
// (#707): one ALSA delay reading 18ms early, inside the 20ms reset, swung
// the rate estimate to -1022ppm against a real ~1ppm, and the scheduler
// then made ~300 corrections a minute chasing it. Normal noise there is
// ±0.3ms. Four gated readings in a row are a real shift, not an outlier,
// and reset the loop onto the new position.
type OutputClock struct {
	nominal float64 // ns per period
	per     float64 // current estimate, ns
	last    time.Time
	n       int
	valid   bool
	gated   int // consecutive readings past outClockGate

	diagMu sync.Mutex
	diag   OutDiag
}

// OutDiag summarises the tracker since it was last taken, for the device log:
// whether correction bursts line up with the DAC position measurement jumping
// (#707). Residual is measured minus predicted; Nudge is how far a single
// period moved the smoothed estimate off its own prediction.
type OutDiag struct {
	N                      int
	ResidMinUs, ResidMaxUs int64
	BigResid               int // |residual| over the scheduler's ~100µs deadband
	NudgeMaxUs             int64
	Gated                  int // readings skipped as outliers
	Coasted                int // readings too uncertain to learn from
	Resets                 int
	RatePpm                float64 // the tracked period against nominal
}

// outDiagBigUs is the scheduler's deadband (5 frames at 48kHz), the size of
// residual that could reach a correction if the loop passed it through.
const outDiagBigUs = 104

const (
	outClockPhaseFloor = 1.0 / 128
	outClockRateFloor  = 1.0 / 32768
	outClockReset      = 20 * time.Millisecond
	outClockGate       = 3 * time.Millisecond
	// outClockTrust is the widest uncertainty a reading may carry and still
	// be learned from: half a status read that took 1ms. A read that slow
	// was interrupted, and the reading says when it resumed, not where the
	// DAC was.
	outClockTrust   = 500 * time.Microsecond
	outClockGateRun = 4
	outClockMaxPPM  = 2000
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
		c.last, c.per, c.n, c.gated = measured, c.nominal, 1, 0
		c.noteDiag(int64(err/1e3), 0, diagReset)
		return measured
	}
	if err > float64(outClockGate) || err < -float64(outClockGate) {
		c.gated++
		if c.gated >= outClockGateRun {
			c.last, c.per, c.n, c.gated = measured, c.nominal, 1, 0
			c.noteDiag(int64(err/1e3), 0, diagReset)
			return measured
		}
		c.last = pred
		c.noteDiag(int64(err/1e3), 0, diagGated)
		return pred
	}
	c.gated = 0
	c.n++
	n := float64(c.n)
	alpha := max(2*(2*n-1)/(n*(n+1)), outClockPhaseFloor)
	beta := max(6/(n*(n+1)), outClockRateFloor)
	c.last = pred.Add(time.Duration(alpha * err))
	c.per += beta * err
	c.noteDiag(int64(err/1e3), int64(alpha*err/1e3), diagUsed)
	lim := c.nominal * outClockMaxPPM / 1e6
	if c.per > c.nominal+lim {
		c.per = c.nominal + lim
	} else if c.per < c.nominal-lim {
		c.per = c.nominal - lim
	}
	return c.last
}

// Coast advances the estimate by one period without learning from this
// reading, for a measurement too uncertain to trust. With no estimate yet
// there is nothing to coast on, so the reading is used as it stands.
func (c *OutputClock) Coast(measured time.Time) time.Time {
	if !c.valid {
		return c.Observe(measured)
	}
	pred := c.last.Add(time.Duration(c.per))
	c.last = pred
	c.noteDiag(int64(measured.Sub(pred)/time.Microsecond), 0, diagCoasted)
	return pred
}

// Reset forgets the loop, for when the output restarts.
func (c *OutputClock) Reset() { c.valid, c.gated = false, 0 }

type diagKind int

const (
	diagUsed diagKind = iota
	diagGated
	diagCoasted
	diagReset
)

func (c *OutputClock) noteDiag(residUs, nudgeUs int64, kind diagKind) {
	c.diagMu.Lock()
	defer c.diagMu.Unlock()
	d := &c.diag
	if d.N == 0 {
		d.ResidMinUs, d.ResidMaxUs = residUs, residUs
	}
	d.N++
	d.ResidMinUs, d.ResidMaxUs = min(d.ResidMinUs, residUs), max(d.ResidMaxUs, residUs)
	if residUs > outDiagBigUs || residUs < -outDiagBigUs {
		d.BigResid++
	}
	if nudgeUs < 0 {
		nudgeUs = -nudgeUs
	}
	d.NudgeMaxUs = max(d.NudgeMaxUs, nudgeUs)
	switch kind {
	case diagGated:
		d.Gated++
	case diagCoasted:
		d.Coasted++
	case diagReset:
		d.Resets++
	}
	d.RatePpm = (c.per - c.nominal) / c.nominal * 1e6
}

// TakeDiag returns the tracker's summary since the last call and starts a new
// window. Safe from any goroutine; Observe runs on the speaker's.
func (c *OutputClock) TakeDiag() OutDiag {
	c.diagMu.Lock()
	defer c.diagMu.Unlock()
	d := c.diag
	c.diag = OutDiag{}
	return d
}
