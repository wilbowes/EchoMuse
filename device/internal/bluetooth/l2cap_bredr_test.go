package bluetooth

import (
	"bytes"
	"encoding/binary"
	"errors"
	"sync"
	"testing"
	"time"
)

// l2Pair wires two signalling engines back to back, each fed by its own
// reader goroutine so frames arrive in order, as on a link.
type l2Pair struct {
	a, b       *l2Signal
	toA, toB   chan l2Frame
	stop       chan struct{}
	wg         sync.WaitGroup
	openedB    chan *L2Channel
	mu         sync.Mutex
	framesToB  [][]byte // signalling frames A sent, for byte-level checks
	dropToB    func(cid uint16, pdu []byte) bool
	rewriteToB func(cid uint16, pdu []byte) []byte
}

type l2Frame struct {
	cid uint16
	pdu []byte
}

func newL2Pair(t *testing.T, acceptB l2Accept) *l2Pair {
	t.Helper()
	p := &l2Pair{toA: make(chan l2Frame, 64), toB: make(chan l2Frame, 64), stop: make(chan struct{}), openedB: make(chan *L2Channel, 4)}
	p.a = newL2Signal(func(cid uint16, pdu []byte) error {
		p.mu.Lock()
		drop, rw := p.dropToB, p.rewriteToB
		if cid == cidSignalBREDR {
			p.framesToB = append(p.framesToB, append([]byte(nil), pdu...))
		}
		p.mu.Unlock()
		if drop != nil && drop(cid, pdu) {
			return nil
		}
		if rw != nil {
			pdu = rw(cid, pdu)
		}
		p.toB <- l2Frame{cid, append([]byte(nil), pdu...)}
		return nil
	}, func(uint16) uint16 { return connBadPSM }, nil, 1024)
	p.b = newL2Signal(func(cid uint16, pdu []byte) error {
		p.toA <- l2Frame{cid, append([]byte(nil), pdu...)}
		return nil
	}, acceptB, func(c *L2Channel) { p.openedB <- c }, 1024)
	pump := func(s *l2Signal, in chan l2Frame) {
		defer p.wg.Done()
		for {
			select {
			case f := <-in:
				s.onFrame(f.cid, f.pdu)
			case <-p.stop:
				return
			}
		}
	}
	p.wg.Add(2)
	go pump(p.a, p.toA)
	go pump(p.b, p.toB)
	t.Cleanup(func() { close(p.stop); p.wg.Wait() })
	return p
}

func acceptOnly(psm uint16) l2Accept {
	return func(p uint16) uint16 {
		if p == psm {
			return connSuccess
		}
		return connBadPSM
	}
}

