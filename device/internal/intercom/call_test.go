package intercom

import (
	"net"
	"sync"
	"testing"
	"time"
)

// fakeMic is a MicTap whose hook the test drives by hand.
type fakeMic struct {
	mu   sync.Mutex
	hook func([]byte)
}

func (f *fakeMic) OnProcessedMic(cb func([]byte)) {
	f.mu.Lock()
	f.hook = cb
	f.mu.Unlock()
}

func (f *fakeMic) emit(frame []byte) {
	f.mu.Lock()
	h := f.hook
	f.mu.Unlock()
	if h != nil {
		h(frame)
	}
}

// fakeSpeaker records every PumpPeriod payload.
type fakeSpeaker struct {
	mu      sync.Mutex
	frames  [][]byte
	flushed int
	err     error
}

func (f *fakeSpeaker) PumpPeriod(p []byte) error {
	f.mu.Lock()
	cp := append([]byte(nil), p...)
	f.frames = append(f.frames, cp)
	err := f.err
	f.mu.Unlock()
	return err
}

func (f *fakeSpeaker) Flush() {
	f.mu.Lock()
	f.flushed++
	f.mu.Unlock()
}

// bytes returns all pumped audio concatenated.
func (f *fakeSpeaker) bytes() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []byte
	for _, fr := range f.frames {
		out = append(out, fr...)
	}
	return out
}

// freePort returns a currently-free localhost TCP port.
func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("freePort listen: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

// peerAddr is the dial address for a started Call.
func peerAddr(t *testing.T, c *Call) string {
	t.Helper()
	addr := c.ListenerAddr()
	if addr == "" {
		t.Fatal("ListenerAddr is empty — Start did not run")
	}
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("SplitHostPort(%q): %v", addr, err)
	}
	return "127.0.0.1:" + port
}

// waitFor fails the test unless cond becomes true before the deadline.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestCallFullFlow(t *testing.T) {
	const sessionID, token = "s1", "tok"
	mCaller := NewMachine()
	mCallee := NewMachine()
	if err := mCaller.Offer(Session{ID: sessionID, Role: RoleCaller}); err != nil {
		t.Fatalf("caller Offer: %v", err)
	}
	if err := mCallee.Offer(Session{ID: sessionID, Token: token, Role: RoleCallee}); err != nil {
		t.Fatalf("callee Offer: %v", err)
	}

	micCaller, spkCaller := &fakeMic{}, &fakeSpeaker{}
	micCallee, spkCallee := &fakeMic{}, &fakeSpeaker{}
	cCaller := NewCall("caller", Session{ID: sessionID, Token: token, Role: RoleCaller}, mCaller, micCaller, spkCaller)
	cCallee := NewCall("callee", Session{ID: sessionID, Token: token, Role: RoleCallee}, mCallee, micCallee, spkCallee)
	cCaller.Port = freePort(t)
	cCallee.Port = freePort(t)
	if err := cCaller.Start(); err != nil {
		t.Fatalf("caller Start: %v", err)
	}
	if err := cCallee.Start(); err != nil {
		t.Fatalf("callee Start: %v", err)
	}
	// The callee dials the caller on answer.
	cCallee.sess.PeerAddr = peerAddr(t, cCaller)
	cCaller.sess.PeerAddr = peerAddr(t, cCallee)

	connectedCaller := make(chan struct{}, 1)
	connectedCallee := make(chan struct{}, 1)
	endedCallee := make(chan string, 1)
	cCaller.OnConnected = func() { connectedCaller <- struct{}{} }
	cCallee.OnConnected = func() { connectedCallee <- struct{}{} }
	cCallee.OnEnded = func(reason string) { endedCallee <- reason }

	cCallee.Answer()
	select {
	case <-connectedCaller:
	case <-time.After(5 * time.Second):
		t.Fatal("caller never connected")
	}
	select {
	case <-connectedCallee:
	case <-time.After(5 * time.Second):
		t.Fatal("callee never connected")
	}
	if got := mCaller.State(); got != StateInCall {
		t.Fatalf("caller state = %q, want in_call", got)
	}
	if got := mCallee.State(); got != StateInCall {
		t.Fatalf("callee state = %q, want in_call", got)
	}

	// Audio flows both ways: a mic frame on one end arrives
	// resampled (3x) on the other's speaker.
	frame := s16(0, 3000, 3000, 3000)
	micCallee.emit(frame)
	waitFor(t, "caller to hear callee", func() bool {
		return len(spkCaller.bytes()) >= 18
	})
	if got := spkCaller.bytes(); len(got) < 18 {
		t.Fatalf("caller heard %d bytes, want >= 18", len(got))
	} else {
		want := s16(0, 1000, 2000, 3000, 3000, 3000, 3000, 3000, 3000)
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("caller audio byte %d = %d, want %d (got %v)", i, got[i], want[i], got16(t, got[:18]))
			}
		}
	}
	micCaller.emit(frame)
	waitFor(t, "callee to hear caller", func() bool {
		return len(spkCallee.bytes()) >= 18
	})

	// Hanging up one end ends the other with peer_disconnected, and
	// both machines return to idle.
	cCaller.Stop()
	select {
	case reason := <-endedCallee:
		if reason != "peer_disconnected" {
			t.Fatalf("callee ended with %q, want peer_disconnected", reason)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("callee never ended after caller hung up")
	}
	if got := mCallee.State(); got != StateIdle {
		t.Fatalf("callee state after hangup = %q, want idle", got)
	}
	if got := mCaller.State(); got != StateIdle {
		t.Fatalf("caller state after hangup = %q, want idle", got)
	}
	cCallee.Stop()
}

