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
	d.Confirm()
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
