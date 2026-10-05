package bluetooth

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"reflect"
	"sync"
	"testing"
	"time"
)

// ── A controller to run a session against ────────────────────────────────────
//
// fakeCtl stands where /dev/stpbt does: it takes the HCI packets the host
// writes and answers as a controller with peers in range would. Each peer is
// a fakeServer (gatt_test.go) behind an address, so everything above HCI —
// the scanner's session, the host's routing, the connection manager, ATT and
// discovery — runs as it does on a device.

type fakePeer struct {
	srv     *fakeServer
	handle  uint16
	mtu     uint16 // receive MTU it answers the exchange with; 0 refuses it
	mute    bool   // never answers ATT
	silent  bool   // never accepts a connection
	delay   time.Duration
	got     [][]byte // what the host sent it that was not a request: by "cid:pdu"
	gotCIDs []uint16
}

type fakeCtl struct {
	mu     sync.Mutex
	out    chan []byte
	closed bool

	aclLen, aclNum int
	leBuffers      bool // answer LE Read Buffer Size with real numbers
	holdCredits    bool // never report completed packets

	peers      map[string]*fakePeer
	links      map[uint16]*fakePeer
	next       uint16
	reasm      aclReassembler
	scan       []byte // every LE Set Scan Enable value, in order
	created    [][]byte
	randomAddr []byte
	cancels    int
	resets     int
	connecting string
	updates    []uint16      // every LE Connection Update's interval, 1.25ms units
	updateLag  time.Duration // before an update completes
}

func newFakeCtl() *fakeCtl {
	return &fakeCtl{
		out: make(chan []byte, 4096), aclLen: 1021, aclNum: 4,
		peers: map[string]*fakePeer{}, links: map[uint16]*fakePeer{}, next: 0x0201,
	}
}

func (f *fakeCtl) peer(addr string) *fakePeer {
	p := &fakePeer{srv: &fakeServer{mtu: attDefaultMTU}, mtu: attDefaultMTU}
	f.peers[addr] = p
	return p
}

func (f *fakeCtl) Read(p []byte) (int, error) {
	pkt, ok := <-f.out
	if !ok {
		return 0, io.EOF
	}
	return copy(p, pkt), nil
}

func (f *fakeCtl) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.closed {
		f.closed = true
		close(f.out)
	}
	return nil
}

func (f *fakeCtl) emit(pkt []byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.closed {
		f.out <- pkt
	}
}

func (f *fakeCtl) event(code byte, params ...byte) {
	f.emit(append([]byte{h4TypeEvent, code, byte(len(params))}, params...))
}

func (f *fakeCtl) complete(op uint16, status byte, ret ...byte) {
	f.event(evtCommandComplete, append([]byte{1, byte(op), byte(op >> 8), status}, ret...)...)
}

func (f *fakeCtl) status(op uint16, status byte) {
	f.event(evtCommandStatus, status, 1, byte(op), byte(op>>8))
}

func (f *fakeCtl) connComplete(status byte, handle uint16, addr string) {
	mac, _ := net.ParseMAC(addr)
	p := []byte{leSubeventConnComplete, status, byte(handle), byte(handle >> 8), 0, 1}
	for i := 5; i >= 0; i-- {
		p = append(p, mac[i])
	}
	f.event(evtLEMeta, append(p, 24, 0, 0, 0, 0xF4, 0x01, 0)...)
}

// toHost sends one L2CAP PDU as the 27-byte fragments a 4.0 link delivers.
func (f *fakeCtl) toHost(handle, cid uint16, pdu []byte) {
	for _, pkt := range fragmentACL(handle, cid, pdu, 27) {
		f.emit(pkt)
	}
}

// drop ends a link from the peer's side.
func (f *fakeCtl) drop(handle uint16, reason byte) {
	f.mu.Lock()
	delete(f.links, handle)
	f.mu.Unlock()
	f.event(evtDisconnectComplete, 0, byte(handle), byte(handle>>8), reason)
}