func TestCallRingTimeout(t *testing.T) {
	m := NewMachine()
	if err := m.Offer(Session{ID: "s1", Role: RoleCallee, Token: "tok"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	c := NewCall("callee", Session{ID: "s1", Token: "tok", Role: RoleCallee}, m, &fakeMic{}, &fakeSpeaker{})
	c.Port = freePort(t)
	c.RingTimeout = 50 * time.Millisecond
	ended := make(chan string, 1)
	c.OnEnded = func(reason string) { ended <- reason }
	if err := c.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	select {
	case reason := <-ended:
		if reason != "no_answer" {
			t.Fatalf("ended with %q, want no_answer", reason)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ring timeout never fired")
	}
	if got := m.State(); got != StateIdle {
		t.Fatalf("state after timeout = %q, want idle", got)
	}
}

func TestCallDialBadToken(t *testing.T) {
	mCaller := NewMachine()
	mCallee := NewMachine()
	if err := mCaller.Offer(Session{ID: "s1", Role: RoleCaller}); err != nil {
		t.Fatalf("caller Offer: %v", err)
	}
	if err := mCallee.Offer(Session{ID: "s1", Token: "tok", Role: RoleCallee}); err != nil {
		t.Fatalf("callee Offer: %v", err)
	}
	cCaller := NewCall("caller", Session{ID: "s1", Token: "tok", Role: RoleCaller}, mCaller, &fakeMic{}, &fakeSpeaker{})
	cCallee := NewCall("callee", Session{ID: "s1", Token: "WRONG", Role: RoleCallee}, mCallee, &fakeMic{}, &fakeSpeaker{})
	cCaller.Port = freePort(t)
	cCallee.Port = freePort(t)
	if err := cCaller.Start(); err != nil {
		t.Fatalf("caller Start: %v", err)
	}
	if err := cCallee.Start(); err != nil {
		t.Fatalf("callee Start: %v", err)
	}
	cCallee.sess.PeerAddr = peerAddr(t, cCaller)
	ended := make(chan string, 1)
	cCallee.OnEnded = func(reason string) { ended <- reason }
	cCallee.Answer()
	select {
	case reason := <-ended:
		if reason != "dial_failed" {
			t.Fatalf("ended with %q, want dial_failed", reason)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("bad-token dial never ended")
	}
	// The caller is still waiting — its listener rejected the
	// handshake but the attempt is alive until it times out or ends.
	if got := mCaller.State(); got != StateCalling {
		t.Fatalf("caller state = %q, want calling", got)
	}
	cCaller.Stop()
	cCallee.Stop()
}

func TestCallAnswerOnlyWhenRinging(t *testing.T) {
	// The caller must never dial: Answer is a callee-only action.
	m := NewMachine()
	if err := m.Offer(Session{ID: "s1", Role: RoleCaller, Token: "tok"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	c := NewCall("caller", Session{ID: "s1", Token: "tok", Role: RoleCaller}, m, &fakeMic{}, &fakeSpeaker{})
	c.Port = freePort(t)
	if err := c.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	connected := make(chan struct{}, 1)
	c.OnConnected = func() { connected <- struct{}{} }
	c.sess.PeerAddr = "127.0.0.1:1" // nothing there; a dial would fail loudly
	c.Answer()
	select {
	case <-connected:
		t.Fatal("caller connected after Answer — the caller must wait to be dialled")
	case <-time.After(200 * time.Millisecond):
	}
	if got := m.State(); got != StateCalling {
		t.Fatalf("state = %q, want calling", got)
	}
	c.Stop()
}

func TestCallSpeakerOnlyDevice(t *testing.T) {
	// mic == nil: the call still connects and still plays peer audio.
	mCaller := NewMachine()
	mCallee := NewMachine()
	if err := mCaller.Offer(Session{ID: "s1", Role: RoleCaller}); err != nil {
		t.Fatalf("caller Offer: %v", err)
	}
	if err := mCallee.Offer(Session{ID: "s1", Token: "tok", Role: RoleCallee}); err != nil {
		t.Fatalf("callee Offer: %v", err)
	}
	spkCallee := &fakeSpeaker{}
	cCaller := NewCall("caller", Session{ID: "s1", Token: "tok", Role: RoleCaller}, mCaller, &fakeMic{}, &fakeSpeaker{})
	cCallee := NewCall("callee", Session{ID: "s1", Token: "tok", Role: RoleCallee}, mCallee, nil, spkCallee)
	cCaller.Port = freePort(t)
	cCallee.Port = freePort(t)
	if err := cCaller.Start(); err != nil {
		t.Fatalf("caller Start: %v", err)
	}
	if err := cCallee.Start(); err != nil {
		t.Fatalf("callee Start: %v", err)
	}
	cCallee.sess.PeerAddr = peerAddr(t, cCaller)
	connected := make(chan struct{}, 1)
	cCallee.OnConnected = func() { connected <- struct{}{} }
	cCallee.Answer()
	select {
	case <-connected:
	case <-time.After(5 * time.Second):
		t.Fatal("speaker-only callee never connected")
	}
	cCaller.Stop()
	cCallee.Stop()
}

func TestCallStopIsSilent(t *testing.T) {
	// Stop tears down without OnEnded: the caller reports the state
	// itself, and a second Stop is a no-op.
	m := NewMachine()
	if err := m.Offer(Session{ID: "s1", Role: RoleCallee, Token: "tok"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	c := NewCall("callee", Session{ID: "s1", Token: "tok", Role: RoleCallee}, m, &fakeMic{}, &fakeSpeaker{})
	c.Port = freePort(t)
	ended := make(chan string, 1)
	c.OnEnded = func(reason string) { ended <- reason }
	if err := c.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	c.Stop()
	c.Stop()
	select {
	case reason := <-ended:
		t.Fatalf("Stop invoked OnEnded(%q) — Stop must stay silent", reason)
	case <-time.After(200 * time.Millisecond):
	}
	if got := m.State(); got != StateIdle {
		t.Fatalf("state after Stop = %q, want idle", got)
	}
}
