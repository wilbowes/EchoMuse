package client

import (
	"encoding/binary"
	"math"
	"math/rand"
	"testing"
	"time"
)

func noiseChans(r *rand.Rand, m, n int, amp float64) [][]int16 {
	out := make([][]int16, m)
	for c := range out {
		out[c] = make([]int16, n)
		for i := range out[c] {
			out[c][i] = int16(amp * r.NormFloat64())
		}
	}
	return out
}

// loudScorer calls a frame speech when it is loud: enough to drive the
// segmenter in a test without Silero.
type loudScorer struct{}

func (loudScorer) Prob(x []float32) (float32, error) {
	e := 0.0
	for _, v := range x {
		e += float64(v) * float64(v)
	}
	if math.Sqrt(e/float64(len(x))) > 0.05 {
		return 0.95, nil
	}
	return 0.02, nil
}

func TestTheTurnStageIsOffUnlessAskedFor(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "")
	if newTurnBeam(4) != nil {
		t.Fatal("the turn stage is on without EM_TURNBEAM=on")
	}
	var tb *turnBeam
	tb.requestLock(time.Now()) // nil-safe
	if tb.step(noiseChans(rand.New(rand.NewSource(1)), 4, 2560, 500), make([]byte, 5120), true) != nil {
		t.Fatal("a nil stage changed the session's audio")
	}
}

// Without a speech detector the stage leaves the plain stream alone.
func TestNoSpeechDetectorMeansThePlainStream(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "on")
	tb := newTurnBeam(4)
	r := rand.New(rand.NewSource(5))
	for i := 0; i < 60; i++ {
		tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), false)
	}
	tb.requestLock(time.Now())
	for i := 0; i < 10; i++ {
		if out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), true); out != nil {
			t.Fatal("the stage replaced audio with no speech detector")
		}
	}
}

// Outside a session the stage never replaces anything: it only learns.
// Inside one with nobody talking, the session carries silence of the same
// length as the plain stream, and it lets go when the session closes.
func TestTheTurnStageOnlyEverSendsLess(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "on")
	tb := newTurnBeam(4)
	tb.newVAD = func() speechScorer { return loudScorer{} }
	r := rand.New(rand.NewSource(2))
	for i := 0; i < 60; i++ { // ~9.6s of room, no session
		if out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), false); out != nil {
			t.Fatal("the stage replaced audio with no session open")
		}
	}
	tb.requestLock(time.Now())
	for i := 0; i < 40; i++ {
		out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), true)
		if len(out) != 2*2560 {
			t.Fatalf("stage output is %d bytes for a 2560-sample batch", len(out))
		}
		for _, b := range out {
			if b != 0 {
				t.Fatal("the session carried audio with nobody talking")
			}
		}
	}
	if !tb.seg.Done() {
		t.Fatal("the turn never resolved with nobody talking")
	}
	if out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), false); out != nil {
		t.Fatal("the stage kept replacing audio after the session closed")
	}
	if tb.active {
		t.Fatal("the stage stayed locked after the session closed")
	}
}

// A talker after the wake: the session carries them, as long as the
// segment, with silence before and after, and nothing inside cut.
func TestTheCommandGoesOutWhole(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "on")
	tb := newTurnBeam(4)
	tb.newVAD = func() speechScorer { return loudScorer{} }
	r := rand.New(rand.NewSource(3))
	batch := 2560
	quiet := func() [][]int16 { return noiseChans(r, 4, batch, 100) }
	talk := func(n int) [][]int16 { // one direction: integer delays
		src := make([]float64, n+4)
		for i := range src {
			src[i] = 4000 * r.NormFloat64()
		}
		out := make([][]int16, 4)
		for c, d := range []int{0, 1, 2, 1} {
			out[c] = make([]int16, n)
			for i := 0; i < n; i++ {
				out[c][i] = int16(src[i+4-d] + 100*r.NormFloat64())
			}
		}
		return out
	}
	for i := 0; i < 60; i++ { // the room, then the wake word in the last 1.6s
		ch := quiet()
		if i >= 50 {
			ch = talk(batch)
		}
		tb.step(ch, make([]byte, 2*batch), false)
	}
	tb.requestLock(time.Now())
	var sent []int16
	for i := 0; i < 30; i++ {
		ch := quiet()
		if t0 := float64(i*batch) / 16000; t0 >= 0.8 && t0 < 1.9 {
			ch = talk(batch)
		}
		out := tb.step(ch, make([]byte, 2*batch), true)
		for j := 0; j+1 < len(out); j += 2 {
			sent = append(sent, int16(binary.LittleEndian.Uint16(out[j:])))
		}
	}
	a, b, ok := tb.seg.Bounds()
	if !ok {
		t.Fatal("no segment for a talker")
	}
	t.Logf("segment %+.2f..%+.2fs around the crossing", a, b)
	first, last := -1, -1
	for i, v := range sent {
		if v != 0 {
			if first < 0 {
				first = i
			}
			last = i
		}
	}
	if first < 0 {
		t.Fatal("the talker was never sent")
	}
	if dur := float64(last-first) / 16000; math.Abs(dur-(b-a)) > 0.05 {
		t.Fatalf("sent %.2fs of audio for a %.2fs segment", dur, b-a)
	}
	for i := first + 480; i+160 < last-480; i += 160 { // inside, past the ramps
		z := 0
		for j := i; j < i+160; j++ {
			if sent[j] == 0 {
				z++
			}
		}
		if z > 20 {
			t.Fatalf("a cut inside the command at sample %d", i)
		}
	}
}

// A late lock (a second look ~1.5s after the wake word) starts with a
// backlog; the session catches up over known silence, so the command goes out
// no later than SegDelayS behind the room plus the remaining backlog, and the
// stream ends up SegDelayS behind.
func TestALateLockCatchesUp(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "on")
	tb := newTurnBeam(4)
	tb.newVAD = func() speechScorer { return loudScorer{} }
	r := rand.New(rand.NewSource(4))
	batch := 2560
	for i := 0; i < 80; i++ {
		tb.step(noiseChans(r, 4, batch, 100), make([]byte, 2*batch), false)
	}
	tb.requestLock(time.Now().Add(-1500 * time.Millisecond))
	for i := 0; i < 30; i++ {
		tb.step(noiseChans(r, 4, batch, 100), make([]byte, 2*batch), true)
	}
	if !tb.active {
		t.Fatal("a lock 1.5s after the crossing was refused")
	}
	lag := float64(len(tb.fifo)) / 16000
	if lag > 0.64+0.2 {
		t.Fatalf("the session is %.2fs behind after catching up, want ~%.2fs", lag, 0.64)
	}
}
