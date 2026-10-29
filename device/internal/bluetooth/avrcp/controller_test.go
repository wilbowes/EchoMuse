package avrcp

import (
	"bytes"
	"errors"
	"sync"
	"testing"
	"time"
)

// fakeSink is a category 2 target, the speaker's side of absolute volume,
// written against the spec from the package's codec. It answers our session
// from its own goroutine, as a radio would.
type fakeSink struct {
	t   *testing.T
	src *Session
	in  chan []byte

	mu           sync.Mutex
	vol          uint8
	volLabel     int  // the outstanding EVENT_VOLUME_CHANGED registration, -1 none
	registered   int  // registrations received
	interimFirst bool // answer SetAbsoluteVolume with INTERIM, then ACCEPTED
	silent       bool // answer nothing
	ipid         bool // answer as a device without AVRCP
	reject       bool // reject every vendor PDU
	fragmented   bool // mark responses as the start of a fragmented one
	aborts       int
}

func newSink(t *testing.T, cfg Config) *fakeSink {
	k := &fakeSink{t: t, in: make(chan []byte, 64), volLabel: -1}
	k.src = NewSession(k, cfg)
	done := make(chan struct{})
	go func() {
		defer close(done)
		var r reassembler
		for p := range k.in {
			if m, ok, err := r.feed(p); err == nil && ok {
				k.handle(m)
			}
		}
	}()
	t.Cleanup(func() { k.src.Close(); close(k.in); <-done })
	return k
}

func (k *fakeSink) Send(b []byte) error {
	k.in <- append([]byte(nil), b...)
	return nil
}

func (k *fakeSink) reply(label uint8, f frame) {
	pkts, _ := fragment(message{label: label, response: true, pid: PID, payload: f.encode()}, 672)
	for _, p := range pkts {
		k.src.Receive(p)
	}
}

func (k *fakeSink) handle(m message) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if m.response || k.silent {
		return
	}
	if k.ipid {
		pkts, _ := fragment(message{label: m.label, response: true, ipid: true, pid: m.pid}, 672)
		k.src.Receive(pkts[0])
		return
	}
	f, err := parseFrame(m.payload)
	if err != nil {
		k.t.Errorf("sink: unreadable command % x", m.payload)
		return
	}
	if f.opcode == opPassThrough {
		k.reply(m.label, frame{RespAccepted, f.subunit, f.opcode, f.operands})
		return
	}
	_, v, err := parseVendor(f.operands)
	if err != nil {
		k.t.Errorf("sink: unreadable PDU % x", f.operands)
		return
	}
	answer := func(code uint8, params []byte) {
		r := vendorFrame(code, v.id, params)
		if k.fragmented {
			r.operands[4] = pktStart
		}
		k.reply(m.label, r)
	}
	if k.reject {
		answer(RespRejected, []byte{StatusInternalError})
		return
	}
	switch v.id {
	case pduGetCapabilities:
		if v.params[0] == capEventsSupported {
			answer(RespStable, []byte{3, 2, byte(EventPlaybackStatusChanged), byte(EventVolumeChanged)})
		} else {
			answer(RespStable, []byte{2, 2, 0x00, 0x19, 0x58, 0x12, 0x34, 0x56})
		}
	case pduSetAbsoluteVolume:
		if f.code != CtypeControl || len(v.params) != 1 {
			answer(RespRejected, []byte{StatusInvalidCommand})
			return
		}
		if k.interimFirst {
			answer(RespInterim, nil)
		}
		k.vol = v.params[0] &^ 1 // a sink with coarser steps rounds
		answer(RespAccepted, []byte{k.vol})
	case pduRegisterNotification:
		k.registered++
		k.volLabel = int(m.label)
		answer(RespInterim, []byte{byte(EventVolumeChanged), k.vol})
	case pduAbortContinuingResponse:
		k.aborts++
		answer(RespAccepted, nil)
	default:
		answer(RespRejected, []byte{StatusInvalidCommand})
	}
}

// turn simulates the speaker's own volume knob.
func (k *fakeSink) turn(v uint8) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.vol = v
	if k.volLabel < 0 {
		return
	}
	l := uint8(k.volLabel)
	k.volLabel = -1
	k.reply(l, vendorFrame(RespChanged, pduRegisterNotification, []byte{byte(EventVolumeChanged), v}))
}

