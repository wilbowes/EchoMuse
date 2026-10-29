package avdtp

import (
	"bytes"
	"errors"
	"testing"
	"time"
)

// The acceptor's answers, command by command, against the reject format each
// signal defines (§8.6-8.19).
func TestAcceptorAnswers(t *testing.T) {
	setConf := func(caps ...byte) []byte { return append([]byte{1 << 2, 2 << 2}, caps...) }
	type step struct {
		sig    SignalID
		params []byte
		typ    MessageType
		resp   []byte // nil: not checked
	}
	refuseAAC := Config{DelayReporting: true}
	for name, tc := range map[string]struct {
		cfg   Config
		open  bool // drive to Open first
		steps []step
	}{
		"discover": {steps: []step{
			{SigDiscover, nil, MsgAccept, []byte{0x04, 0x00}},
			{SigDiscover, []byte{0}, MsgReject, []byte{byte(CodeBadLength)}},
		}},
		"discover reports in use": {open: true, steps: []step{
			{SigDiscover, nil, MsgAccept, []byte{0x06, 0x00}},
		}},
		"capabilities": {cfg: Config{DelayReporting: true}, steps: []step{
			{SigGetCapabilities, []byte{1 << 2}, MsgAccept, []byte{1, 0, 7, 6, 0, 0, 0xFF, 0xFF, 2, 0x35}},
			{SigGetAllCapabilities, []byte{1 << 2}, MsgAccept, []byte{1, 0, 7, 6, 0, 0, 0xFF, 0xFF, 2, 0x35, 8, 0}},
			{SigGetCapabilities, []byte{2 << 2}, MsgReject, []byte{byte(CodeBadACPSEID)}},
			{SigGetCapabilities, nil, MsgReject, []byte{byte(CodeBadLength)}},
		}},
		"set configuration refusals": {cfg: refuseAAC, steps: []step{
			{SigSetConfiguration, []byte{1 << 2}, MsgReject, []byte{0, byte(CodeBadLength)}},
			{SigSetConfiguration, []byte{3 << 2, 2 << 2}, MsgReject, []byte{0, byte(CodeBadACPSEID)}},
			{SigSetConfiguration, []byte{1 << 2, 0}, MsgReject, []byte{0, byte(CodeBadPayloadFormat)}},
			{SigSetConfiguration, setConf(append(sbcConfigBytes, 0x09, 0x00)...), MsgReject, []byte{9, byte(CodeBadServCategory)}},
			{SigSetConfiguration, setConf(append(sbcConfigBytes, 0x03, 0x03, 1, 1, 1)...), MsgReject, []byte{3, byte(CodeInvalidCapabilities)}},
			{SigSetConfiguration, setConf(0x01, 0x01, 0x00), MsgReject, []byte{1, byte(CodeBadMediaTransportFormat)}},
			{SigSetConfiguration, setConf(0x07, 0x06, 0x00, 0x02, 1, 2, 3, 4), MsgReject, []byte{1, byte(CodeInvalidCapabilities)}},
			{SigSetConfiguration, setConf(0x01, 0x00), MsgReject, []byte{7, byte(CodeInvalidCapabilities)}},
			{SigSetConfiguration, setConf(0x01, 0x00, 0x07, 0x06, 0x00, 0x02, 1, 2, 3, 4), MsgReject, []byte{7, byte(CodeUnsupportedConfiguration)}},
			{SigGetConfiguration, []byte{1 << 2}, MsgReject, []byte{byte(CodeSEPNotInUse)}},
			{SigOpen, []byte{1 << 2}, MsgReject, []byte{byte(CodeBadState)}},
			{SigDelayReport, []byte{1 << 2, 0, 1}, MsgReject, []byte{byte(CodeBadState)}},
			{SigSetConfiguration, setConf(append(sbcConfigBytes, 0x08, 0x00)...), MsgAccept, nil},
			{SigSetConfiguration, setConf(sbcConfigBytes...), MsgReject, []byte{0, byte(CodeSEPInUse)}},
			{SigStart, []byte{1 << 2}, MsgReject, []byte{1 << 2, byte(CodeBadState)}},
			{SigGetConfiguration, []byte{1 << 2}, MsgAccept, append(append([]byte(nil), sbcConfigBytes...), 0x08, 0x00)},
			{SigDelayReport, []byte{1 << 2, 0x05, 0xDC}, MsgAccept, nil},
		}},
		"delay reporting not offered": {steps: []step{
			{SigSetConfiguration, setConf(append(sbcConfigBytes, 0x08, 0x00)...), MsgReject, []byte{8, byte(CodeInvalidCapabilities)}},
		}},
		"accept callback chooses the code": {cfg: Config{Accept: func(Codec) error { return &CodeError{CodeInvalidCapabilities} }}, steps: []step{
			{SigSetConfiguration, setConf(sbcConfigBytes...), MsgReject, []byte{7, byte(CodeInvalidCapabilities)}},
		}},
		"accept callback plain error": {cfg: Config{Accept: func(Codec) error { return errors.New("no") }}, steps: []step{
			{SigSetConfiguration, setConf(sbcConfigBytes...), MsgReject, []byte{7, byte(CodeUnsupportedConfiguration)}},
		}},
		"open stream": {open: true, steps: []step{
			{SigStart, []byte{1 << 2, 2 << 2}, MsgReject, []byte{2 << 2, byte(CodeBadACPSEID)}},
			{SigSuspend, []byte{1 << 2}, MsgReject, []byte{1 << 2, byte(CodeBadState)}},
			{SigStart, nil, MsgReject, []byte{0, byte(CodeBadLength)}},
			{SigReconfigure, append([]byte{1 << 2}, 0x01, 0x00), MsgReject, []byte{1, byte(CodeInvalidCapabilities)}},
			{SigReconfigure, append([]byte{1 << 2}, 0x07, 0x06, 0, 0, 0x11, 0x15, 2, 0x20), MsgAccept, nil},
			{SigStart, []byte{1 << 2}, MsgAccept, nil},
			{SigReconfigure, append([]byte{1 << 2}, 0x07, 0x06, 0, 0, 0x11, 0x15, 2, 0x20), MsgReject, []byte{0, byte(CodeBadState)}},
			{SigSecurityControl, []byte{1 << 2, 0}, MsgReject, []byte{byte(CodeBadState)}},
			{SigClose, []byte{2 << 2}, MsgReject, []byte{byte(CodeBadACPSEID)}},
			{SigClose, []byte{1 << 2}, MsgAccept, nil},
			{SigClose, []byte{1 << 2}, MsgReject, []byte{byte(CodeBadState)}},
			{SigReconfigure, []byte{1 << 2}, MsgReject, []byte{0, byte(CodeSEPNotInUse)}},
		}},
		"unknown signal": {steps: []step{
			{0x3F, []byte{1, 2}, MsgGeneralReject, []byte{}},
			{0x0E, nil, MsgGeneralReject, []byte{}},
		}},
	} {
		t.Run(name, func(t *testing.T) {
			sc := newScript(tc.cfg)
			if tc.open {
				openByPeer(t, sc)
			}
			for i, st := range tc.steps {
				sc.command(st.sig, st.params...)
				m := sc.next(t, st.sig)
				if m.Type != st.typ || (st.resp != nil && !bytes.Equal(m.Params, st.resp)) {
					t.Fatalf("step %d (%v % x): type %d % x, want %d % x",
						i, st.sig, st.params, m.Type, m.Params, st.typ, st.resp)
				}
			}
		})
	}
}