func (f *fakeCtl) Write(pkt []byte) (int, error) {
	switch pkt[0] {
	case h4TypeCommand:
		f.command(binary.LittleEndian.Uint16(pkt[1:3]), pkt[4:])
	case h4TypeACL:
		handle := binary.LittleEndian.Uint16(pkt[1:3]) & 0x0FFF
		if !f.holdCredits {
			f.event(evtNumCompletedPackets, 1, byte(handle), byte(handle>>8), 1, 0)
		}
		f.mu.Lock()
		h, cid, pdu, ok := f.reasm.Feed(pkt)
		p := f.links[h]
		f.mu.Unlock()
		if ok && p != nil {
			f.fromHost(p, cid, append([]byte(nil), pdu...))
		}
	}
	return len(pkt), nil
}

func (f *fakeCtl) command(op uint16, params []byte) {
	switch op {
	case opReset:
		f.mu.Lock()
		f.resets++
		f.mu.Unlock()
		f.complete(op, 0)
	case opReadBdAddr:
		f.complete(op, 0, 0x01, 0x63, 0x81, 0x46, 0x00, 0x00)
	case opLEReadBufferSize:
		if f.leBuffers {
			f.complete(op, 0, byte(f.aclLen), byte(f.aclLen>>8), byte(f.aclNum))
		} else {
			f.complete(op, 0, 0, 0, 0)
		}
	case opReadBufferSize:
		f.complete(op, 0, byte(f.aclLen), byte(f.aclLen>>8), 0, byte(f.aclNum), byte(f.aclNum>>8), 0, 0)
	case opLESetRandomAddr:
		f.mu.Lock()
		f.randomAddr = append([]byte(nil), params...)
		f.mu.Unlock()
		f.complete(op, 0)
	case opLESetScanEnable:
		f.mu.Lock()
		f.scan = append(f.scan, params[0])
		f.mu.Unlock()
		f.complete(op, 0)
	case opLECreateConn:
		mac := net.HardwareAddr{params[11], params[10], params[9], params[8], params[7], params[6]}
		addr := mac.String()
		f.mu.Lock()
		f.created = append(f.created, append([]byte(nil), params...))
		f.connecting = addr
		p := f.peers[addr]
		f.mu.Unlock()
		f.status(op, 0)
		if p == nil || p.silent {
			return
		}
		go func() {
			time.Sleep(p.delay)
			f.mu.Lock()
			if f.connecting != addr {
				f.mu.Unlock()
				return
			}
			f.connecting = ""
			p.handle = f.next
			f.next++
			f.links[p.handle] = p
			f.mu.Unlock()
			f.connComplete(0, p.handle, addr)
		}()
	case opLECreateConnCancel:
		f.mu.Lock()
		f.cancels++
		addr := f.connecting
		f.connecting = ""
		f.mu.Unlock()
		f.complete(op, 0)
		if addr != "" {
			f.connComplete(0x02, 0, addr)
		}
	case opDisconnect:
		handle := binary.LittleEndian.Uint16(params[0:2])
		f.status(op, 0)
		f.drop(handle, hciReasonLocalHost)
	case opLEConnUpdate:
		f.mu.Lock()
		f.updates = append(f.updates, binary.LittleEndian.Uint16(params[2:4]))
		lag := f.updateLag
		f.mu.Unlock()
		f.status(op, 0)
		done := append([]byte{leSubeventConnUpdateDone, 0}, params[0:2]...)
		done = append(done, params[2:4]...)
		done = append(done, 0, 0, 0xF4, 0x01)
		go func() {
			time.Sleep(lag)
			f.event(evtLEMeta, done...)
		}()
	default:
		f.complete(op, 0)
	}
}

