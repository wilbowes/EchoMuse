package speaker

import (
	"sync"
	"testing"
	"time"
)

type duckLog struct {
	mu  sync.Mutex
	set []float64
}

func (l *duckLog) fn(db float64) { l.mu.Lock(); l.set = append(l.set, db); l.mu.Unlock() }
func (l *duckLog) last() float64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.set[len(l.set)-1]
}

func TestLocalDuckIsReleasedIfNeverConfirmed(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Start(-18)
	if l.last() != -18 {
		t.Fatal("did not duck at the crossing")
	}
	time.Sleep(60 * time.Millisecond)
	if l.last() != 0 {
		t.Fatal("an unconfirmed duck must release itself")
	}
}

func TestAConfirmedDuckBelongsToTheController(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Start(-18)
	d.Confirm(true)
	d.Cancel() // a session close after confirmation must not un-duck the turn
	time.Sleep(60 * time.Millisecond)
	if l.last() != -18 {
		t.Fatalf("confirmed duck was released locally: %v", l.set)
	}
}

func TestSessionCloseReleasesAnUnconfirmedDuck(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, time.Hour)
	d.Start(-18)
	d.Cancel()
	if l.last() != 0 {
		t.Fatal("a ceded wake left the music ducked")
	}
}

// A barge-in: the wake crosses while the controller's duck is still on. The
// controller sends no second duck for the interrupting turn, so neither the
// barged session closing nor the hold running out may un-duck the music.
func TestAWakeUnderTheControllersDuckLeavesItAlone(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Start(-18)
	d.Confirm(true) // the controller took the first turn
	n := len(l.set)
	d.Start(-18) // wake word over the reply
	d.Cancel()   // the barged session closes
	time.Sleep(60 * time.Millisecond)
	if len(l.set) != n {
		t.Fatalf("the music's gain was touched under the controller's duck: %v", l.set)
	}
}

// Once the controller releases its duck, the next wake ducks locally again
// and is released again if nobody confirms it.
func TestLocalDuckWorksAgainAfterTheControllerReleases(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Confirm(true)
	d.Confirm(false)
	d.Start(-18)
	if l.last() != -18 {
		t.Fatal("did not duck at the crossing after the controller's release")
	}
	time.Sleep(60 * time.Millisecond)
	if l.last() != 0 {
		t.Fatal("an unconfirmed duck must release itself")
	}
}

// The link drops while the controller's duck is on. The controller sends no
// `duck off` for a turn it no longer has, so after Reset the next wake must
// duck locally again rather than wait for a release that never comes.
func TestALostLinkForgetsTheControllersDuck(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Confirm(true)
	d.Reset()
	d.Start(-18)
	if len(l.set) == 0 || l.last() != -18 {
		t.Fatalf("a wake after the link dropped did not duck: %v", l.set)
	}
	time.Sleep(60 * time.Millisecond)
	if l.last() != 0 {
		t.Fatal("an unconfirmed duck must release itself")
	}
}

// Reset must not strand an unconfirmed local duck: with the link gone, its
// own timer is the only thing left to release it.
func TestResetKeepsAnUnconfirmedDucksRelease(t *testing.T) {
	var l duckLog
	d := NewLocalDuck(l.fn, 20*time.Millisecond)
	d.Start(-18)
	d.Reset()
	time.Sleep(60 * time.Millisecond)
	if l.last() != 0 {
		t.Fatalf("an unconfirmed duck stayed on after the link dropped: %v", l.set)
	}
}
