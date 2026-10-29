package bluetooth

import (
	"encoding/binary"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeSpeaker is a BR/EDR device on the far side of fakeBR. Its L2CAP is
// our own engine, so the far end of every channel is real.
type fakeSpeaker struct {
	addr     string
	name     string
	class    uint32
	pairable bool     // in pairing mode
	key      [16]byte // what it holds for us; zero = no pairing
	legacy   bool     // asks for a PIN instead of SSP
	gotPIN   string
	handle   uint16
	sig      *l2Signal
	opened   chan *L2Channel
	accept   l2Accept
	in       chan l2Frame // what the host sent, read in order as on a link
}

// fakeBR is a dual-mode controller as far as BR/EDR goes: just enough of the
// command set, and the event sequences the spec gives for each.
type fakeBR struct {
	mu       sync.Mutex
	out      chan []byte
	closed   bool
	speakers map[string]*fakeSpeaker
	links    map[uint16]*fakeSpeaker
	next     uint16
	reasm    aclReassembler
	scanMode []byte
	scanLE   []byte
	issued   [16]byte // the key the next pairing produces
	authFor  map[uint16]bool
}

func newFakeBR() *fakeBR {
	f := &fakeBR{out: make(chan []byte, 512), speakers: map[string]*fakeSpeaker{}, links: map[uint16]*fakeSpeaker{},
		next: 0x0B, authFor: map[uint16]bool{}}
	for i := range f.issued {
		f.issued[i] = byte(0xA0 + i)
	}
	return f
}

func (f *fakeBR) speaker(addr string) *fakeSpeaker {
	sp := &fakeSpeaker{addr: addr, name: "Speaker " + addr[len(addr)-2:], class: 0x240414,
		opened: make(chan *L2Channel, 4), accept: func(uint16) uint16 { return connSuccess }}
	f.speakers[addr] = sp
	return sp
}

func (f *fakeBR) Read(p []byte) (int, error) {
	pkt, ok := <-f.out
	if !ok {
		return 0, os.ErrClosed
	}
	return copy(p, pkt), nil
}

func (f *fakeBR) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.closed {
		f.closed = true
		close(f.out)
	}
	return nil
}

func (f *fakeBR) emit(pkt []byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.closed {
		f.out <- pkt
	}
}

func (f *fakeBR) event(code byte, params ...byte) {
	f.emit(append([]byte{h4TypeEvent, code, byte(len(params))}, params...))
}

func (f *fakeBR) complete(op uint16, status byte, ret ...byte) {
	f.event(evtCommandComplete, append([]byte{1, byte(op), byte(op >> 8), status}, ret...)...)
}

func (f *fakeBR) status(op uint16, status byte) {
	f.event(evtCommandStatus, status, 1, byte(op), byte(op>>8))
}

func le(addr string) []byte {
	b, _ := bdaddrLE(addr)
	return b
}

func (f *fakeBR) connComplete(status byte, sp *fakeSpeaker) {
	p := append([]byte{status, byte(sp.handle), byte(sp.handle >> 8)}, le(sp.addr)...)
	f.event(evtConnComplete, append(p, linkTypeACL, 0)...)
}

// up registers the link and gives the speaker its L2CAP engine.
func (f *fakeBR) up(sp *fakeSpeaker) {
	f.mu.Lock()
	sp.handle = f.next
	f.next++
	f.links[sp.handle] = sp
	f.mu.Unlock()
	sp.sig = newL2Signal(func(cid uint16, pdu []byte) error {
		for _, pkt := range fragmentACLPB(sp.handle, cid, pdu, 27, 0x2) {
			f.emit(pkt)
		}
		return nil
	}, sp.accept, func(c *L2Channel) { sp.opened <- c }, 672)
	sp.in = make(chan l2Frame, 256)
	go func(sig *l2Signal, in chan l2Frame) {
		for fr := range in {
			sig.onFrame(fr.cid, fr.pdu)
		}
	}(sp.sig, sp.in)
}