// ABORT has no reject: an ABORT for an SEP that is not ours goes unanswered,
// and ours is accepted in any state.
func TestAbortIsNeverRejected(t *testing.T) {
	sc := newScript(Config{})
	sc.command(SigAbort, 2<<2)
	sc.command(SigAbort, 1<<2)
	if m := sc.next(t, SigAbort); m.Type != MsgAccept {
		t.Fatalf("type %d", m.Type)
	}
	select {
	case m := <-sc.sent:
		t.Fatalf("extra response %+v", m)
	case <-time.After(20 * time.Millisecond):
	}
	openByPeer(t, sc)
	sc.command(SigAbort, 1<<2)
	sc.next(t, SigAbort)
	if sc.s.State() != StateIdle { // no media channel: straight to Idle
		t.Fatalf("state %v", sc.s.State())
	}
}

// A command split across packets at the minimum MTU is answered like any
// other.
func TestFragmentedCommandIsAnswered(t *testing.T) {
	sc := newScript(Config{})
	// Codec info long enough to need a second packet.
	codec := Codec{MediaAudio, CodecSBC, append([]byte{0x21, 0x15, 2, 0x35}, make([]byte, 50)...)}
	params := append([]byte{1 << 2, 2 << 2}, Capabilities{MediaTransport: true, Codec: &codec}.Encode()...)
	pkts, err := Fragment(Message{Label: 5, Type: MsgCommand, Signal: SigSetConfiguration, Params: params}, MinSignalMTU)
	if err != nil || len(pkts) < 2 {
		t.Fatal(len(pkts), err)
	}
	for _, p := range pkts {
		sc.s.Feed(p)
	}
	if m := sc.next(t, SigSetConfiguration); m.Type != MsgAccept || m.Label != 5 {
		t.Fatalf("%+v", m)
	}
}

func TestSourceCommandsNeedTheRightState(t *testing.T) {
	sc := newScript(Config{})
	for name, f := range map[string]func() error{
		"start": sc.s.Start, "suspend": sc.s.Suspend, "close": sc.s.Close,
	} {
		if err := f(); !errors.Is(err, ErrBadState) {
			t.Errorf("%s in Idle: %v", name, err)
		}
	}
	if err := sc.s.Abort(); err != nil {
		t.Errorf("abort in Idle: %v", err)
	}
	if err := sc.s.ReportDelay(10); !errors.Is(err, ErrBadState) {
		t.Errorf("delay report in Idle: %v", err)
	}
}
