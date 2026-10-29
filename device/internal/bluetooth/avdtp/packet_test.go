package avdtp

import (
	"bytes"
	"testing"
)

var allSignals = []SignalID{
	SigDiscover, SigGetCapabilities, SigSetConfiguration, SigGetConfiguration,
	SigReconfigure, SigOpen, SigStart, SigClose, SigSuspend, SigAbort,
	SigSecurityControl, SigGetAllCapabilities, SigDelayReport,
}

func reassemble(t *testing.T, pkts [][]byte) Message {
	t.Helper()
	var r Reassembler
	for i, p := range pkts {
		m, ok, err := r.Feed(p)
		if err != nil {
			t.Fatalf("packet %d: %v", i, err)
		}
		if ok != (i == len(pkts)-1) {
			t.Fatalf("packet %d of %d: complete=%v", i, len(pkts), ok)
		}
		if ok {
			return m
		}
	}
	t.Fatal("no message")
	return Message{}
}

// The header's bit layout (§8.4.1): label in the high nibble, packet type,
// message type in the low two bits.
func TestSinglePacketBytes(t *testing.T) {
	pkts, err := Fragment(Message{Label: 3, Type: MsgCommand, Signal: SigDiscover}, DefaultMTU)
	if err != nil || len(pkts) != 1 || !bytes.Equal(pkts[0], []byte{0x30, 0x01}) {
		t.Fatalf("got % x, %v", pkts, err)
	}
	pkts, _ = Fragment(Message{Label: 0xF, Type: MsgReject, Signal: SigStart, Params: []byte{0x04, 0x31}}, DefaultMTU)
	if !bytes.Equal(pkts[0], []byte{0xF3, 0x07, 0x04, 0x31}) {
		t.Fatalf("got % x", pkts[0])
	}
}

func TestEverySignalRoundTrips(t *testing.T) {
	for _, sig := range allSignals {
		for mt := MsgCommand; mt <= MsgReject; mt++ {
			in := Message{Label: uint8(sig) & 0xF, Type: mt, Signal: sig, Params: []byte{1, 2, 3}}
			pkts, err := Fragment(in, MinSignalMTU)
			if err != nil {
				t.Fatal(err)
			}
			if got := reassemble(t, pkts); got.Label != in.Label || got.Type != mt ||
				got.Signal != sig || !bytes.Equal(got.Params, in.Params) {
				t.Errorf("%v/%d: got %+v", sig, mt, got)
			}
		}
	}
}

// Every size either side of each boundary, at the minimum MTU and others:
// each packet within the MTU, NOSP right, and the message back intact.
func TestFragmentBoundaries(t *testing.T) {
	for _, mtu := range []int{MinSignalMTU, 49, 50, DefaultMTU} {
		for n := 0; n <= 3*mtu+5; n++ {
			params := make([]byte, n)
			for i := range params {
				params[i] = byte(i)
			}
			pkts, err := Fragment(Message{Label: 7, Signal: SigSetConfiguration, Params: params}, mtu)
			if err != nil {
				t.Fatal(err)
			}
			wantSingle := 2+n <= mtu
			if (len(pkts) == 1) != wantSingle {
				t.Fatalf("mtu %d n %d: %d packets", mtu, n, len(pkts))
			}
			for i, p := range pkts {
				if len(p) > mtu {
					t.Fatalf("mtu %d n %d: packet %d is %d bytes", mtu, n, i, len(p))
				}
			}
			if !wantSingle {
				if int(pkts[0][1]) != len(pkts) || pkts[0][0]>>2&3 != pktStart ||
					pkts[len(pkts)-1][0]>>2&3 != pktEnd {
					t.Fatalf("mtu %d n %d: bad start/end headers", mtu, n)
				}
				for _, p := range pkts[1 : len(pkts)-1] {
					if p[0]>>2&3 != pktContinue {
						t.Fatalf("mtu %d n %d: middle packet not a continuation", mtu, n)
					}
				}
			}
			if got := reassemble(t, pkts); !bytes.Equal(got.Params, params) {
				t.Fatalf("mtu %d n %d: params differ", mtu, n)
			}
		}
	}
}