func (f *fakeCtl) fromHost(p *fakePeer, cid uint16, pdu []byte) {
	record := func() {
		f.mu.Lock()
		p.got = append(p.got, pdu)
		p.gotCIDs = append(p.gotCIDs, cid)
		f.mu.Unlock()
	}
	if cid != cidATT {
		record()
		return
	}
	switch op := pdu[0]; {
	case op == attOpMTUReq:
		if p.mute {
			return
		}
		if p.mtu == 0 {
			f.toHost(p.handle, cidATT, attErrorRsp(op, 0, attErrReqNotSupport))
			return
		}
		f.toHost(p.handle, cidATT, []byte{attOpMTURsp, byte(p.mtu), byte(p.mtu >> 8)})
	case op == attOpError || op == attOpMTURsp || op == attOpConfirmation:
		record()
	case p.mute:
	default:
		if rsp, _ := p.srv.Request(pdu); rsp != nil {
			f.toHost(p.handle, cidATT, rsp)
		}
	}
}

// received waits for the host to have sent the peer n PDUs outside its own
// requests, and returns them.
func (f *fakeCtl) received(t *testing.T, p *fakePeer, n int) ([][]byte, []uint16) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		f.mu.Lock()
		got, cids := append([][]byte(nil), p.got...), append([]uint16(nil), p.gotCIDs...)
		f.mu.Unlock()
		if len(got) >= n {
			return got, cids
		}
		if time.Now().After(deadline) {
			t.Fatalf("peer received %d PDUs, want %d: % x", len(got), n, got)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// session runs a scanner session on the fake and waits for the connection
// manager to be attached.
func session(t *testing.T, f *fakeCtl, setup func(*Scanner)) *Scanner {
	t.Helper()
	s := NewScanner(nil)
	if setup != nil {
		setup(s)
	}
	stop, done := make(chan struct{}), make(chan error, 1)
	go func() { done <- s.serve(f, stop) }()
	t.Cleanup(func() {
		close(stop)
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("session did not stop")
		}
		f.Close()
	})
	deadline := time.Now().Add(2 * time.Second)
	for {
		if free, _ := s.Conns().Slots(); free == MaxConnSlots {
			return s
		}
		if time.Now().After(deadline) {
			t.Fatal("connection manager never attached")
		}
		time.Sleep(2 * time.Millisecond)
	}
}

const (
	peerA = "c0:00:00:00:00:0a"
	peerB = "c0:00:00:00:00:0b"
	peerC = "c0:00:00:00:00:0c"
	peerD = "c0:00:00:00:00:0d"
)

func table(s *fakeServer) (name, level, long, cccd uint16) {
	s.service(UUID16(0x1800))
	s.characteristic(0x02, UUID16(0x2A00))
	name = uint16(len(s.attrs))
	s.service(uuid128Wire)
	s.characteristic(0x1A, UUID16(0x2A19), UUID16(0x2902))
	level, cccd = uint16(len(s.attrs))-1, uint16(len(s.attrs))
	s.characteristic(0x0A, uuid128Wire)
	long = uint16(len(s.attrs))
	s.attrs[name-1].value = []byte("Chonky Monkey")
	s.attrs[level-1].value = []byte{87}
	return
}

