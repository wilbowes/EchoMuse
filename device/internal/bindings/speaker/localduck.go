package speaker

import (
	"log"
	"sync"
	"time"
)

// LocalDuck ducks music at the device's OWN wake crossing, so the duck lands
// with the listening ring the device lights at the same moment (#263). The
// controller's duck used to be the only one, arriving a round trip later —
// 48-68ms on a good link, seconds on a bad one.
//
// The controller stays the owner of the turn. Its `duck` message confirms or
// releases, and after that this does nothing. What this adds is the case the
// controller never answers for this device — the wake was ceded to another
// Echo, or the link is gone — so a local duck must end by itself: when the
// session closes unconfirmed, or after LocalDuckHold, whichever comes first.
type LocalDuck struct {
	set  func(db float64) // PcmSpeaker.SetDuck
	hold time.Duration

	mu    sync.Mutex
	timer *time.Timer // non-nil while ducked locally and unconfirmed
}

// LocalDuckHold bounds an unconfirmed local duck. Far above a normal
// controller answer, short enough that a ceded wake costs a few seconds of
// quiet music rather than a stuck duck.
const LocalDuckHold = 5 * time.Second

func NewLocalDuck(set func(db float64), hold time.Duration) *LocalDuck {
	return &LocalDuck{set: set, hold: hold}
}

// Start ducks now and arms the release.
func (d *LocalDuck) Start(db float64) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.timer != nil {
		d.timer.Stop()
	}
	d.set(db)
	var t *time.Timer
	t = time.AfterFunc(d.hold, func() {
		d.mu.Lock()
		defer d.mu.Unlock()
		if d.timer != t {
			return
		}
		d.timer = nil
		d.set(0)
		log.Println("[speaker] local duck released — the controller never confirmed it")
	})
	d.timer = t
}

// Confirm hands the duck to the controller: its own on/off follows.
func (d *LocalDuck) Confirm() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.timer != nil {
		d.timer.Stop()
		d.timer = nil
	}
}

// Cancel releases an UNCONFIRMED local duck — the session closed without the
// controller taking the turn. A confirmed duck is the controller's to end.
func (d *LocalDuck) Cancel() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.timer == nil {
		return
	}
	d.timer.Stop()
	d.timer = nil
	d.set(0)
	log.Println("[speaker] local duck released — session closed without a turn")
}
