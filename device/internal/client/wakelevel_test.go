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

// One 80ms frame: this eight-sample pattern 160 times. The same frame and
// answer as controller/tests/test_wakelevel.py.
var tiltPattern = []int16{0, 1000, 3000, 2000, -1000, -4000, -2000, 500}

const tiltWant = -0.1

func tiltFrame() []byte {
	b := make([]byte, 0, 1280*2)
	for i := 0; i < 160; i++ {
		for _, v := range tiltPattern {
			b = append(b, byte(uint16(v)), byte(uint16(v)>>8))
		}
	}
	return b
}

func TestWakeTiltSharedVector(t *testing.T) {
	var r levelRing
	t0 := time.Now()
	var at time.Time
	for i := 0; i < wakeLevelFrames; i++ {
		at = t0.Add(time.Duration(i) * 80 * time.Millisecond)
		r.pushFrame(at, tiltFrame(), 1)
	}
	got, ok := r.tilt(at)
	if !ok || got != tiltWant {
		t.Fatalf("tilt = %v %v, want %v", got, ok, tiltWant)
	}
	// The level is unchanged by carrying the tilt beside it.
	if _, _, ok := r.measure(at); !ok {
		t.Fatal("measure lost the window")
	}
}

func TestWakeTiltIgnoresGain(t *testing.T) {
	var a, b levelRing
	at := time.Now()
	a.pushFrame(at, tiltFrame(), 1)
	b.pushFrame(at, tiltFrame(), 15.85)
	ta, _ := a.tilt(at)
	tb, _ := b.tilt(at)
	if ta != tb {
		t.Fatalf("tilt moved with the gain: %v vs %v", ta, tb)
	}
}

func TestWakeTiltNeedsSignal(t *testing.T) {
	var r levelRing
	at := time.Now()
	r.pushFrame(at, make([]byte, 2560), 1) // digital silence
	if _, ok := r.tilt(at); ok {
		t.Fatal("silence has no tilt")
	}
	r.push(at.Add(80*time.Millisecond), 0.01, 1) // a frame pushed without samples
	if _, ok := r.tilt(at.Add(80 * time.Millisecond)); ok {
		t.Fatal("frames with no samples kept have no tilt")
	}
}