func TestConnectDiscoverReadWrite(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	name, level, long, cccd := table(p.srv)
	s := session(t, f, nil)
	m := s.Conns()

	info, err := m.Connect("C0:00:00:00:00:0A", 1)
	if err != nil {
		t.Fatal(err)
	}
	if info.Addr != peerA || info.MTU != attDefaultMTU || info.IntervalMs != 30 {
		t.Fatalf("info %+v", info)
	}
	if free, limit := m.Slots(); free != 2 || limit != 3 {
		t.Fatalf("slots %d/%d", free, limit)
	}

	services, err := m.Services(peerA)
	if err != nil {
		t.Fatal(err)
	}
	want, err := DiscoverServices(p.srv) // the same walk, straight at the server
	if err != nil || !reflect.DeepEqual(services, want) || len(services) != 2 {
		t.Fatalf("services %+v, direct %+v (%v)", services, want, err)
	}
	before := p.srv.requests
	if again, _ := m.Services(peerA); !reflect.DeepEqual(again, services) || p.srv.requests != before {
		t.Fatal("a second Services call went back to the peer")
	}

	if v, err := m.Read(peerA, name); err != nil || string(v) != "Chonky Monkey" {
		t.Fatalf("read name: %q %v", v, err)
	}
	if v, err := m.Read(peerA, level); err != nil || !bytes.Equal(v, []byte{87}) {
		t.Fatalf("read level: % x %v", v, err)
	}

	// With a response the value is confirmed; without one it just lands.
	if err := m.Write(peerA, cccd, []byte{1, 0}, true); err != nil {
		t.Fatal(err)
	}
	if got := p.srv.attrs[cccd-1].value; !bytes.Equal(got, []byte{1, 0}) {
		t.Fatalf("cccd % x", got)
	}
	if err := m.Write(peerA, long, []byte("no reply"), false); err != nil {
		t.Fatal(err)
	}
	// The next transaction is behind the command on the same link.
	if v, err := m.Read(peerA, long); err != nil || string(v) != "no reply" {
		t.Fatalf("after write command: %q %v", v, err)
	}

	// One ATT write carries MTU-3 bytes and no more.
	if err := m.Write(peerA, long, make([]byte, attDefaultMTU-3), true); err != nil {
		t.Fatalf("largest write: %v", err)
	}
	if err := m.Write(peerA, long, make([]byte, attDefaultMTU-2), true); !errors.Is(err, ErrTooLong) {
		t.Fatalf("one byte over: %v", err)
	}

	// A refusal is an ATT error with the link still up.
	_, err = m.Read(peerA, 0x7777)
	var e *ATTError
	if !errors.As(err, &e) || e.Code != 0x01 {
		t.Fatalf("bad handle: %v", err)
	}
	if _, err := m.Read(peerA, name); err != nil {
		t.Fatalf("link did not survive an ATT error: %v", err)
	}
}

func TestLongReadFollowsWithBlobs(t *testing.T) {
	long := bytes.Repeat([]byte("0123456789"), 6) // 60 bytes: 22 + 22 + 16
	exact := bytes.Repeat([]byte{0xAB}, 44)       // 22 + 22, then an empty blob
	one := bytes.Repeat([]byte{0xCD}, 22)         // fills one response exactly
	for _, notLong := range []bool{false, true} {
		f := newFakeCtl()
		p := f.peer(peerA)
		_, _, h, _ := table(p.srv)
		p.srv.notLong = notLong
		m := session(t, f, nil).Conns()
		if _, err := m.Connect(peerA, 1); err != nil {
			t.Fatal(err)
		}
		for _, want := range [][]byte{long, exact, one, {}} {
			p.srv.attrs[h-1].value = want
			got, err := m.Read(peerA, h)
			if err != nil || !bytes.Equal(got, want) {
				t.Fatalf("notLong=%v len %d: got %d bytes, %v", notLong, len(want), len(got), err)
			}
		}
	}
}

func TestMTUIsTheSmallerOfTheTwo(t *testing.T) {
	for _, c := range []struct {
		peer uint16
		want int
	}{{23, 23}, {185, 185}, {247, 247}, {517, attClientMTU}, {0, attDefaultMTU}} {
		f := newFakeCtl()
		f.peer(peerA).mtu = c.peer
		m := session(t, f, nil).Conns()
		info, err := m.Connect(peerA, 0)
		if err != nil || info.MTU != c.want {
			t.Fatalf("peer mtu %d: got %d %v", c.peer, info.MTU, err)
		}
	}
}

func TestThreeSlots(t *testing.T) {
	f := newFakeCtl()
	for _, a := range []string{peerA, peerB, peerC, peerD} {
		f.peer(a)
	}
	var mu sync.Mutex
	var dropped []string
	s := session(t, f, func(s *Scanner) {
		s.Conns().OnDisconnect = func(addr string, reason byte) {
			mu.Lock()
			dropped = append(dropped, addr)
			mu.Unlock()
		}
	})
	m := s.Conns()
	for _, a := range []string{peerA, peerB, peerC} {
		if _, err := m.Connect(a, 1); err != nil {
			t.Fatalf("%s: %v", a, err)
		}
	}
	if free, _ := m.Slots(); free != 0 {
		t.Fatalf("free %d", free)
	}
	if _, err := m.Connect(peerD, 1); !errors.Is(err, ErrNoSlots) {
		t.Fatalf("fourth link: %v", err)
	}
	if len(f.created) != 3 {
		t.Fatalf("the refused connect reached the controller (%d creates)", len(f.created))
	}
	if err := m.Disconnect(peerB); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Read(peerB, 1); !errors.Is(err, ErrNotConnected) {
		t.Fatalf("read after disconnect: %v", err)
	}
	if _, err := m.Connect(peerA, 1); !errors.Is(err, ErrAlreadyConnected) {
		t.Fatalf("second link to one peer: %v", err)
	}
	if _, err := m.Connect(peerD, 1); err != nil {
		t.Fatalf("freed slot: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if !reflect.DeepEqual(dropped, []string{peerB}) {
		t.Fatalf("disconnect callbacks %v", dropped)
	}
}

func TestPeerDropsMidRequest(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	gone := make(chan byte, 1)
	m := session(t, f, func(s *Scanner) {
		s.Conns().OnDisconnect = func(addr string, reason byte) { gone <- reason }
	}).Conns()
	info, err := m.Connect(peerA, 1)
	if err != nil {
		t.Fatal(err)
	}
	p.mute = true
	res := make(chan error, 1)
	go func() { _, err := m.Read(peerA, 1); res <- err }()
	time.Sleep(20 * time.Millisecond)
	f.drop(info.Handle, 0x08) // connection timeout
	if err := <-res; !errors.Is(err, ErrDisconnected) {
		t.Fatalf("waiting request: %v", err)
	}
	if r := <-gone; r != 0x08 {
		t.Fatalf("reason 0x%02x", r)
	}
	if free, _ := m.Slots(); free != MaxConnSlots {
		t.Fatalf("slot not freed: %d", free)
	}
}

func TestConnectTimeoutCancels(t *testing.T) {
	f := newFakeCtl()
	f.peer(peerA).silent = true
	f.peer(peerB)
	m := session(t, f, func(s *Scanner) { s.Conns().connectTimeout = 30 * time.Millisecond }).Conns()
	if _, err := m.Connect(peerA, 1); !errors.Is(err, ErrConnectTimeout) {
		t.Fatalf("got %v", err)
	}
	if f.cancels != 1 {
		t.Fatalf("%d cancels", f.cancels)
	}
	if free, _ := m.Slots(); free != MaxConnSlots {
		t.Fatalf("a failed connect holds a slot: %d free", free)
	}
	if _, err := m.Connect(peerB, 1); err != nil {
		t.Fatalf("connect after a timeout: %v", err)
	}
}

func TestATTTimeoutDropsTheLink(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	gone := make(chan byte, 1)
	m := session(t, f, func(s *Scanner) {
		s.Conns().attTimeout = 30 * time.Millisecond
		s.Conns().OnDisconnect = func(addr string, reason byte) { gone <- reason }
	}).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	p.mute = true
	if _, err := m.Read(peerA, 1); !errors.Is(err, ErrATTTimeout) {
		t.Fatalf("got %v", err)
	}
	select {
	case <-gone:
	case <-time.After(2 * time.Second):
		t.Fatal("the bearer was left up after a transaction timeout")
	}
}

func TestNotificationsAndIndications(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	type note struct {
		addr   string
		handle uint16
		value  string
		ind    bool
	}
	notes := make(chan note, 4)
	m := session(t, f, func(s *Scanner) {
		s.Conns().OnNotify = func(addr string, handle uint16, value []byte, ind bool) {
			notes <- note{addr, handle, string(value), ind}
		}
	}).Conns()
	info, err := m.Connect(peerA, 1)
	if err != nil {
		t.Fatal(err)
	}
	f.toHost(info.Handle, cidATT, append([]byte{attOpNotification, 0x11, 0x00}, "tick"...))
	if n := <-notes; n != (note{peerA, 0x11, "tick", false}) {
		t.Fatalf("%+v", n)
	}
	// Longer than one 27-byte fragment, so it arrives in pieces.
	big := string(bytes.Repeat([]byte("x"), 40))
	f.toHost(info.Handle, cidATT, append([]byte{attOpIndication, 0x12, 0x00}, big...))
	if n := <-notes; n != (note{peerA, 0x12, big, true}) {
		t.Fatalf("%+v", n)
	}
	// An indication is owed a confirmation; a notification is not.
	got, _ := f.received(t, p, 1)
	if len(got) != 1 || !bytes.Equal(got[0], []byte{attOpConfirmation}) {
		t.Fatalf("% x", got)
	}
}

func TestPeerRequestsAreAnswered(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	p.mtu = 517
	p.srv.mtu = attClientMTU
	_, _, long, _ := table(p.srv)
	m := session(t, f, nil).Conns()
	info, err := m.Connect(peerA, 1)
	if err != nil {
		t.Fatal(err)
	}
	h := info.Handle
	ask := func(cid uint16, pdu []byte, want []byte) {
		t.Helper()
		f.mu.Lock()
		n := len(p.got)
		f.mu.Unlock()
		f.toHost(h, cid, pdu)
		got, cids := f.received(t, p, n+1)
		if cids[n] != cid || !bytes.Equal(got[n], want) {
			t.Fatalf("to % x on cid %d: got % x on cid %d, want % x", pdu, cid, got[n], cids[n], want)
		}
	}
	// What the phone and the Sonos sent within a second of connecting.
	ask(cidATT, []byte{attOpMTUReq, 0x05, 0x02}, []byte{attOpMTURsp, byte(attClientMTU), 0})
	ask(cidATT, []byte{0x10, 1, 0, 0xFF, 0xFF, 0x00, 0x28}, attErrorRsp(0x10, 1, attErrReqNotSupport))
	ask(cidATT, []byte{0x08, 1, 0, 0xFF, 0xFF, 0x3A, 0x2B}, attErrorRsp(0x08, 1, attErrReqNotSupport))
	ask(cidLESig, []byte{0x12, 7, 8, 0, 6, 0, 16, 0, 0, 0, 0xF4, 1}, []byte{0x13, 7, 2, 0, 1, 0})
	ask(cidLESig, []byte{0x0A, 9, 0, 0}, []byte{0x01, 9, 2, 0, 0, 0})
	ask(cidSMP, []byte{0x0B, 0x01}, []byte{0x05, 0x05})

	// The link runs at the smaller of our 247 and its 517.
	if err := m.Write(peerA, long, make([]byte, attClientMTU-3), true); err != nil {
		t.Fatalf("a write that fills the link MTU: %v", err)
	}
	// A command (no response owed) and an unsolicited response get nothing.
	f.toHost(h, cidATT, []byte{attOpWriteCmd, 1, 0, 9})
	f.toHost(h, cidATT, []byte{attOpReadRsp, 1})
	if _, err := m.Read(peerA, long); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(p.got) != 6 {
		t.Fatalf("host sent %d unsolicited PDUs, want 6: % x", len(p.got), p.got)
	}
}

func TestSmallControllerBuffers(t *testing.T) {
	// A controller with one 10-byte buffer: every PDU goes out in fragments,
	// each waiting for the last to be reported complete.
	f := newFakeCtl()
	f.aclLen, f.aclNum, f.leBuffers = 10, 1, true
	p := f.peer(peerA)
	_, _, long, _ := table(p.srv)
	m := session(t, f, nil).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	want := []byte("twenty bytes of data")
	if err := m.Write(peerA, long, want, true); err != nil {
		t.Fatal(err)
	}
	if got := p.srv.attrs[long-1].value; !bytes.Equal(got, want) {
		t.Fatalf("%q", got)
	}
	if services, err := m.Services(peerA); err != nil || len(services) != 2 {
		t.Fatalf("%v %v", services, err)
	}
}

func TestOwnRandomAddress(t *testing.T) {
	a, b := StaticRandomAddr("G090LF0000000001"), StaticRandomAddr("G090LF0000000002")
	if len(a) != 6 || a[5]&0xC0 != 0xC0 || bytes.Equal(a, b) || !bytes.Equal(a, StaticRandomAddr("G090LF0000000001")) {
		t.Fatalf("% x / % x", a, b)
	}
	f := newFakeCtl()
	f.peer(peerA)
	m := session(t, f, func(s *Scanner) { s.Conns().SetOwnAddress(a) }).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(f.randomAddr, a) {
		t.Fatalf("controller was given % x", f.randomAddr)
	}
	if f.created[0][12] != 1 || f.created[0][5] != 1 {
		t.Fatalf("own type %d peer type %d", f.created[0][12], f.created[0][5])
	}

	// Without one the public address is used and nothing is set.
	f2 := newFakeCtl()
	f2.peer(peerA)
	m2 := session(t, f2, nil).Conns()
	if _, err := m2.Connect(peerA, 0); err != nil {
		t.Fatal(err)
	}
	if f2.randomAddr != nil || f2.created[0][12] != 0 || f2.created[0][5] != 0 {
		t.Fatalf("% x own %d peer %d", f2.randomAddr, f2.created[0][12], f2.created[0][5])
	}
}

func TestScanPausesWhileConnecting(t *testing.T) {
	f := newFakeCtl()
	f.peer(peerA).delay = 150 * time.Millisecond
	m := session(t, f, nil).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		f.mu.Lock()
		scan := append([]byte(nil), f.scan...)
		f.mu.Unlock()
		if bytes.Equal(scan, []byte{1, 0, 1}) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("scan enable history %v, want on, off for the connect, on", scan)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestSessionEndEndsEveryLink(t *testing.T) {
	f := newFakeCtl()
	f.peer(peerA)
	f.peer(peerB)
	var mu sync.Mutex
	dropped := map[string]bool{}
	s := NewScanner(nil)
	s.Conns().OnDisconnect = func(addr string, reason byte) {
		mu.Lock()
		dropped[addr] = true
		mu.Unlock()
	}
	stop, done := make(chan struct{}), make(chan error, 1)
	go func() { done <- s.serve(f, stop) }()
	defer f.Close()
	m := s.Conns()
	for free, _ := m.Slots(); free != MaxConnSlots; free, _ = m.Slots() {
		time.Sleep(2 * time.Millisecond)
	}
	for _, a := range []string{peerA, peerB} {
		if _, err := m.Connect(a, 1); err != nil {
			t.Fatal(err)
		}
	}
	close(stop)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if !dropped[peerA] || !dropped[peerB] {
		t.Fatalf("callbacks %v", dropped)
	}
	if _, err := m.Connect(peerA, 1); !errors.Is(err, ErrNotRunning) {
		t.Fatalf("connect with no session: %v", err)
	}
	if free, limit := m.Slots(); free != 0 || limit != MaxConnSlots {
		t.Fatalf("slots %d/%d with no session", free, limit)
	}
	// The controller is reset on the way out, so no peer keeps a link to a
	// closed device.
	if f.resets != 2 {
		t.Fatalf("%d resets", f.resets)
	}
}

func TestAdvertsStillFlowBesideALink(t *testing.T) {
	f := newFakeCtl()
	f.peer(peerA)
	batches := make(chan []Advert, 16)
	s := NewScanner(func(b []Advert) { batches <- b })
	stop, done := make(chan struct{}), make(chan error, 1)
	go func() { done <- s.serve(f, stop) }()
	defer func() { close(stop); <-done; f.Close() }()
	m := s.Conns()
	for free, _ := m.Slots(); free != MaxConnSlots; free, _ = m.Slots() {
		time.Sleep(2 * time.Millisecond)
	}
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	// One advertising report: ADV_IND, random address, 3 bytes of data.
	f.event(evtLEMeta, leSubeventAdvReport, 1, 0x00, 0x01, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11, 3, 0x02, 0x01, 0x06, 0xC4)
	select {
	case b := <-batches:
		if len(b) != 1 || b[0].Addr != "11:22:33:44:55:66" || b[0].Rssi != -60 {
			t.Fatalf("%+v", b)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no advert batch while a link was up")
	}
	if _, err := m.Read(peerA, 1); err == nil {
		// handle 1 does not exist on an empty table; the point is that the
		// link still answers.
		t.Fatal("expected an ATT error")
	} else if !errors.As(err, new(*ATTError)) {
		t.Fatalf("link broke: %v", err)
	}
}

func (f *fakeCtl) waitUpdates(t *testing.T, want []uint16) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		f.mu.Lock()
		got := append([]uint16(nil), f.updates...)
		f.mu.Unlock()
		if reflect.DeepEqual(got, want) {
			return
		}
		if time.Now().After(deadline) || len(got) > len(want) {
			t.Fatalf("connection updates %v, want %v", got, want)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func TestIdleLinkSlowsDownAndABusyOneSpeedsUp(t *testing.T) {
	const fast, slow = 24, 400 // 30ms and 500ms in 1.25ms units
	f := newFakeCtl()
	p := f.peer(peerA)
	name, _, _, _ := table(p.srv)
	m := session(t, f, func(s *Scanner) { s.Conns().idleAfter = 60 * time.Millisecond }).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	// Made fast, used for discovery, then left alone.
	if _, err := m.Services(peerA); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	if len(f.updates) != 0 {
		t.Fatalf("updated while in use: %v", f.updates)
	}
	f.mu.Unlock()
	f.waitUpdates(t, []uint16{slow})

	// The first request on a quiet link asks for the fast interval back, and
	// goes out without waiting for it.
	if _, err := m.Read(peerA, name); err != nil {
		t.Fatal(err)
	}
	f.waitUpdates(t, []uint16{slow, fast})

	// Kept busy, it stays fast: no update per request.
	for end := time.Now().Add(150 * time.Millisecond); time.Now().Before(end); time.Sleep(10 * time.Millisecond) {
		if _, err := m.Read(peerA, name); err != nil {
			t.Fatal(err)
		}
	}
	f.waitUpdates(t, []uint16{slow, fast})
	f.waitUpdates(t, []uint16{slow, fast, slow}) // and quiet again
}

func TestOneConnectionUpdateAtATime(t *testing.T) {
	const fast, slow = 24, 400
	f := newFakeCtl()
	f.updateLag = 120 * time.Millisecond
	p := f.peer(peerA)
	name, _, _, _ := table(p.srv)
	m := session(t, f, func(s *Scanner) { s.Conns().idleAfter = 40 * time.Millisecond }).Conns()
	if _, err := m.Connect(peerA, 1); err != nil {
		t.Fatal(err)
	}
	f.waitUpdates(t, []uint16{slow})
	// Requests keep arriving while the move to slow is still in flight: none
	// of them may issue a second update on top of it. The move back to fast
	// follows once the first completes, and it is asked for once.
	start := time.Now()
	for time.Since(start) < 300*time.Millisecond {
		if _, err := m.Read(peerA, name); err != nil {
			t.Fatal(err)
		}
		f.mu.Lock()
		n := len(f.updates)
		f.mu.Unlock()
		if time.Since(start) < 80*time.Millisecond && n != 1 {
			t.Fatalf("%d updates while the first was in flight", n)
		}
		time.Sleep(10 * time.Millisecond)
	}
	f.waitUpdates(t, []uint16{slow, fast, slow})
}
