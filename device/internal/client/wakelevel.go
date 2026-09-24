package client

import (
	"math"
	"sync"
	"time"
)

// How loud a wake word was here, sent on oww_wake so the controller can log
// it beside the capture time. The controller measures the wakes it scores
// from the stream the same way, and the definition must stay identical on
// both sides: controller/em_wakelevel.py, tested against the same vector.
//
// Per 80ms wake-stream frame RMS with the mic's digital gain removed, over
// the wakeLevelFrames frames ending with the one the score crossed on (2.0s,
// covering the classifier's 1.96s view). level is the energy mean, peak the
// loudest frame, both dBFS rounded to 0.1.

const (
	wakeLevelFrames = 25
	// Frames kept: the window plus slack for a scorer running a few frames
	// behind the mic loop, so the crossing frame is still here when it fires.
	wakeLevelKeep  = 64
	wakeLevelFloor = -120.0
)

// WakeLevel is one wake's level and peak, dBFS.
type WakeLevel struct{ Level, Peak float64 }

type levelFrame struct {
	at  time.Time
	rms float64 // gain removed
}

type levelRing struct {
	mu     sync.Mutex
	frames [wakeLevelKeep]levelFrame
	n      int // frames ever pushed
}

func (r *levelRing) push(at time.Time, rms, gainLin float64) {
	if gainLin > 0 {
		rms /= gainLin
	}
	r.mu.Lock()
	r.frames[r.n%wakeLevelKeep] = levelFrame{at, rms}
	r.n++
	r.mu.Unlock()
}

// measure returns level and peak for the window ending at the frame stamped
// at, or ok=false if that frame has already left the ring.
func (r *levelRing) measure(at time.Time) (level, peak float64, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	oldest := r.n - wakeLevelKeep
	if oldest < 0 {
		oldest = 0
	}
	end := -1
	for i := r.n - 1; i >= oldest; i-- {
		if !r.frames[i%wakeLevelKeep].at.After(at) {
			end = i
			break
		}
	}
	if end < 0 || !r.frames[end%wakeLevelKeep].at.Equal(at) {
		return 0, 0, false
	}
	start := end - wakeLevelFrames + 1
	if start < oldest {
		start = oldest
	}
	rms := make([]float64, 0, wakeLevelFrames)
	for i := start; i <= end; i++ {
		rms = append(rms, r.frames[i%wakeLevelKeep].rms)
	}
	level, peak = wakeLevel(rms)
	return level, peak, true
}

// wakeLevel is em_wakelevel.measure with the gain already removed.
func wakeLevel(rms []float64) (level, peak float64) {
	var energy, max float64
	for _, v := range rms {
		energy += v * v
		if v > max {
			max = v
		}
	}
	return round1(dbfs(math.Sqrt(energy / float64(len(rms))))), round1(dbfs(max))
}

func dbfs(rms float64) float64 {
	if rms <= 0 {
		return wakeLevelFloor
	}
	return math.Max(wakeLevelFloor, 20*math.Log10(rms))
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }
