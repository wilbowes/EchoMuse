package avdtp

import (
	"errors"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var (
	sbcCaps   = Codec{MediaAudio, CodecSBC, []byte{0xFF, 0xFF, 0x02, 0x35}}
	sbcChosen = Codec{MediaAudio, CodecSBC, []byte{0x21, 0x15, 0x02, 0x35}}
)

func choose(Codec) (Codec, error) { return sbcChosen, nil }

// wire carries one direction, in order, after an optional latency, and
// counts the commands it carried by signal.
type wire struct {
	ch      chan []byte
	latency time.Duration
	mu      sync.Mutex
	cmds    map[SignalID]int
	reasm   Reassembler
}

func newWire(latency time.Duration) *wire {
	return &wire{ch: make(chan []byte, 256), latency: latency, cmds: map[SignalID]int{}}
}

func (w *wire) Send(p []byte) error {
	w.mu.Lock()
	if m, ok, _ := w.reasm.Feed(p); ok && m.Type == MsgCommand {
		w.cmds[m.Signal]++
	}
	w.mu.Unlock()
	w.ch <- append([]byte(nil), p...)
	return nil
}

func (w *wire) count(sig SignalID) int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.cmds[sig]
}

func (w *wire) deliver(to *Session) {
	for p := range w.ch {
		if w.latency > 0 {
			time.Sleep(w.latency)
		}
		to.Feed(p)
	}
}

// pair wires a source and a sink together.
func pair(t *testing.T, src, snk Config, latency time.Duration) (*Session, *Session, *wire, *wire) {
	t.Helper()
	ab, ba := newWire(latency), newWire(latency)
	src.Signal, snk.Signal = ab, ba
	snk.Type = SEPSink
	for _, c := range []*Config{&src, &snk} {
		if c.Codec.Info == nil {
			c.Codec = sbcCaps
		}
		if c.Select == nil {
			c.Select = choose
		}
		if c.Timeout == 0 {
			c.Timeout = time.Second
		}
	}
	a, b := NewSession(src), NewSession(snk)
	go ab.deliver(b)
	go ba.deliver(a)
	t.Cleanup(func() { close(ab.ch); close(ba.ch) })
	return a, b, ab, ba
}

// eventually waits for a condition the peer's goroutine settles.
func eventually(t *testing.T, what string, f func() bool) {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
		if f() {
			return
		}
	}
	t.Fatalf("never: %s", what)
}

func TestSourceInitiatesTheWholeStream(t *testing.T) {
	var states []State
	var smu sync.Mutex
	var delay atomic.Uint32
	src := Config{
		DelayReporting: true,
		OnState: func(s State) {
			smu.Lock()
			states = append(states, s)
			smu.Unlock()
		},
		OnDelay: func(d uint16) { delay.Store(uint32(d)) },
	}
	a, b, ab, _ := pair(t, src, Config{DelayReporting: true}, 0)
	if err := a.Connect(); err != nil {
		t.Fatal(err)
	}
	eventually(t, "sink open", func() bool { return b.State() == StateOpen })
	if a.State() != StateOpen || ab.count(SigGetAllCapabilities) != 1 || ab.count(SigGetCapabilities) != 0 {
		t.Fatalf("state %v, caps commands %d/%d", a.State(), ab.count(SigGetAllCapabilities), ab.count(SigGetCapabilities))
	}
	for _, s := range []*Session{a, b} {
		if c, ok := s.Configuration(); !ok || !reflect.DeepEqual(c, sbcChosen) {
			t.Fatalf("configuration %+v", c)
		}
	}
	if a.RemoteSEID() != 1 || b.RemoteSEID() != 1 {
		t.Fatal("remote SEIDs not recorded")
	}

	if err := b.ReportDelay(1500); err != nil {
		t.Fatal(err)
	}
	if d, ok := a.Delay(); !ok || d != 1500 || delay.Load() != 1500 {
		t.Fatalf("delay %d %v (callback %d)", d, ok, delay.Load())
	}

	steps := []struct {
		do   func() error
		want State
	}{{a.Start, StateStreaming}, {a.Suspend, StateOpen}, {a.Start, StateStreaming}, {a.Close, StateIdle}}
	for i, st := range steps {
		if err := st.do(); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
		eventually(t, "sink follows", func() bool { return b.State() == st.want })
		if a.State() != st.want {
			t.Fatalf("step %d: source %v", i, a.State())
		}
	}
	smu.Lock()
	defer smu.Unlock()
	want := []State{StateConfigured, StateOpen, StateStreaming, StateOpen, StateStreaming, StateIdle}
	if !reflect.DeepEqual(states, want) {
		t.Fatalf("OnState %v", states)
	}
}

// Many speakers configure the source themselves on connect.
func TestSourceAcceptsASinkThatInitiates(t *testing.T) {
	var accepted atomic.Bool
	src := Config{Accept: func(c Codec) error { accepted.Store(true); return nil }}
	a, b, _, _ := pair(t, src, Config{}, 0)
	if err := b.Connect(); err != nil {
		t.Fatal(err)
	}
	eventually(t, "source open", func() bool { return a.State() == StateOpen })
	if !accepted.Load() {
		t.Fatal("Accept not consulted")
	}
	// Connect on a stream the peer opened is already done.
	if err := a.Connect(); err != nil {
		t.Fatal(err)
	}
	if err := b.Start(); err != nil {
		t.Fatal(err)
	}
	eventually(t, "source streaming", func() bool { return a.State() == StateStreaming })
	// The source can suspend a stream the sink set up.
	if err := a.Suspend(); err != nil || b.State() != StateOpen {
		t.Fatalf("%v, sink %v", err, b.State())
	}
}

// Both ends SET_CONFIGURATION at once and both accept the other's: both
// abort, back off by different amounts, and converge on one stream.
func TestCollisionConverges(t *testing.T) {
	var gate sync.WaitGroup
	gate.Add(2)
	barrier := func(c Codec) (Codec, error) {
		gate.Done()
		gate.Wait()
		return sbcChosen, nil
	}
	// Only the first attempt on each side meets at the barrier.
	once := func() func(Codec) (Codec, error) {
		var o sync.Once
		return func(c Codec) (Codec, error) {
			o.Do(func() { barrier(c) })
			return sbcChosen, nil
		}
	}
	src := Config{Select: once(), Backoff: func(int) time.Duration { return time.Millisecond }}
	snk := Config{Select: once(), Backoff: func(int) time.Duration { return 300 * time.Millisecond }}
	a, b, ab, ba := pair(t, src, snk, 20*time.Millisecond)
	errs := make(chan error, 2)
	go func() { errs <- a.Connect() }()
	go func() { errs <- b.Connect() }()
	for i := 0; i < 2; i++ {
		if err := <-errs; err != nil {
			t.Fatal(err)
		}
	}
	if ab.count(SigAbort)+ba.count(SigAbort) == 0 {
		t.Fatal("no collision happened; the test proves nothing")
	}
	eventually(t, "both open", func() bool { return a.State() == StateOpen && b.State() == StateOpen })
	ca, _ := a.Configuration()
	cb, _ := b.Configuration()
	if !reflect.DeepEqual(ca, cb) {
		t.Fatalf("configurations differ: %+v %+v", ca, cb)
	}
}

// script is a hand-driven peer: it sees every message the session sends and
// answers through reply.
type script struct {
	s      *Session
	sent   chan Message
	reasm  Reassembler
	labels uint8
}

func (sc *script) Send(p []byte) error {
	if m, ok, _ := sc.reasm.Feed(p); ok {
		sc.sent <- m
	}
	return nil
}

func newScript(cfg Config) *script {
	sc := &script{sent: make(chan Message, 64)}
	cfg.Signal = sc
	if cfg.Codec.Info == nil {
		cfg.Codec = sbcCaps
	}
	if cfg.Select == nil {
		cfg.Select = choose
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = time.Second
	}
	sc.s = NewSession(cfg)
	return sc
}

func (sc *script) next(t *testing.T, sig SignalID) Message {
	t.Helper()
	select {
	case m := <-sc.sent:
		if m.Signal != sig {
			t.Fatalf("got %v (type %d), want %v", m.Signal, m.Type, sig)
		}
		return m
	case <-time.After(2 * time.Second):
		t.Fatalf("no %v", sig)
	}
	return Message{}
}

func (sc *script) feed(m Message) {
	pkts, _ := Fragment(m, DefaultMTU)
	for _, p := range pkts {
		sc.s.Feed(p)
	}
}

func (sc *script) reply(m Message, t MessageType, params ...byte) {
	sc.feed(Message{m.Label, t, m.Signal, params})
}

func (sc *script) command(sig SignalID, params ...byte) {
	sc.labels++
	sc.feed(Message{sc.labels & 0xF, MsgCommand, sig, params})
}

var sinkSEP = encodeSEPs([]SEPInfo{{2, false, MediaAudio, SEPSink}})

// The speaker sends its own SET_CONFIGURATION while ours is in flight, then
// refuses ours as SEP_IN_USE: the stream is the speaker's to open.
func TestCollisionWhenThePeerRefusesOurs(t *testing.T) {
	sc := newScript(Config{})
	done := make(chan error, 1)
	go func() { done <- sc.s.Connect() }()
	sc.reply(sc.next(t, SigDiscover), MsgAccept, sinkSEP...)
	sc.reply(sc.next(t, SigGetAllCapabilities), MsgAccept, Capabilities{MediaTransport: true, Codec: &sbcCaps}.Encode()...)
	ours := sc.next(t, SigSetConfiguration)
	theirs := Codec{MediaAudio, CodecSBC, []byte{0x11, 0x15, 0x02, 0x20}}
	sc.command(SigSetConfiguration, append([]byte{1 << 2, 2 << 2}, Capabilities{MediaTransport: true, Codec: &theirs}.Encode()...)...)
	if m := sc.next(t, SigSetConfiguration); m.Type != MsgAccept {
		t.Fatalf("their configuration got type %d", m.Type)
	}
	sc.reply(ours, MsgReject, 0, byte(CodeSEPInUse))
	sc.command(SigOpen, 1<<2)
	sc.next(t, SigOpen)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if c, _ := sc.s.Configuration(); !reflect.DeepEqual(c, theirs) || sc.s.State() != StateOpen {
		t.Fatalf("%v %+v", sc.s.State(), c)
	}
}

