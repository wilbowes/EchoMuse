package avrcp

import (
	"bytes"
	"errors"
	"sync"
	"testing"
	"time"
)

// recorder captures what a session sends, for byte-level target tests.
type recorder struct {
	mu   sync.Mutex
	pkts [][]byte
}

func (r *recorder) Send(b []byte) error {
	r.mu.Lock()
	r.pkts = append(r.pkts, append([]byte(nil), b...))
	r.mu.Unlock()
	return nil
}

func (r *recorder) take() [][]byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.pkts
	r.pkts = nil
	return p
}

// command frames f as a single AVCTP command packet.
func command(label uint8, f frame) []byte {
	p, _ := fragment(message{label: label, pid: PID, payload: f.encode()}, 672)
	return p[0]
}

// ask sends one command to s and returns the single response packet.
func ask(t *testing.T, s *Session, r *recorder, label uint8, f frame) []byte {
	t.Helper()
	if err := s.Receive(command(label, f)); err != nil {
		t.Fatal(err)
	}
	out := r.take()
	if len(out) != 1 {
		t.Fatalf("%d packets in answer", len(out))
	}
	return out[0]
}

func target(t *testing.T, cfg Config) (*Session, *recorder) {
	r := &recorder{}
	return NewSession(r, cfg), r
}

func TestUnitAndSubunitInfo(t *testing.T) {
	s, r := target(t, Config{})
	got := ask(t, s, r, 2, frame{CtypeStatus, subunitUnit, opUnitInfo, []byte{0xFF, 0xFF, 0xFF, 0xFF, 0xFF}})
	// label 2 response; STABLE, unit, UNIT INFO, 0x07, panel, SIG company ID.
	want := []byte{0x22, 0x11, 0x0E, 0x0C, 0xFF, 0x30, 0x07, 0x48, 0x00, 0x19, 0x58}
	if !bytes.Equal(got, want) {
		t.Errorf("UNIT INFO: % x, want % x", got, want)
	}
	got = ask(t, s, r, 3, frame{CtypeStatus, subunitUnit, opSubunitInfo, []byte{0x07, 0xFF, 0xFF, 0xFF, 0xFF}})
	want = []byte{0x32, 0x11, 0x0E, 0x0C, 0xFF, 0x31, 0x07, 0x48, 0xFF, 0xFF, 0xFF}
	if !bytes.Equal(got, want) {
		t.Errorf("SUBUNIT INFO: % x, want % x", got, want)
	}
	got = ask(t, s, r, 3, frame{CtypeControl, subunitUnit, opUnitInfo, []byte{0xFF, 0xFF, 0xFF, 0xFF, 0xFF}})
	if got[3] != RespNotImplemented {
		t.Errorf("UNIT INFO as CONTROL answered 0x%x", got[3])
	}
}

func TestPassThroughTarget(t *testing.T) {
	type press struct {
		op      Op
		pressed bool
	}
	var seen []press
	s, r := target(t, Config{OnPassThrough: func(op Op, pressed bool) { seen = append(seen, press{op, pressed}) }})
	for op := range supportedOps {
		for _, rel := range []bool{false, true} {
			got := ask(t, s, r, 1, frame{CtypeControl, subunitPanel, opPassThrough, passThroughOperands(op, rel)})
			f, _ := parseFrame(got[3:])
			if f.code != RespAccepted || !bytes.Equal(f.operands, passThroughOperands(op, rel)) {
				t.Errorf("%v released=%v: % x", op, rel, got)
			}
		}
	}
	if len(seen) != 2*len(supportedOps) || !seen[0].pressed || seen[1].pressed {
		t.Errorf("callbacks: %+v", seen)
	}
	seen = nil
	for _, op := range []Op{0x47, 0x00, opVendorUnique} { // record, select, vendor unique
		got := ask(t, s, r, 1, frame{CtypeControl, subunitPanel, opPassThrough, passThroughOperands(op, false)})
		if got[3] != RespNotImplemented {
			t.Errorf("%v answered 0x%x", op, got[3])
		}
	}
	got := ask(t, s, r, 1, frame{CtypeControl, subunitPanel, opPassThrough, []byte{0x44, 3}})
	if got[3] != RespNotImplemented || len(seen) != 0 {
		t.Errorf("malformed pass through: % x, callbacks %v", got, seen)
	}
}