func (k *fakeSink) set(f func(k *fakeSink)) {
	k.mu.Lock()
	f(k)
	k.mu.Unlock()
}

func TestCapabilitiesFromThePeer(t *testing.T) {
	k := newSink(t, Config{})
	ev, err := k.src.PeerEvents()
	if err != nil || len(ev) != 2 || ev[1] != EventVolumeChanged {
		t.Errorf("events %v %v", ev, err)
	}
	co, err := k.src.PeerCompanies()
	if err != nil || len(co) != 2 || co[0] != CompanyBTSIG || co[1] != 0x123456 {
		t.Errorf("companies %x %v", co, err)
	}
}

func TestSetAbsoluteVolume(t *testing.T) {
	k := newSink(t, Config{})
	if got, err := k.src.SetAbsoluteVolume(0x41); err != nil || got != 0x40 {
		t.Errorf("got %#x %v, want the sink's rounded 0x40", got, err)
	}
	if _, err := k.src.SetAbsoluteVolume(0x80); !errors.Is(err, ErrRange) {
		t.Errorf("0x80: %v", err)
	}
	// INTERIM first: the target took longer than TMTC and gets TMTP.
	k.set(func(k *fakeSink) { k.interimFirst = true })
	if got, err := k.src.SetAbsoluteVolume(0x7F); err != nil || got != 0x7E {
		t.Errorf("after INTERIM: %#x %v", got, err)
	}
}

