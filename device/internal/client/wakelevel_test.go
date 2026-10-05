package client

import (
	"math"
	"testing"
	"time"
)

// The same vector and answer as controller/tests/test_wakelevel.py.
var wakeVector = []float64{
	0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004,
	0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004,
	0.002, 0.01, 0.03, 0.05, 0.04, 0.02, 0.008, 0.003, 0.001, 0.0005,
}

const gainDb = 24.0

func TestWakeLevelSharedVector(t *testing.T) {
	var r levelRing
	gain := math.Pow(10, gainDb/20)
	t0 := time.Now()
	for i := 0; i < 30; i++ { // loud, before the window
		r.push(t0.Add(time.Duration(i)*80*time.Millisecond), 0.9, gain)
	}
	var at time.Time
	for i, v := range wakeVector {
		at = t0.Add(time.Duration(30+i) * 80 * time.Millisecond)
		r.push(at, v, gain)
	}
	// Frames after the crossing, pushed before the scorer fired, are not in it.
	r.push(at.Add(80*time.Millisecond), 0.9, gain)

	level, peak, ok := r.measure(at)
	want := [2]float64{-60.5, -50.0}
	if !ok || level != want[0] || peak != want[1] {
		t.Fatalf("measure = %v %v %v, want %v", level, peak, ok, want)
	}
}

func TestWakeLevelFrameGone(t *testing.T) {
	var r levelRing
	t0 := time.Now()
	for i := 0; i < wakeLevelKeep+5; i++ {
		r.push(t0.Add(time.Duration(i)*80*time.Millisecond), 0.01, 1)
	}
	if _, _, ok := r.measure(t0); ok {
		t.Fatal("a frame that left the ring must not be measured")
	}
	if _, _, ok := r.measure(t0.Add(time.Millisecond)); ok {
		t.Fatal("a crossing stamp that matches no frame must not be measured")
	}
}

func TestWakeLevelSilenceIsTheFloor(t *testing.T) {
	level, peak := wakeLevel([]float64{0, 0})
	if level != wakeLevelFloor || peak != wakeLevelFloor {
		t.Fatalf("got %v %v", level, peak)
	}
}

func TestWakeLevelShortRing(t *testing.T) {
	var r levelRing
	at := time.Now()
	r.push(at, 0.1, 1)
	if level, peak, ok := r.measure(at); !ok || level != -20 || peak != -20 {
		t.Fatalf("got %v %v %v", level, peak, ok)
	}
}