// vendorAnswer sends an AVRCP PDU to s and returns the response code and
// the response's PDU.
func vendorAnswer(t *testing.T, s *Session, r *recorder, label, ctype, pdu uint8, params []byte) (uint8, vendorPDU) {
	t.Helper()
	got := ask(t, s, r, label, vendorFrame(ctype, pdu, params))
	f, err := parseFrame(got[3:])
	if err != nil {
		t.Fatal(err)
	}
	c, v, err := parseVendor(f.operands)
	if err != nil || c != CompanyBTSIG || v.id != pdu {
		t.Fatalf("response % x: %v", got, err)
	}
	return f.code, v
}

func TestTargetVendorPDUs(t *testing.T) {
	s, r := target(t, Config{})
	code, v := vendorAnswer(t, s, r, 0, CtypeStatus, pduGetCapabilities, []byte{capCompanyID})
	if code != RespStable || !bytes.Equal(v.params, []byte{2, 1, 0x00, 0x19, 0x58}) {
		t.Errorf("companies: %x % x", code, v.params)
	}
	code, v = vendorAnswer(t, s, r, 0, CtypeStatus, pduGetCapabilities, []byte{capEventsSupported})
	if code != RespStable || !bytes.Equal(v.params, []byte{3, 1, byte(EventPlaybackStatusChanged)}) {
		t.Errorf("events: %x % x", code, v.params)
	}
	code, v = vendorAnswer(t, s, r, 0, CtypeStatus, pduGetPlayStatus, nil)
	if code != RespStable || !bytes.Equal(v.params, []byte{0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, byte(PlayStopped)}) {
		t.Errorf("play status: %x % x", code, v.params)
	}

	for _, c := range []struct {
		name   string
		ctype  uint8
		pdu    uint8
		params []byte
		status uint8
	}{
		{"unknown capability", CtypeStatus, pduGetCapabilities, []byte{0x07}, StatusInvalidParameter},
		{"capabilities as CONTROL", CtypeControl, pduGetCapabilities, []byte{capCompanyID}, StatusInvalidCommand},
		{"capabilities, no parameter", CtypeStatus, pduGetCapabilities, nil, StatusParameterContent},
		{"volume event: the sink's, not ours", CtypeNotify, pduRegisterNotification, []byte{0x0D, 0, 0, 0, 0}, StatusInvalidParameter},
		{"short registration", CtypeNotify, pduRegisterNotification, []byte{0x01}, StatusParameterContent},
		{"registration as STATUS", CtypeStatus, pduRegisterNotification, []byte{0x01, 0, 0, 0, 0}, StatusInvalidCommand},
		{"absolute volume: we are no amplifier", CtypeControl, pduSetAbsoluteVolume, []byte{0x40}, StatusInvalidCommand},
		{"continuing with nothing to continue", CtypeControl, pduRequestContinuingResponse, []byte{0x10}, StatusInvalidParameter},
		{"unknown PDU", CtypeStatus, 0x20, nil, StatusInvalidCommand},
	} {
		code, v := vendorAnswer(t, s, r, 0, c.ctype, c.pdu, c.params)
		if code != RespRejected || !bytes.Equal(v.params, []byte{c.status}) {
			t.Errorf("%s: %x % x, want REJECTED %02x", c.name, code, v.params, c.status)
		}
	}

	// A fragmented command is invalid: commands always fit one frame.
	frag := vendorFrame(CtypeStatus, pduGetCapabilities, []byte{capCompanyID})
	frag.operands[4] = pktStart
	got := ask(t, s, r, 0, frag)
	if got[3] != RespRejected {
		t.Errorf("fragmented command answered 0x%x", got[3])
	}
	// Another company's vendor-dependent frame is not AVRCP's.
	other := frame{CtypeStatus, subunitPanel, opVendor, []byte{0x12, 0x34, 0x56, 0x10, 0, 0, 0}}
	if got := ask(t, s, r, 0, other); got[3] != RespNotImplemented || !bytes.Equal(got[4:], other.encode()[1:]) {
		t.Errorf("other company: % x", got)
	}
}

