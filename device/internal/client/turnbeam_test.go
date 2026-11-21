package client

import (
	"math/rand"
	"testing"
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

func TestTheTurnStageIsOffUnlessAskedFor(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "")
	if newTurnBeam(4) != nil {
		t.Fatal("the turn stage is on without EM_TURNBEAM=on")
	}
	var tb *turnBeam
	tb.requestLock() // nil-safe
	if tb.step(noiseChans(rand.New(rand.NewSource(1)), 4, 2560, 500), make([]byte, 5120), true) != nil {
		t.Fatal("a nil stage changed the session's audio")
	}
}

// Outside a session the stage never replaces anything: it only learns.
// Inside one, after it calls the talker done, the session carries silence
// of the same length as the plain stream.
func TestTheTurnStageOnlyEverSendsLess(t *testing.T) {
	t.Setenv("EM_TURNBEAM", "on")
	tb := newTurnBeam(4)
	r := rand.New(rand.NewSource(2))
	for i := 0; i < 60; i++ { // ~9.6s of room, no session
		if out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), false); out != nil {
			t.Fatal("the stage replaced audio with no session open")
		}
	}
	tb.requestLock()
	sawQuiet := false
	for i := 0; i < 40; i++ { // the room alone: the stage ends at StartS
		out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), true)
		if out == nil {
			continue
		}
		if len(out) != 2*2560 {
			t.Fatalf("stage output is %d bytes for a 2560-sample batch", len(out))
		}
		if tb.quiet {
			sawQuiet = true
			for _, b := range out {
				if b != 0 {
					t.Fatal("after the endpoint the session carried audio")
				}
			}
		}
	}
	if !sawQuiet {
		t.Fatal("the stage never ended a turn with nobody talking")
	}
	if out := tb.step(noiseChans(r, 4, 2560, 500), make([]byte, 5120), false); out != nil {
		t.Fatal("the stage kept replacing audio after the session closed")
	}
	if tb.active {
		t.Fatal("the stage stayed locked after the session closed")
	}
}