func TestFallsBackToGetCapabilities(t *testing.T) {
	sc := newScript(Config{})
	done := make(chan error, 1)
	go func() { done <- sc.s.Connect() }()
	sc.reply(sc.next(t, SigDiscover), MsgAccept, sinkSEP...)
	sc.reply(sc.next(t, SigGetAllCapabilities), MsgGeneralReject)
	sc.reply(sc.next(t, SigGetCapabilities), MsgAccept, Capabilities{MediaTransport: true, Codec: &sbcCaps}.Encode()...)
	sc.reply(sc.next(t, SigSetConfiguration), MsgAccept)
	sc.reply(sc.next(t, SigOpen), MsgAccept)
	if err := <-done; err != nil {
		t.Fatal(err)
	}

	// A peer whose SDP record says 1.2 is never asked GET_ALL_CAPABILITIES.
	sc = newScript(Config{PeerVersion: 0x0102})
	go func() { done <- sc.s.Connect() }()
	sc.reply(sc.next(t, SigDiscover), MsgAccept, sinkSEP...)
	sc.next(t, SigGetCapabilities)
	sc.s.Disconnected()
	if err := <-done; !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}

func TestSkipsSinksItCannotUse(t *testing.T) {
	sc := newScript(Config{})
	done := make(chan error, 1)
	go func() { done <- sc.s.Connect() }()
	seps := encodeSEPs([]SEPInfo{
		{1, true, MediaAudio, SEPSink},  // in use
		{2, false, MediaVideo, SEPSink}, // video
		{3, false, MediaAudio, SEPSource},
		{4, false, MediaAudio, SEPSink},
	})
	sc.reply(sc.next(t, SigDiscover), MsgAccept, seps...)
	m := sc.next(t, SigGetAllCapabilities)
	if m.Params[0]>>2 != 4 {
		t.Fatalf("asked SEID %d", m.Params[0]>>2)
	}
	sc.reply(m, MsgReject, byte(CodeBadACPSEID))
	if err := <-done; err == nil {
		t.Fatal("connected with no usable sink")
	}
}

func TestCommandTimesOut(t *testing.T) {
	sc := newScript(Config{Timeout: 30 * time.Millisecond})
	start := time.Now()
	if err := sc.s.Connect(); !errors.Is(err, ErrTimeout) || time.Since(start) > time.Second {
		t.Fatalf("%v after %v", err, time.Since(start))
	}
	// A late answer to the abandoned command is dropped, not delivered to the
	// next one.
	m := sc.next(t, SigDiscover)
	sc.reply(m, MsgAccept, sinkSEP...)
	if len(sc.s.pending) != 0 {
		t.Fatal("pending entry left behind")
	}
}

func TestDisconnectFailsTheCommandInFlight(t *testing.T) {
	sc := newScript(Config{Timeout: time.Minute})
	done := make(chan error, 1)
	go func() { done <- sc.s.Connect() }()
	sc.next(t, SigDiscover)
	sc.s.Disconnected()
	if err := <-done; !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}

// openByPeer drives a script's session to Open as the peer would.
func openByPeer(t *testing.T, sc *script) {
	t.Helper()
	sc.command(SigSetConfiguration, append([]byte{1 << 2, 2 << 2}, sbcConfigBytes...)...)
	if m := sc.next(t, SigSetConfiguration); m.Type != MsgAccept {
		t.Fatalf("set configuration: type %d % x", m.Type, m.Params)
	}
	sc.command(SigOpen, 1<<2)
	sc.next(t, SigOpen)
}

func TestCloseWaitsForTheMediaChannel(t *testing.T) {
	sc := newScript(Config{})
	openByPeer(t, sc)
	sc.s.MediaOpened()
	sc.command(SigClose, 1<<2)
	sc.next(t, SigClose)
	if sc.s.State() != StateClosing {
		t.Fatalf("state %v", sc.s.State())
	}
	sc.s.MediaClosed()
	if sc.s.State() != StateIdle {
		t.Fatalf("state %v", sc.s.State())
	}
	if _, ok := sc.s.Configuration(); ok {
		t.Fatal("configuration survived Idle")
	}

	openByPeer(t, sc)
	sc.s.MediaOpened()
	sc.command(SigAbort, 1<<2)
	sc.next(t, SigAbort)
	if sc.s.State() != StateAborting {
		t.Fatalf("state %v", sc.s.State())
	}
	sc.s.MediaClosed()
	if sc.s.State() != StateIdle {
		t.Fatalf("state %v", sc.s.State())
	}
}
