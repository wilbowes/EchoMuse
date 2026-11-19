package speaker

import (
	"sync"
	"sync/atomic"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// jackGate serialises every mixer write that routes the jack, and owns
// `plugged`, the fact the write loop reads to let a period carry L != R
// (#273). Here rather than in pcm_speaker.go (build tag server) so its
// ordering is held by host tests; the mixer is injected.
//
// The contract, all of it for one reason: the Dot 2's internal speaker plays
// the RIGHT channel alone, so plugged must never be true while it can play.
//
//   - Removal clears plugged FIRST, before waiting for the lock and again
//     under it, before any write.
//   - Insert sets plugged LAST, after every write, and only if every write
//     returned nil. A failed write leaves the speaker's state unknown.
//   - Every write sequence (a plug change, the reconcile, the speaker's own
//     amp on) holds mu from its read of the desired position to its last
//     write, so a reconcile that read "out" cannot write the Remove set (amp
//     ON) after an insert has completed and set plugged.
//   - A reconcile about to write the Remove set clears plugged first. One
//     that finds the Insert set fully in place (every control read and
//     matched, or rewritten without error) may set it, which is how a failed
//     insert write is recovered.
//   - Any sequence that sets plugged withdraws it again if a removal has
//     begun meanwhile (intent), since that removal may be waiting on mu.
//
// Lock order: mu, then the speaker's jackMu (route's update and reconcile's
// desired take it under mu); nothing takes jackMu and then mu. The ALSA write
// goroutine takes neither: it only loads plugged.
type jackGate struct {
	mu      sync.Mutex
	intent  atomic.Bool // the latest position route was given: true = inserted
	plugged atomic.Bool

	write func(mixerWrite) error
	read  func(ctl string) (string, error)
}

// writeAll makes every write, in order, and names the controls that failed.
// It carries on past a failure: the rest of the set is still the best state
// to be in. Call with mu held.
func (g *jackGate) writeAll(ws []mixerWrite) (failed []string) {
	for _, w := range ws {
		if err := g.write(w); err != nil {
			failed = append(failed, w.Ctl)
		}
	}
	return failed
}

// permit sets plugged, then takes it back if a removal has begun since this
// sequence read the position. A removal stores intent BEFORE it clears
// plugged, so either its clear comes after this store and wins, or its
// intent is already visible here and this withdraws the store. Between the
// two, plugged can read true for an instant, with the Insert set (amp off)
// in place. Call with mu held, after a complete, error-free Insert set.
func (g *jackGate) permit() {
	g.plugged.Store(true)
	if !g.intent.Load() {
		g.plugged.Store(false)
	}
}

// route applies a plug position: update (the speaker's own state, under
// jackMu) and then ws, the position's writes, all under mu. It returns the
// controls whose writes failed; plugged is set only when inserted and there
// are none.
func (g *jackGate) route(inserted bool, update func(), ws []mixerWrite) (failed []string) {
	g.intent.Store(inserted)
	if !inserted {
		g.plugged.Store(false) // before the wait for mu
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if !inserted {
		g.plugged.Store(false) // a reconcile holding mu may have set it
	}
	if update != nil {
		update()
	}
	failed = g.writeAll(ws)
	if inserted && len(failed) == 0 {
		g.permit()
	}
	return failed
}

// reconcile reads the desired position's controls back and rewrites the ones
// that moved (jackRoutingDrift), holding mu from desired() to the last write.
// Nothing before a position is known. It reports how many controls it
// rewrote, which writes failed, and whether it set plugged (a recovered
// insert).
func (g *jackGate) reconcile(j board.Jack, desired func() (inserted, known bool)) (rewritten int, failed []string, permitted bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	inserted, known := desired()
	if !known {
		return 0, nil, false
	}
	if !inserted {
		g.plugged.Store(false) // the Remove set turns the speaker on
	}
	current := map[string]string{}
	complete := true
	for _, w := range jackRouting(j, inserted) {
		if v, err := g.read(w.Ctl); err == nil {
			current[w.Ctl] = v
		} else {
			complete = false // failure to look is not proof of the state
		}
	}
	drift := jackRoutingDrift(j, inserted, current)
	failed = g.writeAll(drift)
	if inserted && len(failed) > 0 {
		// A drifted Insert control we could not put back: the speaker may be
		// on, so stereo goes until a later reconcile confirms the whole set.
		g.plugged.Store(false)
	}
	if inserted && complete && len(failed) == 0 && !g.plugged.Load() {
		g.permit()
		permitted = g.plugged.Load()
	}
	return len(drift), failed, permitted
}

// speakerOn makes writes that may turn the internal speaker on outside a
// plug change (start-up's amp on): plugged is cleared first, and the writes
// are serialised with the rest.
func (g *jackGate) speakerOn(ws []mixerWrite) (failed []string) {
	g.plugged.Store(false)
	g.mu.Lock()
	defer g.mu.Unlock()
	g.plugged.Store(false)
	return g.writeAll(ws)
}