func TestWatchVolumeReRegisters(t *testing.T) {
	got := make(chan uint8, 8)
	k := newSink(t, Config{OnVolume: func(v uint8) { got <- v }})
	k.set(func(k *fakeSink) { k.vol = 0x30 })
	if v, err := k.src.WatchVolume(); err != nil || v != 0x30 {
		t.Fatalf("interim %#x %v", v, err)
	}
	for _, v := range []uint8{0x10, 0x55, 0x00} {
		k.turn(v)
		select {
		case g := <-got:
			if g != v {
				t.Errorf("reported %#x, want %#x", g, v)
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("no report of %#x: the watch was not renewed", v)
		}
		// Wait for the re-registration before the next turn of the knob.
		deadline := time.Now().Add(2 * time.Second)
		for {
			k.mu.Lock()
			ok := k.volLabel >= 0
			k.mu.Unlock()
			if ok || time.Now().After(deadline) {
				break
			}
			time.Sleep(time.Millisecond)
		}
	}
	k.mu.Lock()
	n := k.registered
	k.mu.Unlock()
	if n != 4 {
		t.Errorf("%d registrations, want the first and one per CHANGED", n)
	}
	if v, err := k.src.WatchVolume(); err != nil || v != 0x00 {
		t.Errorf("a second watch: %#x %v", v, err)
	}
}

func TestControllerFailures(t *testing.T) {
	quick := Config{Slack: 10 * time.Millisecond}

	k := newSink(t, quick)
	k.set(func(k *fakeSink) { k.silent = true })
	start := time.Now()
	if _, err := k.src.SetAbsoluteVolume(1); !errors.Is(err, ErrTimeout) {
		t.Errorf("silent: %v", err)
	}
	if d := time.Since(start); d < TMTC || d > TMTC+time.Second {
		t.Errorf("CONTROL timed out after %v, want TMTC plus slack", d)
	}
	if n := len(k.src.pending); n != 0 {
		t.Errorf("%d labels still held after a timeout", n)
	}
	if _, err := k.src.WatchVolume(); !errors.Is(err, ErrTimeout) || k.src.volWatch {
		t.Errorf("silent watch: %v, watching %v", err, k.src.volWatch)
	}

	k = newSink(t, quick)
	k.set(func(k *fakeSink) { k.ipid = true })
	if _, err := k.src.PeerEvents(); !errors.Is(err, ErrNoProfile) {
		t.Errorf("IPID: %v", err)
	}

	k = newSink(t, quick)
	k.set(func(k *fakeSink) { k.reject = true })
	var se *StatusError
	if _, err := k.src.SetAbsoluteVolume(1); !errors.As(err, &se) || se.Status != StatusInternalError {
		t.Errorf("rejected: %v", err)
	}

	k = newSink(t, quick)
	k.set(func(k *fakeSink) { k.fragmented = true })
	if _, err := k.src.PeerEvents(); !errors.Is(err, ErrFragmented) {
		t.Errorf("fragmented: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		k.mu.Lock()
		n := k.aborts
		k.mu.Unlock()
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("no AbortContinuingResponse after a fragmented answer")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestCloseFailsWhatIsInFlight(t *testing.T) {
	k := newSink(t, Config{Slack: time.Hour})
	k.set(func(k *fakeSink) { k.silent = true })
	errc := make(chan error, 1)
	go func() { _, err := k.src.SetAbsoluteVolume(1); errc <- err }()
	for {
		k.src.mu.Lock()
		n := len(k.src.pending)
		k.src.mu.Unlock()
		if n == 1 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	k.src.Close()
	if err := <-errc; !errors.Is(err, ErrClosed) {
		t.Errorf("got %v", err)
	}
	if _, err := k.src.SetAbsoluteVolume(1); !errors.Is(err, ErrClosed) {
		t.Errorf("after close: %v", err)
	}
}

// A late or unsolicited response answers nothing and is counted.
func TestUnsolicitedResponseDropped(t *testing.T) {
	s, _ := target(t, Config{})
	pkts, _ := fragment(message{label: 4, response: true, pid: PID, payload: []byte{RespAccepted, 0x48, 0x7C, 0x44, 0}}, 672)
	s.Receive(pkts[0])
	if s.Dropped() != 1 {
		t.Errorf("dropped %d", s.Dropped())
	}
}

// Two sessions wired back to back: one speaker's controller pressing the
// other's buttons and reading its capabilities and play status.
func TestTwoSessions(t *testing.T) {
	var mu sync.Mutex
	var presses []Op
	srcIn, sinkIn := make(chan []byte, 64), make(chan []byte, 64)
	src := NewSession(chanSender(sinkIn), Config{OnPassThrough: func(op Op, pressed bool) {
		if pressed {
			mu.Lock()
			presses = append(presses, op)
			mu.Unlock()
		}
	}})
	sink := NewSession(chanSender(srcIn), Config{})
	for _, p := range []struct {
		s  *Session
		in chan []byte
	}{{src, srcIn}, {sink, sinkIn}} {
		go func(s *Session, in chan []byte) {
			for b := range in {
				s.Receive(b)
			}
		}(p.s, p.in)
	}
	t.Cleanup(func() { src.Close(); sink.Close(); close(srcIn); close(sinkIn) })

	for _, op := range []Op{OpPlay, OpPause, OpForward} {
		if err := sink.PassThrough(op); err != nil {
			t.Fatalf("%v: %v", op, err)
		}
	}
	if err := sink.PassThrough(0x47); !errors.Is(err, ErrNotImplemented) {
		t.Errorf("record: %v", err)
	}
	mu.Lock()
	if len(presses) != 3 || presses[0] != OpPlay || presses[2] != OpForward {
		t.Errorf("presses %v", presses)
	}
	mu.Unlock()
	if ev, err := sink.PeerEvents(); err != nil || len(ev) != 1 || ev[0] != EventPlaybackStatusChanged {
		t.Errorf("events %v %v", ev, err)
	}
	// We are no amplifier: absolute volume sent to the source is refused.
	var se *StatusError
	if _, err := sink.SetAbsoluteVolume(1); !errors.As(err, &se) || se.Status != StatusInvalidCommand {
		t.Errorf("absolute volume to a source: %v", err)
	}
	src.SetPlayStatus(PlayPlaying)
	f, err := sink.request(vendorFrame(CtypeStatus, pduGetPlayStatus, nil), false)
	if p, _ := sink.vendorResult(f, pduGetPlayStatus); err != nil || len(p) != 9 || p[8] != byte(PlayPlaying) {
		t.Errorf("play status % x %v", p, err)
	}
	if !bytes.Equal(passThroughOperands(OpPlay, false), []byte{0x44, 0}) {
		t.Fatal("operands")
	}
}

type chanSender chan []byte

func (c chanSender) Send(b []byte) error {
	c <- append([]byte(nil), b...)
	return nil
}