func TestL2ConnectConfigureAndExchange(t *testing.T) {
	p := newL2Pair(t, acceptOnly(0x0019))
	ca, err := p.a.Connect(0x0019)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	var cb *L2Channel
	select {
	case cb = <-p.openedB:
	case <-time.After(2 * time.Second):
		t.Fatal("the acceptor never reported the channel open")
	}
	if ca.Remote != cb.Local || cb.Remote != ca.Local {
		t.Fatalf("CIDs do not pair: a %d->%d, b %d->%d", ca.Local, ca.Remote, cb.Local, cb.Remote)
	}
	if ca.Local < cidDynFirst || cb.Local < cidDynFirst {
		t.Fatalf("a dynamic CID below 0x0040: %d %d", ca.Local, cb.Local)
	}
	if ca.MTU() != 1024 || cb.MTU() != 1024 {
		t.Fatalf("MTUs not taken from configuration: %d %d", ca.MTU(), cb.MTU())
	}
	got := make(chan []byte, 1)
	cb.SetOnData(func(b []byte) { got <- append([]byte(nil), b...) })
	if err := ca.Send([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	select {
	case b := <-got:
		if string(b) != "hello" {
			t.Fatalf("got %q", b)
		}
	case <-time.After(time.Second):
		t.Fatal("no data")
	}
	if err := ca.Send(make([]byte, 1025)); err == nil {
		t.Fatal("an SDU over the peer's MTU was sent")
	}
	if err := ca.Close(); err != nil {
		t.Fatalf("disconnect: %v", err)
	}
	select {
	case <-cb.Gone():
	case <-time.After(time.Second):
		t.Fatal("the peer's channel did not end on disconnect")
	}
	if err := ca.Send([]byte("x")); !errors.Is(err, ErrL2Closed) {
		t.Fatalf("send after close: %v", err)
	}
}

func TestL2RefusedPSM(t *testing.T) {
	p := newL2Pair(t, acceptOnly(0x0019))
	if _, err := p.a.Connect(0x0017); !errors.Is(err, ErrL2Refused) {
		t.Fatalf("want refusal, got %v", err)
	}
	p.a.mu.Lock()
	n := len(p.a.chans)
	p.a.mu.Unlock()
	if n != 0 {
		t.Fatalf("a refused channel was kept (%d)", n)
	}
}

func TestL2LinkDownEndsChannels(t *testing.T) {
	p := newL2Pair(t, acceptOnly(1))
	ca, err := p.a.Connect(1)
	if err != nil {
		t.Fatal(err)
	}
	p.a.linkDown()
	select {
	case <-ca.Gone():
	default:
		t.Fatal("link down left the channel open")
	}
	if _, err := p.a.Connect(1); err == nil {
		t.Fatal("connected on a link that is down")
	}
}

func TestL2EchoAndInfo(t *testing.T) {
	p := newL2Pair(t, nil)
	r, err := p.a.request(sigEchoReq, []byte{1, 2, 3})
	if err != nil || r[0] != sigEchoRsp || !bytes.Equal(r[1:], []byte{1, 2, 3}) {
		t.Fatalf("echo: %v %x", err, r)
	}
	r, err = p.a.request(sigInfoReq, []byte{0x02, 0x00})
	if err != nil || r[0] != sigInfoRsp || binary.LittleEndian.Uint16(r[3:5]) != 0 ||
		binary.LittleEndian.Uint32(r[5:9]) != 0x80 {
		t.Fatalf("extended features: %v %x", err, r)
	}
	r, _ = p.a.request(sigInfoReq, []byte{0x03, 0x00})
	if binary.LittleEndian.Uint64(r[5:13]) != 0x02 {
		t.Fatalf("fixed channels: %x", r)
	}
	r, _ = p.a.request(sigInfoReq, []byte{0x01, 0x00}) // connectionless MTU
	if binary.LittleEndian.Uint16(r[3:5]) != 0x0001 {
		t.Fatalf("an unsupported info type was not refused: %x", r)
	}
	// An unknown command is rejected, Not Understood (§4.1).
	r, _ = p.a.request(0x7F, nil)
	if r[0] != sigCommandReject || binary.LittleEndian.Uint16(r[1:3]) != rejectNotUnderstood {
		t.Fatalf("unknown command: %x", r)
	}
}

func TestL2ConfigOptions(t *testing.T) {
	cases := []struct {
		name         string
		opts         []byte
		mtu          int
		unknown, bad bool
	}{
		{"none: default MTU", nil, 0, false, false},
		{"MTU 895", []byte{optMTU, 2, 0x7F, 0x03}, 895, false, false},
		{"MTU at the minimum", []byte{optMTU, 2, 48, 0}, 48, false, false},
		{"MTU below the minimum", []byte{optMTU, 2, 47, 0}, 47, false, true},
		{"flush timeout is accepted", []byte{optFlushTimeout, 2, 0xFF, 0xFF}, 0, false, false},
		{"basic mode RFC", []byte{optRFC, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0}, 0, false, false},
		{"ERTM is refused", []byte{optRFC, 9, 3, 0, 0, 0, 0, 0, 0, 0, 0}, 0, false, true},
		{"unknown option", []byte{0x10, 1, 0}, 0, true, false},
		{"unknown hint is ignored", []byte{0x90, 1, 0}, 0, false, false},
		{"truncated option stops parsing", []byte{optMTU, 2, 0x00}, 0, false, false},
	}
	for _, tc := range cases {
		mtu, unk, bad := configOptions(tc.opts)
		if mtu != tc.mtu || (len(unk) > 0) != tc.unknown || (len(bad) > 0) != tc.bad {
			t.Errorf("%s: mtu %d unknown %x bad %x", tc.name, mtu, unk, bad)
		}
	}
}

// A ConfigReq split with the continuation flag is acknowledged piecewise and
// applied whole (§4.4).
func TestL2ConfigContinuation(t *testing.T) {
	p := newL2Pair(t, acceptOnly(1))
	ca, err := p.a.Connect(1)
	if err != nil {
		t.Fatal(err)
	}
	cb := <-p.openedB
	part := func(flags uint16, opts []byte) []byte {
		b := make([]byte, 4+len(opts))
		binary.LittleEndian.PutUint16(b[0:2], cb.Local)
		binary.LittleEndian.PutUint16(b[2:4], flags)
		copy(b[4:], opts)
		return b
	}
	r, err := p.a.request(sigConfigReq, part(1, []byte{optFlushTimeout, 2, 0xFF, 0xFF}))
	if err != nil || r[0] != sigConfigRsp || binary.LittleEndian.Uint16(r[3:5]) != 1 {
		t.Fatalf("continued part: %v %x", err, r)
	}
	r, _ = p.a.request(sigConfigReq, part(0, []byte{optMTU, 2, 0x00, 0x02}))
	if binary.LittleEndian.Uint16(r[5:7]) != cfgSuccess {
		t.Fatalf("final part: %x", r)
	}
	if cb.MTU() != 512 {
		t.Fatalf("MTU from a continued request: %d", cb.MTU())
	}
	_ = ca
}

// A peer that refuses our MTU and offers its own gets a second request with
// that value.
func TestL2ConfigTakesCounterProposal(t *testing.T) {
	p := newL2Pair(t, acceptOnly(1))
	p.mu.Lock()
	p.rewriteToB = func(cid uint16, pdu []byte) []byte {
		// Make B see our first ConfigReq as offering 40 bytes, below the
		// minimum, so it answers Unacceptable with 48.
		if cid == cidSignalBREDR && pdu[0] == sigConfigReq && binary.LittleEndian.Uint16(pdu[10:12]) == 1024 {
			q := append([]byte(nil), pdu...)
			binary.LittleEndian.PutUint16(q[10:12], 40)
			return q
		}
		return pdu
	}
	p.mu.Unlock()
	ca, err := p.a.Connect(1)
	if err != nil {
		t.Fatalf("did not converge: %v", err)
	}
	cb := <-p.openedB
	if cb.MTU() != 48 {
		t.Fatalf("the second request did not carry the counter-proposal: %d", cb.MTU())
	}
	_ = ca
}

func TestL2DisconnectUnknownCIDIsRejected(t *testing.T) {
	p := newL2Pair(t, nil)
	r, err := p.a.request(sigDisconnReq, []byte{0x40, 0, 0x41, 0})
	if err != nil || r[0] != sigCommandReject || binary.LittleEndian.Uint16(r[1:3]) != rejectInvalidCID {
		t.Fatalf("%v %x", err, r)
	}
}

// Several commands in one C-frame are each answered (§4: BR/EDR may pack them).
func TestL2SeveralCommandsInOneFrame(t *testing.T) {
	var out [][]byte
	s := newL2Signal(func(cid uint16, pdu []byte) error {
		out = append(out, append([]byte(nil), pdu...))
		return nil
	}, nil, nil, 0)
	frame := append(command(sigEchoReq, 1, []byte{9}), command(sigEchoReq, 2, nil)...)
	frame = append(frame, 0x08, 0x03) // a truncated trailer is ignored
	s.onFrame(cidSignalBREDR, frame)
	if len(out) != 2 || out[0][1] != 1 || out[1][1] != 2 {
		t.Fatalf("answers: %x", out)
	}
}

func TestL2Timeout(t *testing.T) {
	p := newL2Pair(t, acceptOnly(1))
	p.mu.Lock()
	p.dropToB = func(uint16, []byte) bool { return true }
	p.mu.Unlock()
	old := p.a
	_ = old
	start := time.Now()
	done := make(chan error, 1)
	go func() { _, err := p.a.request(sigEchoReq, nil); done <- err }()
	select {
	case err := <-done:
		t.Fatalf("returned early: %v after %s", err, time.Since(start))
	case <-time.After(200 * time.Millisecond):
		// still waiting, as it should be; RTX itself is 10s and not worth a
		// test run's time.
	}
}

func TestL2IdentifierSkipsZero(t *testing.T) {
	s := newL2Signal(func(uint16, []byte) error { return nil }, nil, nil, 0)
	s.nextID = 0xFF
	if a, b := s.ident(), s.ident(); a != 0xFF || b != 1 {
		t.Fatalf("identifiers %d %d", a, b)
	}
}
