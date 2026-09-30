package sendspin

import (
	"math"
	"sync"
)

// timeFilter maps the server's clock onto ours: a 2-D Kalman filter over
// offset and drift, fed NTP-style exchanges. The spec REQUIRES this exact
// algorithm, so it is a line-for-line port of aiosendspin 9.1.1's
// client/time_sync.py (itself a port of the ESPHome/C++ reference), and the
// test replays the same inputs through both and compares.
//
// All times are microseconds. "Client time" is this process's monotonic
// clock (see clock.go).
type timeFilter struct {
	mu sync.Mutex

	lastUpdate int64
	count      int

	offset, drift float64

	offsetCov, offsetDriftCov, driftCov float64

	processVar, driftProcessVar, forgetVarFactor float64

	cur timeElement
}

type timeElement struct {
	lastUpdate    int64
	offset, drift float64
	useDrift      bool
}

const (
	adaptiveForgettingCutoff = 3.0
	maxErrorScale            = 0.5
	driftSignificanceSq      = 2.0 * 2.0
)

func newTimeFilter() *timeFilter {
	f := &timeFilter{}
	f.init(0, 2.0, 1e-11)
	return f
}

func (f *timeFilter) init(processStd, forget, driftProcessStd float64) {
	f.processVar = processStd * processStd
	f.driftProcessVar = driftProcessStd * driftProcessStd
	f.forgetVarFactor = forget * forget
	f.resetLocked()
}

func (f *timeFilter) reset() {
	f.mu.Lock()
	f.resetLocked()
	f.mu.Unlock()
}

func (f *timeFilter) resetLocked() {
	f.count, f.lastUpdate = 0, 0
	f.offset, f.drift = 0, 0
	f.offsetCov, f.offsetDriftCov, f.driftCov = math.Inf(1), 0, 0
	f.cur = timeElement{}
}

// update takes one measurement: offset = ((T2-T1)+(T3-T4))/2 and
// maxError = ((T4-T1)-(T3-T2))/2, taken at client time timeAdded.
func (f *timeFilter) update(measurement, maxError, timeAdded int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if timeAdded <= f.lastUpdate {
		return
	}
	dt := float64(timeAdded - f.lastUpdate)
	f.lastUpdate = timeAdded

	updateStd := float64(maxError) * maxErrorScale
	measVar := updateStd * updateStd

	if f.count <= 0 {
		f.count++
		f.offset = float64(measurement)
		f.offsetCov = measVar
		f.drift = 0
		f.cur = timeElement{lastUpdate: f.lastUpdate, offset: f.offset, drift: f.drift}
		return
	}
	if f.count == 1 {
		f.count++
		f.drift = (float64(measurement) - f.offset) / dt
		f.offset = float64(measurement)
		f.driftCov = (f.offsetCov + measVar) / (dt * dt)
		f.offsetCov = measVar
		f.cur = timeElement{lastUpdate: f.lastUpdate, offset: f.offset, drift: f.drift}
		return
	}

	offset := f.offset + f.drift*dt
	dt2 := dt * dt
	newDriftCov := f.driftCov + dt*f.driftProcessVar
	newOffsetDriftCov := f.offsetDriftCov + f.driftCov*dt
	newOffsetCov := f.offsetCov + 2*f.offsetDriftCov*dt + f.driftCov*dt2 + dt*f.processVar

	residual := float64(measurement) - offset
	cutoff := float64(maxError) * adaptiveForgettingCutoff
	if f.count < 100 {
		f.count++
	} else if math.Abs(residual) > cutoff {
		newDriftCov *= f.forgetVarFactor
		newOffsetDriftCov *= f.forgetVarFactor
		newOffsetCov *= f.forgetVarFactor
	}

	uncertainty := 1.0 / math.Max(newOffsetCov+measVar, 1e-9)
	offsetGain := newOffsetCov * uncertainty
	driftGain := newOffsetDriftCov * uncertainty

	f.offset = offset + offsetGain*residual
	f.drift += driftGain * residual

	f.driftCov = newDriftCov - driftGain*newOffsetDriftCov
	f.offsetDriftCov = newOffsetDriftCov - driftGain*newOffsetCov
	f.offsetCov = newOffsetCov - offsetGain*newOffsetCov

	useDrift := f.drift*f.drift > driftSignificanceSq*f.driftCov
	f.cur = timeElement{lastUpdate: f.lastUpdate, offset: f.offset, drift: f.drift, useDrift: useDrift}
}

// pyRound is Python's round(): half to even. The port has to round the same
// way to agree with the reference to the microsecond.
func pyRound(x float64) int64 { return int64(math.RoundToEven(x)) }

func (f *timeFilter) serverTime(client int64) int64 {
	f.mu.Lock()
	e := f.cur
	f.mu.Unlock()
	drift := 0.0
	if e.useDrift {
		drift = e.drift
	}
	dt := float64(client - e.lastUpdate)
	return client + pyRound(e.offset+drift*dt)
}

func (f *timeFilter) clientTime(server int64) int64 {
	f.mu.Lock()
	e := f.cur
	f.mu.Unlock()
	drift := 0.0
	if e.useDrift {
		drift = e.drift
	}
	return pyRound((float64(server) - e.offset + drift*float64(e.lastUpdate)) / (1.0 + drift))
}

func (f *timeFilter) synchronized() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.count >= 2 && !math.IsInf(f.offsetCov, 1)
}

// errorUs is the filter's standard deviation estimate.
func (f *timeFilter) errorUs() int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return pyRound(math.Sqrt(f.offsetCov))
}

// syncInterval is how long to wait before the next client/time: fast while
// converging, 3s once the estimate is inside a millisecond. Same ladder as
// the reference client.
func (f *timeFilter) syncIntervalMs() int {
	if !f.synchronized() {
		return 200
	}
	switch e := f.errorUs(); {
	case e < 1000:
		return 3000
	case e < 2000:
		return 1000
	case e < 5000:
		return 500
	}
	return 200
}
