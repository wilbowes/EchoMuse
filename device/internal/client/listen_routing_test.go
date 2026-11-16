package client

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// What of the wake stream leaves the device, per listen state
// (docs/listening.md). These are the privacy rules: in private listening
// nothing is sent until the Echo's own wake opens a session, a degraded Echo
// sends nothing at all, and nothing ever falls back to streaming. Before
// routeWakeFrame was split out of streamMic, no test failed when any of them
// broke (test-suite review 2026-11-16, mutants G07/G08).

type sent struct {
	plain   int
	session int
	ids     []uint32
}

func (s *sent) send(b []byte) { s.plain++ }
func (s *sent) sendListen(id uint32, b []byte) {
	s.session++
	s.ids = append(s.ids, id)
}

func newRoutingClient(t *testing.T) *DataClient {
	t.Helper()
	return NewDataClient("routing-test", newFanoutMic(), nil, aec.New(board.BiscuitMics().NumMics()))
}

func pushFrames(d *DataClient, s *sent, state string, start time.Time, n int) time.Time {
	at := start
	for i := 0; i < n; i++ {
		d.routeWakeFrame(state, make([]byte, vadOwwChunkBytes), at, s.send, s.sendListen)
		at = at.Add(80 * time.Millisecond)
	}
	return at
}

func TestStreamModeSendsEveryFrame(t *testing.T) {
	d := newRoutingClient(t)
	var s sent
	pushFrames(d, &s, ListenStream, time.Now(), 10)
	if s.plain != 10 || s.session != 0 {
		t.Fatalf("stream: sent %d plain, %d session frames; want 10, 0", s.plain, s.session)
	}
}

func TestLocalModeSendsNothingWithoutASession(t *testing.T) {
	d := newRoutingClient(t)
	d.SetListenState(ListenLocal)
	var s sent
	pushFrames(d, &s, ListenLocal, time.Now(), 50)
	if s.plain != 0 || s.session != 0 {
		t.Fatalf("private listening with no wake sent %d plain, %d session frames; "+
			"nothing may leave before the Echo's own wake", s.plain, s.session)
	}
}

func TestLocalModeSendsOnlyTheOpenSessionsAudio(t *testing.T) {
	d := newRoutingClient(t)
	d.SetListenState(ListenLocal)
	var s sent
	now := time.Now()
	at := pushFrames(d, &s, ListenLocal, now, 10)
	id, ok := d.OpenListen(at.Add(-80 * time.Millisecond))
	if !ok || id == 0 {
		t.Fatalf("OpenListen in private listening = (%d, %v); want a session", id, ok)
	}
	pushFrames(d, &s, ListenLocal, at, 5)
	if s.plain != 0 {
		t.Fatalf("a session's audio went out as %d plain stream frames", s.plain)
	}
	if s.session == 0 {
		t.Fatal("an open session sent nothing")
	}
	for _, got := range s.ids {
		if got != id {
			t.Fatalf("frame tagged session %d; want %d", got, id)
		}
	}
}

func TestDegradedSendsNothingEver(t *testing.T) {
	d := newRoutingClient(t)
	d.SetListenState(ListenDegraded)
	var s sent
	at := pushFrames(d, &s, ListenDegraded, time.Now(), 20)
	if _, ok := d.OpenListen(at); ok {
		t.Fatal("a degraded Echo opened a listening session")
	}
	pushFrames(d, &s, ListenDegraded, at, 20)
	if s.plain != 0 || s.session != 0 {
		t.Fatalf("degraded sent %d plain, %d session frames; a degraded Echo "+
			"is button-only and sends nothing", s.plain, s.session)
	}
}

func TestAnUnknownStateFailsClosed(t *testing.T) {
	d := newRoutingClient(t)
	var s sent
	for _, state := range []string{"", "Local", "streaming", "off"} {
		pushFrames(d, &s, state, time.Now(), 5)
	}
	if s.plain != 0 || s.session != 0 {
		t.Fatalf("an unknown listen state sent %d plain, %d session frames; "+
			"only %q may stream", s.plain, s.session, ListenStream)
	}
}

func TestOpenListenRefusedOutsidePrivateListening(t *testing.T) {
	d := newRoutingClient(t)
	for _, state := range []string{ListenStream, ListenDegraded} {
		d.SetListenState(state)
		if _, ok := d.OpenListen(time.Now()); ok {
			t.Errorf("OpenListen succeeded in %q", state)
		}
	}
}

func TestLeavingPrivateListeningClosesTheSession(t *testing.T) {
	d := newRoutingClient(t)
	d.SetListenState(ListenLocal)
	var s sent
	at := pushFrames(d, &s, ListenLocal, time.Now(), 10)
	if _, ok := d.OpenListen(at); !ok {
		t.Fatal("precondition: session opens")
	}
	d.SetListenState(ListenDegraded)
	if d.ListenOpen() {
		t.Fatal("a session stayed open after leaving private listening")
	}
	before := s.session
	pushFrames(d, &s, ListenDegraded, at, 10)
	if s.session != before || s.plain != 0 {
		t.Fatal("audio left after the session was closed by a state change")
	}
}
