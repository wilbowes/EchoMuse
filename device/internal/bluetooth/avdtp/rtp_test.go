package avdtp

import (
	"bytes"
	"errors"
	"testing"
)

// SBC 44.1kHz joint stereo, 16 blocks, 8 subbands, bitpool 53: 119 bytes and
// 128 samples a frame.
const (
	sbcFrameLen     = 119
	samplesPerFrame = 128
)

func frames(n, size int) [][]byte {
	out := make([][]byte, n)
	for i := range out {
		out[i] = bytes.Repeat([]byte{byte(i + 1)}, size)
	}
	return out
}

func TestPacketHeaderBytes(t *testing.T) {
	p := Packetiser{MTU: DefaultMTU, SSRC: 0x01020304, Seq: 0xABCD, Timestamp: 0x11223344}
	pkt, n, err := p.Pack(frames(2, 4), samplesPerFrame)
	if err != nil || n != 2 {
		t.Fatal(n, err)
	}
	want := []byte{0x80, 96, 0xAB, 0xCD, 0x11, 0x22, 0x33, 0x44, 0x01, 0x02, 0x03, 0x04, 0x02,
		1, 1, 1, 1, 2, 2, 2, 2}
	if !bytes.Equal(pkt, want) {
		t.Fatalf("got  % x\nwant % x", pkt, want)
	}
}

func TestFramesPerPacket(t *testing.T) {
	for _, tc := range []struct{ mtu, frame, want int }{
		{DefaultMTU, sbcFrameLen, 5}, // (672-13)/119
		{895, sbcFrameLen, 7},
		{13 + sbcFrameLen, sbcFrameLen, 1},
		{12 + sbcFrameLen, sbcFrameLen, 0},
		{MinSignalMTU, sbcFrameLen, 0},
		{DefaultMTU, 10, MaxSBCFrames}, // the 4-bit count caps it
		{DefaultMTU, 0, 0},
	} {
		if got := FramesPerPacket(tc.mtu, tc.frame); got != tc.want {
			t.Errorf("mtu %d frame %d: %d, want %d", tc.mtu, tc.frame, got, tc.want)
		}
	}
}

func TestPackFillsToTheMTUAndAdvances(t *testing.T) {
	p := Packetiser{MTU: DefaultMTU, Seq: 0xFFFF, Timestamp: 0xFFFFFF00}
	all := frames(12, sbcFrameLen)
	var total int
	for i, want := range []int{5, 5, 2} {
		pkt, n, err := p.Pack(all[total:], samplesPerFrame)
		if err != nil || n != want || len(pkt) > p.MTU {
			t.Fatalf("packet %d: n=%d len=%d err=%v", i, n, len(pkt), err)
		}
		m, err := ParseMediaPacket(pkt)
		if err != nil || m.Frames != n || len(m.Payload) != n*sbcFrameLen {
			t.Fatalf("packet %d parsed: %+v %v", i, m, err)
		}
		total += n
	}
	// Three packets from 0xFFFF wrap to 2; the timestamp counts samples and
	// wraps too.
	if want := uint32(12*samplesPerFrame - 0x100); p.Seq != 2 || p.Timestamp != want {
		t.Fatalf("seq %d ts %#x", p.Seq, p.Timestamp)
	}
	if _, _, err := p.Pack(nil, samplesPerFrame); !errors.Is(err, ErrNoFrames) {
		t.Fatal(err)
	}
	small := Packetiser{MTU: MinSignalMTU}
	if _, _, err := small.Pack(all, samplesPerFrame); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatal(err)
	}
	if small.Seq != 0 {
		t.Fatal("a failed Pack advanced the sequence")
	}
}

func TestParseMediaPacketHonoursRTPOptions(t *testing.T) {
	// One CSRC, a one-word extension, two bytes of padding, F|S|L set.
	pkt := []byte{0xB1, 0xE0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3,
		9, 9, 9, 9, // CSRC
		0xBE, 0xDE, 0, 1, 7, 7, 7, 7, // extension
		0xE1, 0xAA, 0xBB, // SBC header, one frame
		0, 2} // padding
	m, err := ParseMediaPacket(pkt)
	if err != nil {
		t.Fatal(err)
	}
	if !m.Marker || m.PayloadType != 0x60 || m.Seq != 1 || m.Timestamp != 2 || m.SSRC != 3 ||
		!m.Fragmented || !m.Start || !m.Last || m.Frames != 1 || !bytes.Equal(m.Payload, []byte{0xAA, 0xBB}) {
		t.Fatalf("got %+v", m)
	}
	for name, bad := range map[string][]byte{
		"short":              make([]byte, 12),
		"version 1":          append([]byte{0x40}, make([]byte, 12)...),
		"padding too large":  append([]byte{0xA0}, append(make([]byte, 11), 0, 0xFF)...),
		"padding zero":       append([]byte{0xA0}, append(make([]byte, 11), 1, 0)...),
		"CSRC past end":      append([]byte{0x8F}, make([]byte, 12)...),
		"extension past end": append([]byte{0x90}, make([]byte, 14)...),
	} {
		if _, err := ParseMediaPacket(bad); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
