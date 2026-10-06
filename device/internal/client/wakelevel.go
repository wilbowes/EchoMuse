package client

import (
	"encoding/binary"
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
//
// tilt is the word's tone, over the same window: the energy of the
// sample-to-sample difference over the energy of the samples, in dB. A voice
// loses its high frequencies with distance and round a corner, and the
// difference weights them up, so a nearer voice reads higher. It is a ratio
// inside one Echo, so no gain setting or microphone sensitivity is in it.
// Logged only; nothing decides on it yet.

const (
	wakeLevelFrames = 25
	// Frames kept: the window plus slack for a scorer running a few frames
	// behind the mic loop, so the crossing frame is still here when it fires.
	wakeLevelKeep  = 64
	wakeLevelFloor = -120.0
)

// WakeLevel is one wake's level and peak, dBFS, and its tilt in dB when the
// window held any signal to measure it on.
type WakeLevel struct {
	Level, Peak float64
	Tilt        float64
	HasTilt     bool
}

type levelFrame struct {
	at  time.Time
	rms float64 // gain removed
	// Sums for the tilt: samples squared, and their first difference squared.
	sq, dsq float64
}

type levelRing struct {
	mu     sync.Mutex
	frames [wakeLevelKeep]levelFrame
	n      int // frames ever pushed
}

func (r *levelRing) push(at time.Time, rms, gainLin float64) {
	r.store(at, rms, gainLin, 0, 0)
}

// pushFrame is push for one 80ms frame of the wake stream (S16LE mono), which
// also keeps what the tilt needs.
func (r *levelRing) pushFrame(at time.Time, mono []byte, gainLin float64) {
	sq, dsq := tiltSums(mono)
	r.store(at, vadPeriodRMS(mono), gainLin, sq, dsq)
}

func (r *levelRing) store(at time.Time, rms, gainLin, sq, dsq float64) {
	if gainLin > 0 {
		rms /= gainLin
	}
	r.mu.Lock()
	r.frames[r.n%wakeLevelKeep] = levelFrame{at, rms, sq, dsq}
	r.n++
	r.mu.Unlock()
}

// tiltSums is one frame's contribution to the tilt: the sum of squares of
// samples 1..n-1 and of their differences from the sample before. Each frame
// starts afresh, so the answer does not depend on what came before it.
func tiltSums(mono []byte) (sq, dsq float64) {
	n := len(mono) / 2
	if n < 2 {
		return 0, 0
	}
	prev := float64(int16(binary.LittleEndian.Uint16(mono))) / 32768.0
	for i := 1; i < n; i++ {
		f := float64(int16(binary.LittleEndian.Uint16(mono[i*2:]))) / 32768.0
		sq += f * f
		dsq += (f - prev) * (f - prev)
		prev = f
	}
	return sq, dsq
}

// wakeTilt is em_wakelevel.tilt: 10*log10(dsq/sq) to 0.1dB, or ok=false when
// there is nothing to take a ratio of.
func wakeTilt(sq, dsq float64) (float64, bool) {
	if sq <= 0 || dsq <= 0 {
		return 0, false
	}
	return round1(10 * math.Log10(dsq/sq)), true
}

// window is the frames of the wake whose last frame was stamped at, oldest
// first, or ok=false if that frame has already left the ring. Called with mu
// held.
func (r *levelRing) window(at time.Time) (frames []levelFrame, ok bool) {
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
		return nil, false
	}
	start := end - wakeLevelFrames + 1
	if start < oldest {
		start = oldest
	}
	frames = make([]levelFrame, 0, wakeLevelFrames)
	for i := start; i <= end; i++ {
		frames = append(frames, r.frames[i%wakeLevelKeep])
	}
	return frames, true
}

// measure returns level and peak for the window ending at the frame stamped
// at, or ok=false if that frame has already left the ring.
func (r *levelRing) measure(at time.Time) (level, peak float64, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	frames, ok := r.window(at)
	if !ok {
		return 0, 0, false
	}
	rms := make([]float64, 0, len(frames))
	for _, f := range frames {
		rms = append(rms, f.rms)
	}
	level, peak = wakeLevel(rms)
	return level, peak, true
}

// tilt returns the tilt of the same window measure uses.
func (r *levelRing) tilt(at time.Time) (float64, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	frames, ok := r.window(at)
	if !ok {
		return 0, false
	}
	var sq, dsq float64
	for _, f := range frames {
		sq += f.sq
		dsq += f.dsq
	}
	return wakeTilt(sq, dsq)
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
