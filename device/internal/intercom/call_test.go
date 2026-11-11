package intercom

import (
	"errors"
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
	mu           sync.Mutex
	frames       [][]byte
	flushed      int
	endedStreams int
	intercomMode bool
	err          error
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

func (f *fakeSpeaker) EndStream() {
	f.mu.Lock()
	f.endedStreams++
	f.mu.Unlock()
}

func (f *fakeSpeaker) SetIntercomMode(on bool) {
	f.mu.Lock()
	f.intercomMode = on
	f.mu.Unlock()
}

// mode reports the last intercom mode the call set: true while peer
// audio is flowing on the low prime.
func (f *fakeSpeaker) mode() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.intercomMode
}

// ends reports how many EndStream calls arrived.
func (f *fakeSpeaker) ends() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.endedStreams
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

// twoCalls is a pair of connected calls for flow tests.
type twoCalls struct {
	caller, callee *Call
	mCaller        *Machine
	mCallee        *Machine
	micCaller      *fakeMic
	micCallee      *fakeMic
	spkCaller      *fakeSpeaker
	spkCallee      *fakeSpeaker
}

// connectTwoCalls offers, starts and answers a call between two
// devices, and waits until both ends report connected. The caller
// hangs up at the end of the test via t.Cleanup.
func connectTwoCalls(t *testing.T) *twoCalls {
	t.Helper()
	const sessionID, token = "s1", "tok"
	tc := &twoCalls{
		mCaller:   NewMachine(),
		mCallee:   NewMachine(),
		micCaller: &fakeMic{},
		micCallee: &fakeMic{},
		spkCaller: &fakeSpeaker{},
		spkCallee: &fakeSpeaker{},
	}
	if err := tc.mCaller.Offer(Session{ID: sessionID, Role: RoleCaller}); err != nil {
		t.Fatalf("caller Offer: %v", err)
	}
	if err := tc.mCallee.Offer(Session{ID: sessionID, Token: token, Role: RoleCallee}); err != nil {
		t.Fatalf("callee Offer: %v", err)
	}
	tc.caller = NewCall("caller", Session{ID: sessionID, Token: token, Role: RoleCaller}, tc.mCaller, tc.micCaller, tc.spkCaller)
	tc.callee = NewCall("callee", Session{ID: sessionID, Token: token, Role: RoleCallee}, tc.mCallee, tc.micCallee, tc.spkCallee)
	tc.caller.Port = freePort(t)
	tc.callee.Port = freePort(t)
	if err := tc.caller.Start(); err != nil {
		t.Fatalf("caller Start: %v", err)
	}
	if err := tc.callee.Start(); err != nil {
		t.Fatalf("callee Start: %v", err)
	}
	// The callee dials the caller on answer.
	tc.callee.sess.PeerAddr = peerAddr(t, tc.caller)
	tc.caller.sess.PeerAddr = peerAddr(t, tc.callee)

	connectedCaller := make(chan struct{}, 1)
	connectedCallee := make(chan struct{}, 1)
	tc.caller.OnConnected = func() { connectedCaller <- struct{}{} }
	tc.callee.OnConnected = func() { connectedCallee <- struct{}{} }

	tc.callee.Answer()
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
	if got := tc.mCaller.State(); got != StateInCall {
		t.Fatalf("caller state = %q, want in_call", got)
	}
	if got := tc.mCallee.State(); got != StateInCall {
		t.Fatalf("callee state = %q, want in_call", got)
	}
	t.Cleanup(func() {
		tc.caller.Stop()
		tc.callee.Stop()
	})
	return tc
}

// bigFrame is one production-sized mic frame (2560 bytes, 1280
// samples): first sample 0, the rest 3000 — so the resampled output
// starts with the same ramp the small-frame test asserted.
func bigFrame() []byte {
	samples := make([]int16, 1280)
	for i := range samples {
		samples[i] = 3000
	}
	samples[0] = 0
	out := make([]byte, 0, 2560)
	for _, s := range samples {
		out = append(out, byte(s), byte(s>>8))
	}
	return out
}

func TestCallFullFlow(t *testing.T) {
	tc := connectTwoCalls(t)
	mCaller, mCallee := tc.mCaller, tc.mCallee
	cCaller, cCallee := tc.caller, tc.callee
	micCaller, micCallee := tc.micCaller, tc.micCallee
	spkCaller, spkCallee := tc.spkCaller, tc.spkCallee

	// Audio flows both ways: a mic frame on one end arrives
	// resampled (3x) on the other's speaker. Three production-sized
	// frames prime the receiver's jitter buffer (~170ms); the first
	// bytes out are the resampled ramp of the first frame.
	frame := bigFrame()
	for i := 0; i < 3; i++ {
		micCallee.emit(frame)
	}
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
	for i := 0; i < 3; i++ {
		micCaller.emit(frame)
	}
	waitFor(t, "callee to hear caller", func() bool {
		return len(spkCallee.bytes()) >= 18
	})

	// Hanging up one end ends the other with peer_disconnected, and
	// both machines return to idle.
	endedCallee := make(chan string, 1)
	cCallee.OnEnded = func(reason string) { endedCallee <- reason }
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
}

