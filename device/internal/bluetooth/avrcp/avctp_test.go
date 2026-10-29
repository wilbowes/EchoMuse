package avrcp

import (
	"bytes"
	"errors"
	"testing"
)

func seq(n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = byte(i * 7)
	}
	return b
}

// AVCTP 1.4 §6.1.1: label 3, single, command, PID 0x110E.
func TestSinglePacketBytes(t *testing.T) {
	pkts, err := fragment(message{label: 3, pid: PID, payload: []byte{0xAA}}, 672)
	if err != nil {
		t.Fatal(err)
	}
	if want := []byte{0x30, 0x11, 0x0E, 0xAA}; len(pkts) != 1 || !bytes.Equal(pkts[0], want) {
		t.Fatalf("got % x, want % x", pkts, want)
	}
	pkts, _ = fragment(message{label: 0xF, response: true, ipid: true, pid: 0x1234}, 672)
	if want := []byte{0xF3, 0x12, 0x34}; !bytes.Equal(pkts[0], want) {
		t.Fatalf("IPID response: got % x, want % x", pkts[0], want)
	}
}

// A payload that fills the MTU exactly with the 3-byte header is still one
// packet; one byte more makes a start and an end.
func TestFragmentBoundary(t *testing.T) {
	for _, mtu := range []int{MinMTU, 49, 100, 672} {
		one, _ := fragment(message{pid: PID, payload: seq(mtu - 3)}, mtu)
		if len(one) != 1 {
			t.Errorf("mtu %d: %d bytes fit one packet, got %d", mtu, mtu-3, len(one))
		}
		two, _ := fragment(message{pid: PID, payload: seq(mtu - 2)}, mtu)
		if len(two) != 2 || two[0][0]>>2&3 != pktStart || two[1][0]>>2&3 != pktEnd || two[0][1] != 2 {
			t.Errorf("mtu %d: %d bytes should be start+end, got %d packets", mtu, mtu-2, len(two))
		}
	}
}

// Every length up to an AV/C frame survives fragmentation and reassembly at
// every MTU, no packet exceeds the MTU, and the start packet's count is right.
func TestFragmentRoundTrip(t *testing.T) {
	for _, mtu := range []int{MinMTU, 49, 64, 100, 672} {
		for n := 0; n <= MaxFrame; n++ {
			in := message{label: uint8(n % 16), response: n%2 == 0, pid: PID, payload: seq(n)}
			pkts, err := fragment(in, mtu)
			if err != nil {
				t.Fatalf("mtu %d n %d: %v", mtu, n, err)
			}
			if len(pkts) > 1 && int(pkts[0][1]) != len(pkts) {
				t.Fatalf("mtu %d n %d: count %d for %d packets", mtu, n, pkts[0][1], len(pkts))
			}
			var r reassembler
			for i, p := range pkts {
				if len(p) > mtu {
					t.Fatalf("mtu %d n %d: packet %d is %d bytes", mtu, n, i, len(p))
				}
				out, ok, err := r.feed(p)
				if err != nil {
					t.Fatalf("mtu %d n %d: %v", mtu, n, err)
				}
				if ok != (i == len(pkts)-1) {
					t.Fatalf("mtu %d n %d: complete at %d of %d", mtu, n, i, len(pkts))
				}
				if ok && (out.label != in.label || out.response != in.response || out.pid != PID || !bytes.Equal(out.payload, in.payload)) {
					t.Fatalf("mtu %d n %d: round trip changed the message", mtu, n)
				}
			}
		}
	}
}

func TestFragmentClampsMTUAndRejectsBadLabel(t *testing.T) {
	pkts, _ := fragment(message{pid: PID, payload: seq(45)}, 10)
	if len(pkts) != 1 {
		t.Errorf("an MTU below %d must be treated as %d", MinMTU, MinMTU)
	}
	if _, err := fragment(message{label: 16, pid: PID}, 672); err == nil {
		t.Error("a 5-bit label was accepted")
	}
	// 255 packets is the most a start packet can count.
	if _, err := fragment(message{pid: PID, payload: seq(44 + 254*47 + 1)}, MinMTU); err == nil {
		t.Error("a message needing 256 packets was framed")
	}
}

func TestReassemblerRejects(t *testing.T) {
	start := []byte{0x14, 3, 0x11, 0x0E, 1} // label 1, start, 3 packets
	cont := []byte{0x18, 2}                 // label 1, continue
	end := []byte{0x1C, 3}                  // label 1, end
	cases := []struct {
		name string
		pkts [][]byte
	}{
		{"empty", [][]byte{{}}},
		{"short single", [][]byte{{0x10, 0x11}}},
		{"short start", [][]byte{{0x14, 2, 0x11}}},
		{"start counting one", [][]byte{{0x14, 1, 0x11, 0x0E}}},
		{"continue with nothing started", [][]byte{cont}},
		{"end with nothing started", [][]byte{end}},
		{"end before the count", [][]byte{start, end}},
		{"continue where the end belongs", [][]byte{start, cont, cont}},
		{"other label", [][]byte{start, {0x28, 2}}},
		{"other direction", [][]byte{start, {0x1A, 2}}},
		{"oversized single", [][]byte{append([]byte{0x10, 0x11, 0x0E}, seq(MaxFrame+1)...)}},
		{"oversized reassembly", [][]byte{append([]byte{0x14, 3, 0x11, 0x0E}, seq(300)...), append([]byte{0x18}, seq(300)...)}},
	}
	for _, c := range cases {
		var r reassembler
		var err error
		for _, p := range c.pkts {
			if _, _, err = r.feed(p); err != nil {
				break
			}
		}
		if !errors.Is(err, ErrMalformed) {
			t.Errorf("%s: accepted", c.name)
		}
	}
}

// A new start abandons a message in flight rather than splicing the two.
func TestReassemblerRestart(t *testing.T) {
	var r reassembler
	r.feed([]byte{0x14, 2, 0x11, 0x0E, 1})
	r.feed([]byte{0x24, 2, 0x11, 0x0E, 2})
	m, ok, err := r.feed([]byte{0x2C, 3})
	if err != nil || !ok || m.label != 2 || !bytes.Equal(m.payload, []byte{2, 3}) {
		t.Fatalf("got %+v %v %v", m, ok, err)
	}
}

func FuzzReassembler(f *testing.F) {
	f.Add([]byte{0x30, 0x11, 0x0E, 1}, []byte{0x14, 2, 0x11, 0x0E}, []byte{0x1C})
	f.Fuzz(func(t *testing.T, a, b, c []byte) {
		var r reassembler
		for _, p := range [][]byte{a, b, c} {
			if m, ok, err := r.feed(p); ok && (err != nil || len(m.payload) > MaxFrame) {
				t.Fatalf("bad message %d bytes, %v", len(m.payload), err)
			}
		}
	})
}