func (f *fakeBR) byAddr(params []byte) *fakeSpeaker {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.speakers[formatBdAddr(params[0:6])]
}

func (f *fakeBR) byHandle(params []byte) *fakeSpeaker {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.links[binary.LittleEndian.Uint16(params[0:2])&0x0FFF]
}

func (f *fakeBR) Write(pkt []byte) (int, error) {
	switch pkt[0] {
	case h4TypeCommand:
		f.command(binary.LittleEndian.Uint16(pkt[1:3]), append([]byte(nil), pkt[4:]...))
	case h4TypeACL:
		handle := binary.LittleEndian.Uint16(pkt[1:3]) & 0x0FFF
		f.event(evtNumCompletedPackets, 1, byte(handle), byte(handle>>8), 1, 0)
		f.mu.Lock()
		h, cid, pdu, ok := f.reasm.Feed(pkt)
		sp := f.links[h]
		f.mu.Unlock()
		if ok && sp != nil {
			sp.in <- l2Frame{cid, append([]byte(nil), pdu...)}
		}
	}
	return len(pkt), nil
}

func (f *fakeBR) command(op uint16, p []byte) {
	switch op {
	case opReadBufferSize:
		f.complete(op, 0, 0xFD, 0x03, 0, 8, 0, 0, 0) // 1021 bytes x 8
	case opLEReadBufferSize:
		f.complete(op, 0, 0, 0, 0)
	case opLESetScanEnable:
		f.mu.Lock()
		f.scanLE = append(f.scanLE, p[0])
		f.mu.Unlock()
		f.complete(op, 0)
	case opWriteScanEnable:
		f.mu.Lock()
		f.scanMode = append(f.scanMode, p[0])
		f.mu.Unlock()
		f.complete(op, 0)
	case opCreateConn:
		sp := f.byAddr(p)
		f.status(op, 0)
		if sp == nil {
			go f.event(evtConnComplete, append(append([]byte{0x04, 0, 0}, p[0:6]...), linkTypeACL, 0)...)
			return
		}
		go func() { f.up(sp); f.connComplete(0, sp) }()
	case opAcceptConnReq:
		sp := f.byAddr(p)
		f.status(op, 0)
		go func() { f.up(sp); f.connComplete(0, sp) }()
	case opRejectConnReq:
		f.status(op, 0)
		go f.event(evtConnComplete, append(append([]byte{p[6], 0, 0}, p[0:6]...), linkTypeACL, 0)...)
	case opAuthRequested:
		sp := f.byHandle(p)
		f.status(op, 0)
		go f.event(evtLinkKeyRequest, le(sp.addr)...)
	case opLinkKeyReqReply:
		sp := f.byAddr(p)
		f.complete(op, 0, p[0:6]...)
		var key [16]byte
		copy(key[:], p[6:22])
		st := byte(0)
		if key != sp.key {
			st = hciKeyMissing
		}
		go f.event(evtAuthComplete, st, byte(sp.handle), byte(sp.handle>>8))
	case opLinkKeyReqNegReply:
		sp := f.byAddr(p)
		f.complete(op, 0, p[0:6]...)
		switch {
		case !sp.pairable:
			go f.event(evtAuthComplete, hciKeyMissing, byte(sp.handle), byte(sp.handle>>8))
		case sp.legacy:
			go f.event(evtPinCodeRequest, le(sp.addr)...)
		default:
			go f.event(evtIOCapRequest, le(sp.addr)...)
		}
	case opIOCapReqReply:
		sp := f.byAddr(p)
		f.complete(op, 0, p[0:6]...)
		go func() {
			f.event(evtIOCapResponse, append(le(sp.addr), ioCapNoInputNoOutput, 0, authReqGeneralBonding)...)
			f.event(evtUserConfirmRequest, append(le(sp.addr), 0x40, 0xE2, 0x01, 0x00)...)
		}()
	case opIOCapReqNegReply, opUserConfirmNegReply, opPinCodeReqNegReply:
		sp := f.byAddr(p)
		f.complete(op, 0, p[0:6]...)
		go func() {
			if op != opPinCodeReqNegReply {
				f.event(evtSimplePairingDone, append([]byte{hciPairingNotAllowed}, le(sp.addr)...)...)
			}
			if sp.handle != 0 {
				f.event(evtAuthComplete, hciAuthFailure, byte(sp.handle), byte(sp.handle>>8))
			}
		}()
	case opUserConfirmReqReply, opPinCodeReqReply:
		sp := f.byAddr(p)
		f.complete(op, 0, p[0:6]...)
		if op == opPinCodeReqReply {
			sp.gotPIN = string(p[7 : 7+p[6]])
		}
		go func() {
			if op == opUserConfirmReqReply {
				f.event(evtSimplePairingDone, append([]byte{0}, le(sp.addr)...)...)
			}
			sp.key = f.issued
			f.event(evtLinkKeyNotification, append(append(le(sp.addr), f.issued[:]...), 0x04)...)
			f.event(evtAuthComplete, 0, byte(sp.handle), byte(sp.handle>>8))
		}()
	case opSetConnEncryption:
		sp := f.byHandle(p)
		f.status(op, 0)
		go f.event(evtEncryptionChange, 0, byte(sp.handle), byte(sp.handle>>8), 1)
	case opDisconnect:
		sp := f.byHandle(p)
		f.status(op, 0)
		go f.dropLink(sp, hciReasonLocalHost)
	case opInquiry:
		f.status(op, 0)
		go func() {
			f.mu.Lock()
			sps := make([]*fakeSpeaker, 0, len(f.speakers))
			for _, sp := range f.speakers {
				sps = append(sps, sp)
			}
			f.mu.Unlock()
			for i, sp := range sps {
				r := make([]byte, 255)
				r[0] = 1
				copy(r[1:7], le(sp.addr))
				r[9], r[10], r[11] = byte(sp.class), byte(sp.class>>8), byte(sp.class>>16)
				r[14] = 0xC4
				if i%2 == 0 { // half carry their name in the EIR
					r[15], r[16] = byte(1+len(sp.name)), 0x09
					copy(r[17:], sp.name)
				}
				f.event(evtExtInquiryResult, r...)
			}
			f.event(evtInquiryComplete, 0)
		}()
	case opRemoteNameReq:
		sp := f.byAddr(p)
		f.status(op, 0)
		go func() {
			r := make([]byte, 255)
			copy(r[1:7], p[0:6])
			copy(r[7:], sp.name)
			f.event(evtRemoteNameComplete, r...)
		}()
	default:
		f.complete(op, 0)
	}
}

