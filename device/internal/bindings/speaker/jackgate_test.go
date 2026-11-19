package speaker

import (
	"sync"
	"testing"
	"time"
)

// The jack gate's ordering contract (#273), on biscuit's table, against a
// fake mixer. The one invariant behind every test: `plugged` (which lets the
// write loop play L != R) is never true while the internal speaker, which
// plays the right channel alone, can be on.

// fakeMixer holds control values and checks the invariant on every write.
type fakeMixer struct {
	t *testing.T
	g *jackGate

	mu       sync.Mutex
	state    map[string]string
	failSet  map[string]bool
	failRead map[string]bool
	writes   []string // "ctl=value", in order
	hook     func(w mixerWrite)
	broken   int // writes that turned the amp on with plugged true
}

func newFakeGate(t *testing.T) (*jackGate, *fakeMixer) {
	g := &jackGate{}
	f := &fakeMixer{t: t, g: g, state: map[string]string{}, failSet: map[string]bool{}, failRead: map[string]bool{}}
	g.write = f.write
	g.read = f.read
	return g, f
}

func (f *fakeMixer) write(w mixerWrite) error {
	f.mu.Lock()
	hook := f.hook
	f.mu.Unlock()
	if hook != nil {
		hook(w)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if w.Ctl == amp && w.Args[0] == "On" && f.g.plugged.Load() {
		f.broken++
	}
	f.writes = append(f.writes, w.Ctl+"="+w.Args[0])
	if f.failSet[w.Ctl] {
		return errFake
	}
	f.state[w.Ctl] = w.Args[0]
	return nil
}

func (f *fakeMixer) read(ctl string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failRead[ctl] {
		return "", errFake
	}
	v, ok := f.state[ctl]
	if !ok {
		return "", errFake
	}
	return v, nil
}

func (f *fakeMixer) get(ctl string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.state[ctl]
}

type fakeErr struct{}

func (fakeErr) Error() string { return "fake mixer: refused" }

var errFake error = fakeErr{}

// position stands in for the speaker's jackMu-guarded plug state.
type position struct {
	mu              sync.Mutex
	inserted, known bool
}

func (p *position) set(in bool) func() {
	return func() {
		p.mu.Lock()
		p.inserted, p.known = in, true
		p.mu.Unlock()
	}
}

func (p *position) get() (bool, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.inserted, p.known
}

func routeTo(g *jackGate, pos *position, in bool) []string {
	return g.route(in, pos.set(in), jackRouting(biscuitJack, in))
}

func TestRouteClearsPluggedFirstOnRemoval(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	routeTo(g, &pos, true)
	if !g.plugged.Load() {
		t.Fatal("not plugged after a clean insert")
	}
	f.hook = func(mixerWrite) {
		if g.plugged.Load() {
			t.Error("a removal write ran with plugged still true")
		}
	}
	updated := false
	g.route(false, func() {
		updated = true
		if g.plugged.Load() {
			t.Error("the removal's state update ran with plugged still true")
		}
	}, jackRouting(biscuitJack, false))
	if !updated || g.plugged.Load() || f.get(amp) != "On" || f.broken != 0 {
		t.Errorf("after removal: plugged=%v amp=%s broken=%d", g.plugged.Load(), f.get(amp), f.broken)
	}
}

func TestRouteSetsPluggedLastOnInsert(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	f.hook = func(mixerWrite) {
		if g.plugged.Load() {
			t.Error("plugged true before the insert's writes were done")
		}
	}
	if failed := routeTo(g, &pos, true); len(failed) != 0 {
		t.Fatalf("failed %v", failed)
	}
	if !g.plugged.Load() || f.get(amp) != "Off" {
		t.Errorf("after insert: plugged=%v amp=%s", g.plugged.Load(), f.get(amp))
	}
}

// A failed Insert write leaves the outputs unknown: no stereo, and the
// failing control is named.
func TestRouteWithholdsPluggedWhenAnInsertWriteFails(t *testing.T) {
	for _, ctl := range []string{amp, gain, mux} {
		g, f := newFakeGate(t)
		var pos position
		f.failSet[ctl] = true
		failed := routeTo(g, &pos, true)
		if g.plugged.Load() || len(failed) != 1 || failed[0] != ctl {
			t.Errorf("%s refused: plugged=%v failed=%v", ctl, g.plugged.Load(), failed)
		}
		// The rest of the set was still written.
		if len(f.writes) != len(biscuitJack.Insert) {
			t.Errorf("%s refused: %d of %d writes made", ctl, len(f.writes), len(biscuitJack.Insert))
		}
	}
}

// The race the review found: a reconcile that read "out" must not write the
// Remove set (amp ON) after an insert completed and set plugged. The insert
// waits for the reconcile, then puts the amp back off, and only then is
// plugged set.
func TestReconcileAndRouteAreSerialised(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	// Speaker on, plug out; then the HAL moves every control.
	routeTo(g, &pos, false)
	f.state[gain], f.state[mux], f.state[amp] = "0", "On", "Off"

	inserted := make(chan struct{})
	n, _, _ := g.reconcile(biscuitJack, func() (bool, bool) {
		in, known := pos.get() // "out", as the reconcile reads it
		go func() {
			routeTo(g, &pos, true) // the plug goes in meanwhile
			close(inserted)
		}()
		time.Sleep(20 * time.Millisecond) // the insert, if not held, would finish here
		return in, known
	})
	if n != 3 {
		t.Fatalf("reconcile rewrote %d controls, want 3", n)
	}
	select {
	case <-inserted:
	case <-time.After(5 * time.Second):
		t.Fatal("the insert never finished")
	}
	if f.broken != 0 {
		t.Fatalf("%d write(s) turned the speaker on with plugged true", f.broken)
	}
	if !g.plugged.Load() || f.get(amp) != "Off" {
		t.Errorf("after both: plugged=%v amp=%s, want plugged with the amp off", g.plugged.Load(), f.get(amp))
	}
	// The reconcile's Remove writes all came before the insert's.
	last := -1
	for i, w := range f.writes {
		if w == amp+"=On" {
			last = i
		}
	}
	if last < 0 || f.writes[len(f.writes)-1] != mux+"=On" || f.writes[last+1] != amp+"=Off" {
		t.Errorf("writes interleaved: %v", f.writes)
	}
}

// A reconcile about to write the Remove set clears plugged first, whatever
// it was.
func TestReconcileClearsPluggedBeforeTheRemoveSet(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	routeTo(g, &pos, true)
	pos.set(false)() // the position says out; plugged is still true
	f.hook = func(w mixerWrite) {
		if g.plugged.Load() {
			t.Errorf("reconcile wrote %s with plugged true", w.Ctl)
		}
	}
	g.reconcile(biscuitJack, pos.get)
	if g.plugged.Load() || f.get(amp) != "On" || f.broken != 0 {
		t.Errorf("plugged=%v amp=%s broken=%d", g.plugged.Load(), f.get(amp), f.broken)
	}
}

// A failed insert is recovered by a reconcile that puts the whole Insert set
// in place without an error; one that cannot read a control proves nothing.
func TestReconcileRecoversAFailedInsert(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	f.failSet[mux] = true
	f.state[mux] = "Off" // the control exists; its write is refused
	routeTo(g, &pos, true)
	if g.plugged.Load() {
		t.Fatal("plugged after a failed insert")
	}
	_, failed, permitted := g.reconcile(biscuitJack, pos.get)
	if g.plugged.Load() || permitted || len(failed) != 1 {
		t.Fatalf("still refused: plugged=%v permitted=%v failed=%v", g.plugged.Load(), permitted, failed)
	}

	f.failSet[mux] = false
	f.failRead[gain] = true
	if _, _, permitted := g.reconcile(biscuitJack, pos.get); permitted || g.plugged.Load() {
		t.Fatal("permitted with a control it could not read")
	}

	f.failRead[gain] = false
	n, failed, permitted := g.reconcile(biscuitJack, pos.get)
	if !permitted || !g.plugged.Load() || len(failed) != 0 {
		t.Errorf("recovered insert: n=%d failed=%v permitted=%v plugged=%v", n, failed, permitted, g.plugged.Load())
	}
	// Already plugged: a clean reconcile says nothing new.
	if _, _, again := g.reconcile(biscuitJack, pos.get); again {
		t.Error("a second clean reconcile reported a new permission")
	}
}

// Before any position is known the reconcile does nothing at all.
func TestReconcileWaitsForAPosition(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	if n, failed, permitted := g.reconcile(biscuitJack, pos.get); n != 0 || failed != nil || permitted || len(f.writes) != 0 {
		t.Errorf("unknown position: n=%d failed=%v permitted=%v writes=%v", n, failed, permitted, f.writes)
	}
}

// A removal that begins while a reconcile is confirming an insert wins: the
// reconcile does not leave plugged true behind it.
func TestRemovalDuringAReconcileWins(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	f.failSet[mux] = true
	routeTo(g, &pos, true) // plugged withheld
	f.failSet[mux] = false
	f.state[mux] = "Off" // readable, and wrong: the reconcile rewrites it

	removed := make(chan struct{})
	started := false
	f.hook = func(w mixerWrite) {
		if started {
			return
		}
		started = true
		go func() {
			routeTo(g, &pos, false)
			close(removed)
		}()
		for g.intent.Load() { // the removal has begun; it waits on the lock
			time.Sleep(time.Millisecond)
		}
	}
	g.reconcile(biscuitJack, pos.get)
	if !started {
		t.Fatal("the reconcile wrote nothing: the test did not overlap a removal")
	}
	if g.plugged.Load() {
		t.Error("plugged left true by a reconcile a removal overtook")
	}
	select {
	case <-removed:
	case <-time.After(5 * time.Second):
		t.Fatal("the removal never finished")
	}
	if g.plugged.Load() || f.get(amp) != "On" || f.broken != 0 {
		t.Errorf("after the removal: plugged=%v amp=%s broken=%d", g.plugged.Load(), f.get(amp), f.broken)
	}
}

// Start-up's amp on goes through the gate and clears plugged first.
func TestSpeakerOnClearsPlugged(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	routeTo(g, &pos, true)
	g.speakerOn([]mixerWrite{{Ctl: amp, Args: []string{"On"}}})
	if g.plugged.Load() || f.broken != 0 {
		t.Errorf("plugged=%v broken=%d", g.plugged.Load(), f.broken)
	}
}

// Plug changes and reconciles at random, concurrently, with the HAL moving
// controls under them: no write ever turns the speaker on while plugged is
// true, and when it all settles, plugged means the amp is off.
func TestJackGateHoldsUnderConcurrency(t *testing.T) {
	g, f := newFakeGate(t)
	var pos position
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 300; i++ {
			routeTo(g, &pos, i&1 == 0)
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 300; i++ {
			f.mu.Lock()
			f.state[amp] = map[bool]string{true: "On", false: "Off"}[i&2 == 0] // the HAL
			f.state[mux] = "Off"
			f.mu.Unlock()
			g.reconcile(biscuitJack, pos.get)
		}
	}()
	wg.Wait()
	if f.broken != 0 {
		t.Errorf("%d write(s) turned the speaker on with plugged true", f.broken)
	}
	g.reconcile(biscuitJack, pos.get)
	if in, _ := pos.get(); g.plugged.Load() != in || (g.plugged.Load() && f.get(amp) != "Off") {
		t.Errorf("settled: inserted=%v plugged=%v amp=%s", in, g.plugged.Load(), f.get(amp))
	}
}
