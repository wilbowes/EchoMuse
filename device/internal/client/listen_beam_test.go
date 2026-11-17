package client

import (
	"sync/atomic"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/listen"
)

// The session owns the beam lock: locked at the Echo's own wake, released
// when the session closes, by the controller or by the device. Held until
// the reply ended, it made barge-ins land on the last command's perimeter
// mic (C95, 2026-11-17).
func TestListenSessionOwnsTheBeamLock(t *testing.T) {
	open := func(t *testing.T) (*DataClient, uint32) {
		d := newRoutingClient(t)
		d.SetListenState(ListenLocal)
		var s sent
		at := pushFrames(d, &s, ListenLocal, time.Now(), 10)
		id, ok := d.OpenListen(at.Add(-80 * time.Millisecond))
		if !ok {
			t.Fatal("OpenListen refused in private listening")
		}
		if got := atomic.LoadInt32(&d.beamReq); got != beamReqLock {
			t.Fatalf("after the wake, beam request = %d; want lock (%d)", got, beamReqLock)
		}
		atomic.StoreInt32(&d.beamReq, beamReqNone) // the mic stream consumed it
		return d, id
	}

	t.Run("controller closes", func(t *testing.T) {
		d, id := open(t)
		if d.CloseListen(id + 1) {
			t.Fatal("closed a session that is not the open one")
		}
		if got := atomic.LoadInt32(&d.beamReq); got != beamReqNone {
			t.Fatalf("a stale listen_close moved the beam: request %d", got)
		}
		if !d.CloseListen(id) {
			t.Fatal("listen_close for the open session refused")
		}
		if got := atomic.LoadInt32(&d.beamReq); got != beamReqUnlock {
			t.Fatalf("after listen_close, beam request = %d; want unlock (%d)", got, beamReqUnlock)
		}
	})
	t.Run("device closes", func(t *testing.T) {
		d, _ := open(t)
		d.CloseAnyListen(listen.ReasonMuted)
		if got := atomic.LoadInt32(&d.beamReq); got != beamReqUnlock {
			t.Fatalf("after the device closed it, beam request = %d; want unlock (%d)", got, beamReqUnlock)
		}
	})
	t.Run("words inside a session", func(t *testing.T) {
		d, _ := open(t)
		if _, ok := d.OpenListen(time.Now()); ok {
			t.Fatal("a second wake opened a second session")
		}
		if got := atomic.LoadInt32(&d.beamReq); got != beamReqNone {
			t.Fatalf("a refused wake moved the beam: request %d", got)
		}
	})
}