func (f *fakeBR) dropLink(sp *fakeSpeaker, reason byte) {
	f.mu.Lock()
	delete(f.links, sp.handle)
	h := sp.handle
	f.mu.Unlock()
	if sp.sig != nil {
		sp.sig.linkDown()
	}
	f.event(evtDisconnectComplete, 0, byte(h), byte(h>>8), reason)
}

// pageUs has the speaker connect to the Echo.
func (f *fakeBR) pageUs(sp *fakeSpeaker) {
	f.event(evtConnRequest, append(le(sp.addr), byte(sp.class), byte(sp.class>>8), byte(sp.class>>16), linkTypeACL)...)
}

// classicSession runs a radio session with Classic attached and the proxy off.
func classicSession(t *testing.T, f *fakeBR, setup func(*Classic)) (*Classic, *Scanner) {
	t.Helper()
	c := NewClassic(filepath.Join(t.TempDir(), "paired.json"), "EchoMuse Test")
	if setup != nil {
		setup(c)
	}
	s := NewScanner(nil)
	s.SetClassic(c)
	s.audioOn = true
	s.scanOff.Store(true)
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
		if _, err := c.running(); err == nil {
			// attach also writes scan enable last; wait for it.
			f.mu.Lock()
			n := len(f.scanMode)
			f.mu.Unlock()
			if n > 0 {
				return c, s
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("classic never attached")
		}
		time.Sleep(2 * time.Millisecond)
	}
}