func TestCallSuspendResume(t *testing.T) {
	tc := connectTwoCalls(t)
	// The low prime is on from the moment audio flows.
	if !tc.spkCaller.mode() || !tc.spkCallee.mode() {
		t.Fatalf("intercom mode not set on connect (caller=%v callee=%v)",
			tc.spkCaller.mode(), tc.spkCallee.mode())
	}
	// Audio flows before the suspend.
	frame := bigFrame()
	for i := 0; i < 3; i++ {
		tc.micCallee.emit(frame)
	}
	waitFor(t, "caller to hear callee before suspend", func() bool {
		return len(tc.spkCaller.bytes()) >= 18
	})
	heardBefore := len(tc.spkCaller.bytes())

	// Suspend: the machine leaves in_call, the mic hook comes off,
	// the voice plane is flushed and its prime restored.
	if err := tc.caller.Suspend(); err != nil {
		t.Fatalf("Suspend: %v", err)
	}
	if got := tc.mCaller.State(); got != StateSuspended {
		t.Fatalf("caller state = %q, want suspended", got)
	}
	if tc.spkCaller.mode() {
		t.Fatal("intercom mode still on after suspend — the turn's TTS would ride the low prime")
	}
	if tc.spkCaller.ends() < 1 {
		t.Fatal("Suspend did not end the voice stream — queued peer audio would play over the turn")
	}
	// Peer audio sent during the suspend is drained, never played,
	// and the peer's sender does not end the call over it.
	for i := 0; i < 3; i++ {
		tc.micCallee.emit(frame)
	}
	time.Sleep(300 * time.Millisecond)
	if got := len(tc.spkCaller.bytes()); got != heardBefore {
		t.Fatalf("caller heard %d bytes while suspended, want %d — peer audio leaked into the turn",
			got, heardBefore)
	}
	if got := tc.mCallee.State(); got != StateInCall {
		t.Fatalf("callee state = %q, want in_call — the suspend must not end the peer's call", got)
	}

	// A second suspend is refused: the call is not flowing.
	if err := tc.caller.Suspend(); err == nil {
		t.Fatal("second Suspend succeeded — want ErrNotInCall")
	}

	// Resume: the machine flows again, the low prime is back, and
	// audio flows again (the cushion rebuilds first).
	if err := tc.caller.Resume(); err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if got := tc.mCaller.State(); got != StateInCall {
		t.Fatalf("caller state = %q, want in_call", got)
	}
	if !tc.spkCaller.mode() {
		t.Fatal("intercom mode not restored on resume")
	}
	for i := 0; i < 3; i++ {
		tc.micCallee.emit(frame)
	}
	waitFor(t, "caller to hear callee after resume", func() bool {
		return len(tc.spkCaller.bytes()) > heardBefore
	})

	// Resume without a suspend is refused.
	if err := tc.caller.Resume(); err == nil {
		t.Fatal("Resume without suspend succeeded — want ErrNotSuspended")
	}
}

func TestCallSuspendResumeRefused(t *testing.T) {
	m := NewMachine()
	if err := m.Offer(Session{ID: "s1", Role: RoleCallee, Token: "tok"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	c := NewCall("callee", Session{ID: "s1", Token: "tok", Role: RoleCallee}, m, &fakeMic{}, &fakeSpeaker{})
	c.Port = freePort(t)
	if err := c.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()
	// Ringing is not flowing: suspend and resume both refuse.
	if err := c.Suspend(); !errors.Is(err, ErrNotInCall) {
		t.Fatalf("Suspend while ringing = %v, want ErrNotInCall", err)
	}
	if err := c.Resume(); !errors.Is(err, ErrNotSuspended) {
		t.Fatalf("Resume while ringing = %v, want ErrNotSuspended", err)
	}
}

func TestJitterBuffer(t *testing.T) {
	period := func(b byte) []byte {
		p := make([]byte, intercomPeriodBytes)
		for i := range p {
			p[i] = b
		}
		return p
	}
	var jb jitterBuffer
	// Below the target, nothing pumps — the cushion is still building.
	// A partial period counts toward it: three full periods after a
	// 1000-byte head still leave it short.
	if out := jb.write(period(0)[:1000]); len(out) != 0 {
		t.Fatalf("write below target pumped %d periods", len(out))
	}
	for i := 1; i <= 3; i++ {
		if out := jb.write(period(byte(i))); len(out) != 0 {
			t.Fatalf("write %d pumped %d periods before the target", i, len(out))
		}
	}
	jb.reset()
	// Whole periods: the fourth reaches the 4-period target and all
	// four pump at once, in order.
	var out [][]byte
	for i := 0; i < 4; i++ {
		out = jb.write(period(byte(i)))
	}
	if len(out) != 4 {
		t.Fatalf("reaching the target pumped %d periods, want 4", len(out))
	}
	for i, p := range out {
		if p[0] != byte(i) {
			t.Fatalf("pumped period %d starts with %d, want %d — order lost", i, p[0], i)
		}
	}
	// Steady state: one period in, one period out.
	if out := jb.write(period(5)); len(out) != 1 || out[0][0] != 5 {
		t.Fatalf("steady-state write pumped %d periods, want [5]", len(out))
	}
	// A flood cannot grow the latency without limit: ten periods in
	// one write drop the oldest six and pump the newest four.
	var flood []byte
	for i := 10; i < 20; i++ {
		flood = append(flood, period(byte(i))...)
	}
	out = jb.write(flood)
	if len(out) != 4 {
		t.Fatalf("flood pumped %d periods, want 4", len(out))
	}
	for i, p := range out {
		if want := byte(16 + i); p[0] != want {
			t.Fatalf("flood period %d starts with %d, want %d — the oldest were not dropped", i, p[0], want)
		}
	}
	// Reset drops the cushion: nothing pumps until the target rebuilds.
	jb.reset()
	if out := jb.write(period(30)); len(out) != 0 {
		t.Fatalf("write after reset pumped %d periods", len(out))
	}
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