func TestFragmentRefuses(t *testing.T) {
	for name, tc := range map[string]struct {
		m   Message
		mtu int
	}{
		"mtu below 48": {Message{Signal: SigDiscover}, 47},
		"label 16":     {Message{Label: 16, Signal: SigDiscover}, 48},
		"signal 0x40":  {Message{Signal: 0x40}, 48},
		"256 packets":  {Message{Signal: SigDiscover, Params: make([]byte, 45+254*47+1)}, 48},
	} {
		if _, err := Fragment(tc.m, tc.mtu); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// Exactly 255 packets is the ceiling, and allowed.
	if p, err := Fragment(Message{Signal: SigDiscover, Params: make([]byte, 45+254*47)}, 48); err != nil || len(p) != 255 {
		t.Errorf("255 packets: %d, %v", len(p), err)
	}
}

func TestReassemblerRefusesHostileSequences(t *testing.T) {
	start := func(label, nosp byte) []byte { return []byte{label<<4 | pktStart<<2, nosp, byte(SigOpen), 0xAA} }
	cont := func(label byte) []byte { return []byte{label<<4 | pktContinue<<2, 0xBB} }
	end := func(label byte) []byte { return []byte{label<<4 | pktEnd<<2, 0xCC} }
	for name, seq := range map[string][][]byte{
		"empty":                {{}},
		"short single":         {{0x00}},
		"short start":          {{pktStart << 2, 2}},
		"start nosp 1":         {start(1, 1)},
		"start nosp 0":         {start(1, 0)},
		"orphan continue":      {cont(1)},
		"orphan end":           {end(1)},
		"end too early":        {start(1, 3), end(1)},
		"continue as last":     {start(1, 2), cont(1)},
		"label changes":        {start(1, 3), cont(2)},
		"message type changes": {start(1, 2), {1<<4 | pktEnd<<2 | byte(MsgAccept), 0}},
	} {
		var r Reassembler
		var err error
		for _, p := range seq {
			if _, _, err = r.Feed(p); err != nil {
				break
			}
		}
		if err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// A start abandons an unfinished message; a single packet in between is
	// whole on its own.
	var r Reassembler
	r.Feed(start(1, 3))
	if m, ok, err := r.Feed([]byte{0x20, byte(SigClose), 0x04}); !ok || err != nil || m.Signal != SigClose {
		t.Fatalf("single after start: %+v %v %v", m, ok, err)
	}
	if _, _, err := r.Feed(end(1)); err == nil {
		t.Fatal("the abandoned message's end was accepted")
	}
}

func TestReassemblerBoundsTheMessage(t *testing.T) {
	var r Reassembler
	r.Feed([]byte{pktStart << 2, 255, byte(SigOpen)})
	big := append([]byte{pktContinue << 2}, make([]byte, 1000)...)
	var err error
	for i := 0; i < 10 && err == nil; i++ {
		_, _, err = r.Feed(big)
	}
	if err == nil {
		t.Fatal("buffered past maxMessage")
	}
}

func FuzzReassembler(f *testing.F) {
	f.Add([]byte{0x30, 0x01}, []byte{0x14, 0x02, 0x03, 0x00}, []byte{0x1C, 0x00})
	f.Add([]byte{0x04, 0x03, 0x03}, []byte{0x08}, []byte{0x0C, 0xFF})
	f.Fuzz(func(t *testing.T, a, b, c []byte) {
		var r Reassembler
		for _, p := range [][]byte{a, b, c} {
			m, ok, err := r.Feed(p)
			if ok && (err != nil || m.Label > 0xF || m.Signal > 0x3F || len(m.Params) > maxMessage) {
				t.Fatalf("bad message %+v %v", m, err)
			}
		}
	})
}