const spkA = "11:22:33:44:55:66"
const spkB = "11:22:33:44:55:77"

func TestPairWithSecureSimplePairing(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	l, err := c.Pair(spkA, "Kitchen")
	if err != nil {
		t.Fatalf("pair: %v", err)
	}
	if !l.Encrypted() {
		t.Fatal("paired link not encrypted")
	}
	key, ok := c.keys.key(spkA)
	if !ok || key != f.issued {
		t.Fatalf("key not stored: %v %x", ok, key)
	}
	if p := c.Paired(); len(p) != 1 || p[0].Addr != spkA || p[0].Name != "Kitchen" {
		t.Fatalf("paired list %+v", p)
	}
	// Persisted, without the key in the listing.
	c2 := NewClassic(c.keys.path, "")
	if k, ok := c2.keys.key(spkA); !ok || k != f.issued {
		t.Fatal("the pairing did not survive a reload")
	}
	f.mu.Lock()
	last := f.scanMode[len(f.scanMode)-1]
	f.mu.Unlock()
	if last != scanPage {
		t.Fatalf("not connectable after pairing: scan mode %d", last)
	}
}

func TestPairLegacyPIN(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable, sp.legacy = true, true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatalf("pair: %v", err)
	}
	if sp.gotPIN != "0000" {
		t.Fatalf("PIN sent %q", sp.gotPIN)
	}
}

func TestPairFailsWhenSpeakerNotInPairingMode(t *testing.T) {
	f := newFakeBR()
	f.speaker(spkA)
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); !errors.Is(err, ErrPairingRefused) {
		t.Fatalf("want a pairing failure, got %v", err)
	}
	if c.keys.known(spkA) {
		t.Fatal("a key was kept from a failed pairing")
	}
}

func TestConnectUsesStoredKey(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	f.dropLink(sp, 0x13)
	waitFor(t, func() bool { return len(c.Links()) == 0 })
	sp.pairable = false // a reconnect must not need pairing mode
	l, err := c.Connect(spkA)
	if err != nil {
		t.Fatalf("reconnect: %v", err)
	}
	if !l.Encrypted() {
		t.Fatal("not encrypted")
	}
}

func TestConnectReportsAForgottenPairing(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	f.dropLink(sp, 0x13)
	waitFor(t, func() bool { return len(c.Links()) == 0 })
	sp.pairable, sp.key = false, [16]byte{} // the speaker was reset
	if _, err := c.Connect(spkA); !errors.Is(err, ErrKeyMissing) {
		t.Fatalf("want ErrKeyMissing, got %v", err)
	}
}

func TestConnectRefusesAnUnpairedDevice(t *testing.T) {
	f := newFakeBR()
	f.speaker(spkA)
	c, _ := classicSession(t, f, nil)
	if _, err := c.Connect(spkA); !errors.Is(err, ErrNotPaired) {
		t.Fatalf("got %v", err)
	}
}