func TestPlayStatusNotification(t *testing.T) {
	s, r := target(t, Config{})
	if err := s.SetPlayStatus(PlayPaused); err != nil || len(r.take()) != 0 {
		t.Fatal("a change with no registration sent something")
	}
	code, v := vendorAnswer(t, s, r, 9, CtypeNotify, pduRegisterNotification, []byte{0x01, 0, 0, 0, 0})
	if code != RespInterim || !bytes.Equal(v.params, []byte{0x01, byte(PlayPaused)}) {
		t.Fatalf("interim: %x % x", code, v.params)
	}
	s.SetPlayStatus(PlayPaused) // no change, nothing sent
	s.SetPlayStatus(PlayPlaying)
	out := r.take()
	want := append([]byte{0x92, 0x11, 0x0E}, vendorFrame(RespChanged, pduRegisterNotification, []byte{0x01, byte(PlayPlaying)}).encode()...)
	if len(out) != 1 || !bytes.Equal(out[0], want) {
		t.Fatalf("CHANGED: % x, want % x", out, want)
	}
	// CHANGED ended the registration: the next change waits for a new one.
	s.SetPlayStatus(PlayStopped)
	if len(r.take()) != 0 {
		t.Error("a change after CHANGED was sent without a re-registration")
	}
}

func TestIPIDAndUnreadableCommands(t *testing.T) {
	s, r := target(t, Config{})
	s.Receive([]byte{0x40, 0x11, 0x0F, 0x01, 0x48, 0x7C})
	if out := r.take(); len(out) != 1 || !bytes.Equal(out[0], []byte{0x43, 0x11, 0x0F}) {
		t.Errorf("other profile: % x", out)
	}
	// Reserved nibble set: unreadable but addressable, so NOT IMPLEMENTED.
	s.Receive([]byte{0x40, 0x11, 0x0E, 0x10, 0x48, 0x7C, 0x44, 0x00})
	if out := r.take(); len(out) != 1 || out[0][3] != RespNotImplemented {
		t.Errorf("reserved bits: % x", out)
	}
	if err := s.Receive([]byte{0x40, 0x11, 0x0E, 0x00}); err == nil || s.Dropped() == 0 {
		t.Error("a 1-byte frame was not dropped")
	}
}

// A command bigger than the MTU arrives in fragments and its answer leaves
// in fragments.
func TestFragmentedCommandAndAnswer(t *testing.T) {
	s, r := target(t, Config{MTU: MinMTU})
	ops := append([]byte{byte(opVendorUnique), 100}, seq(100)...)
	pkts, _ := fragment(message{label: 5, pid: PID, payload: frame{CtypeControl, subunitPanel, opPassThrough, ops}.encode()}, MinMTU)
	for _, p := range pkts {
		if err := s.Receive(p); err != nil {
			t.Fatal(err)
		}
	}
	out := r.take()
	if len(out) < 2 {
		t.Fatalf("answer in %d packets", len(out))
	}
	var re reassembler
	var m message
	for _, p := range out {
		if len(p) > MinMTU {
			t.Fatalf("packet of %d bytes over a %d MTU", len(p), MinMTU)
		}
		m, _, _ = re.feed(p)
	}
	f, _ := parseFrame(m.payload)
	if m.label != 5 || !m.response || f.code != RespNotImplemented || !bytes.Equal(f.operands, ops) {
		t.Errorf("answer %+v", f)
	}
}

func FuzzReceive(f *testing.F) {
	f.Add(command(1, vendorFrame(CtypeStatus, pduGetCapabilities, []byte{3})))
	f.Add(command(1, frame{CtypeControl, subunitPanel, opPassThrough, []byte{0x44, 0}}))
	f.Add([]byte{0x12, 0x11, 0x0E, 0x0C, 0x48, 0x00})
	f.Fuzz(func(t *testing.T, b []byte) {
		s := NewSession(&recorder{}, Config{OnPassThrough: func(Op, bool) {}})
		s.Receive(b)
		s.Receive(b)
	})
}

func TestLabelsRunOut(t *testing.T) {
	s, _ := target(t, Config{})
	for i := 0; i < 16; i++ {
		s.pending[uint8(i)] = &pending{timer: time.NewTimer(time.Hour)}
	}
	if _, err := s.SetAbsoluteVolume(1); !errors.Is(err, ErrBusy) {
		t.Errorf("got %v", err)
	}
}