// Nobody asked to pair with B, so B may neither connect nor pair.
func TestUnsolicitedDeviceIsRefused(t *testing.T) {
	f := newFakeBR()
	spB := f.speaker(spkB)
	c, _ := classicSession(t, f, nil)
	w := c.await(func(p []byte) bool { return p[1] == evtConnComplete }, 4)
	defer c.unwait(w)
	f.pageUs(spB)
	select {
	case p := <-w.ch:
		if cc, _ := parseClassicConnComplete(p); cc.status != hciUnacceptableAddr {
			t.Fatalf("an unpaired device was let in: status 0x%02x", cc.status)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no answer to the connection request")
	}
	// And a pairing it starts on its own is refused at the IO capability step.
	f.up(spB)
	w2 := c.await(func(p []byte) bool { return p[1] == evtSimplePairingDone }, 4)
	defer c.unwait(w2)
	f.event(evtIOCapRequest, le(spkB)...)
	select {
	case p := <-w2.ch:
		if p[3] != hciPairingNotAllowed {
			t.Fatalf("pairing status 0x%02x", p[3])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the pairing attempt was never answered")
	}
	if c.keys.known(spkB) {
		t.Fatal("an unsolicited device got a key")
	}
	// Nor does a key notification for it get stored.
	f.event(evtLinkKeyNotification, append(append(le(spkB), f.issued[:]...), 0x04)...)
	time.Sleep(50 * time.Millisecond)
	if c.keys.known(spkB) {
		t.Fatal("a key notification for an unknown device was stored")
	}
}

func TestPairedDeviceMayConnectToUs(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	f.dropLink(sp, 0x13)
	waitFor(t, func() bool { return len(c.Links()) == 0 })
	f.pageUs(sp)
	waitFor(t, func() bool { return len(c.Links()) == 1 })
}

func TestProfileChannelsNeedEncryption(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	got := make(chan *L2Channel, 2)
	c, _ := classicSession(t, f, func(c *Classic) {
		c.OnChannel = func(_ *ClassicLink, ch *L2Channel) { got <- ch }
	})
	l, err := c.page(spkA)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return sp.sig != nil })
	if _, err := sp.sig.Connect(psmAVDTP); err == nil || !strings.Contains(err.Error(), "0x0003") {
		t.Fatalf("AVDTP on an unencrypted link: %v", err)
	}
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	if !l.Encrypted() {
		t.Fatal("pairing did not encrypt the link it found")
	}
	ch, err := sp.sig.Connect(psmAVDTP)
	if err != nil {
		t.Fatalf("AVDTP after encryption: %v", err)
	}
	select {
	case in := <-got:
		if in.PSM != psmAVDTP || in.Remote != ch.Local {
			t.Fatalf("channel %+v", in)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("OnChannel never called")
	}
	if _, err := sp.sig.Connect(0x1001); err == nil {
		t.Fatal("an unknown PSM was accepted")
	}
}

func TestSDPIsServedWithoutEncryption(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, func(c *Classic) {
		c.SDP = func(req []byte, mtu int) []byte { return append([]byte("rsp:"), req...) }
	})
	if _, err := c.page(spkA); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return sp.sig != nil })
	ch, err := sp.sig.Connect(psmSDP)
	if err != nil {
		t.Fatalf("SDP channel: %v", err)
	}
	r := newL2Requester(ch)
	rsp, err := r.Request([]byte("q"))
	if err != nil || string(rsp) != "rsp:q" {
		t.Fatalf("%q %v", rsp, err)
	}
}

func TestOutboundChannelOverTheLink(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	l, err := c.Pair(spkA, "")
	if err != nil {
		t.Fatal(err)
	}
	ch, err := l.Connect(psmAVDTP)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	var far *L2Channel
	select {
	case far = <-sp.opened:
	case <-time.After(2 * time.Second):
		t.Fatal("the speaker never saw the channel")
	}
	data := make(chan []byte, 1)
	far.SetOnData(func(b []byte) { data <- append([]byte(nil), b...) })
	big := make([]byte, 600) // more than one 27-byte ACL fragment each way
	for i := range big {
		big[i] = byte(i)
	}
	if err := ch.Send(big); err != nil {
		t.Fatal(err)
	}
	select {
	case b := <-data:
		if len(b) != 600 || b[599] != byte(599&0xFF) {
			t.Fatalf("payload %d bytes", len(b))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no data at the speaker")
	}
}

func TestLinkDownEndsChannelsAndReports(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	down := make(chan string, 1)
	c, _ := classicSession(t, f, func(c *Classic) {
		c.OnLinkDown = func(addr string, _ byte) { down <- addr }
	})
	l, err := c.Pair(spkA, "")
	if err != nil {
		t.Fatal(err)
	}
	ch, err := l.Connect(psmAVDTP)
	if err != nil {
		t.Fatal(err)
	}
	f.dropLink(sp, 0x08)
	select {
	case a := <-down:
		if a != spkA {
			t.Fatalf("reported %s", a)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no link-down report")
	}
	select {
	case <-ch.Gone():
	default:
		t.Fatal("channel outlived its link")
	}
	select {
	case <-l.Gone():
	default:
		t.Fatal("link not marked gone")
	}
}

func TestDiscoverFindsAudioSinksWithNames(t *testing.T) {
	f := newFakeBR()
	f.speaker(spkA)
	f.speaker(spkB)
	phone := f.speaker("11:22:33:44:55:88")
	phone.class = 0x5A020C // phone: not an audio sink
	c, _ := classicSession(t, f, nil)
	res, err := c.Discover(1280 * time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 2 {
		t.Fatalf("results %+v", res)
	}
	for _, r := range res {
		if r.Name != "Speaker "+r.Addr[len(r.Addr)-2:] {
			t.Errorf("%s named %q", r.Addr, r.Name)
		}
	}
}

func TestForgetDropsLinkAndKey(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	if err := c.Forget(spkA); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return len(c.Links()) == 0 })
	if c.keys.known(spkA) {
		t.Fatal("key kept")
	}
	f.mu.Lock()
	last := f.scanMode[len(f.scanMode)-1]
	f.mu.Unlock()
	if last != 0 {
		t.Fatal("still connectable with nothing paired")
	}
}

// Classic and an LE link share the controller: the LE side must hand every
// BR/EDR packet across, and the audio-only session must not scan.
func TestAudioOnlySessionDoesNotScan(t *testing.T) {
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	c, _ := classicSession(t, f, nil)
	if _, err := c.Pair(spkA, ""); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, v := range f.scanLE {
		if v != 0 {
			t.Fatalf("LE scan enabled in an audio-only session: %v", f.scanLE)
		}
	}
}

func TestKeyStoreBoundsAndCorruption(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "k.json")
	ks, _ := openKeyStore(path)
	for i := 0; i < maxPaired+3; i++ {
		mac := net.HardwareAddr{0, 0, 0, 0, 0, byte(i)}
		if err := ks.put(mac.String(), [16]byte{byte(i)}, 4, ""); err != nil {
			t.Fatal(err)
		}
	}
	if n := len(ks.list()); n != maxPaired {
		t.Fatalf("%d records kept", n)
	}
	st, _ := os.Stat(path)
	before := st.ModTime()
	time.Sleep(10 * time.Millisecond)
	last := net.HardwareAddr{0, 0, 0, 0, 0, byte(maxPaired + 2)}.String()
	if err := ks.put(last, [16]byte{byte(maxPaired + 2)}, 4, ""); err != nil {
		t.Fatal(err)
	}
	if st, _ := os.Stat(path); !st.ModTime().Equal(before) {
		t.Fatal("an unchanged key rewrote the file")
	}
	os.WriteFile(path, []byte("{not json"), 0600)
	if ks, err := openKeyStore(path); err == nil || len(ks.list()) != 0 {
		t.Fatal("a corrupt file must read as empty and say so")
	}
	os.WriteFile(path, []byte(`[{"addr":"zz","key":"00"},{"addr":"00:00:00:00:00:01","key":"short"}]`), 0600)
	if ks, _ := openKeyStore(path); len(ks.list()) != 0 {
		t.Fatal("malformed records were loaded")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition never held")
		}
		time.Sleep(2 * time.Millisecond)
	}
}
