package bluetooth

import (
	"bytes"
	"net"
	"testing"
)

func TestBuildACLFraming(t *testing.T) {
	// ATT Exchange MTU Request (185) on handle 0x0040.
	got := buildACL(0x0040, cidATT, []byte{0x02, 0xB9, 0x00})
	want := []byte{0x02, 0x40, 0x00, 0x07, 0x00, 0x03, 0x00, 0x04, 0x00, 0x02, 0xB9, 0x00}
	if !bytes.Equal(got, want) {
		t.Fatalf("got % x want % x", got, want)
	}
	// The handle is 12 bits; the flag bits above it stay clear (PB=00).
	if got := buildACL(0xFFFF, cidATT, nil); got[1] != 0xFF || got[2] != 0x0F {
		t.Fatalf("handle bytes % x", got[1:3])
	}
}

func TestACLReassembly(t *testing.T) {
	payload := []byte("a name longer than one fragment")
	whole := buildACL(0x0040, cidATT, payload)[5:] // L2CAP header + payload

	frag := func(pb byte, data []byte) []byte {
		pkt := []byte{h4TypeACL, 0x40, pb << 4, byte(len(data)), 0}
		return append(pkt, data...)
	}
	var r aclReassembler

	// A continuation with nothing started is dropped.
	if _, _, _, ok := r.Feed(frag(0x1, whole[:5])); ok {
		t.Fatal("orphan continuation produced a PDU")
	}
	// Split inside the L2CAP header, then across the payload.
	if _, _, _, ok := r.Feed(frag(0x2, whole[:3])); ok {
		t.Fatal("complete after 3 bytes")
	}
	if _, _, _, ok := r.Feed(frag(0x1, whole[3:10])); ok {
		t.Fatal("complete after 10 bytes")
	}
	h, cid, got, ok := r.Feed(frag(0x1, whole[10:]))
	if !ok || h != 0x0040 || cid != cidATT || !bytes.Equal(got, payload) {
		t.Fatalf("ok=%v h=%#x cid=%#x got %q", ok, h, cid, got)
	}
	// A new start discards an unfinished PDU on the same handle.
	r.Feed(frag(0x2, whole[:6]))
	_, _, got, ok = r.Feed(frag(0x2, whole))
	if !ok || !bytes.Equal(got, payload) {
		t.Fatalf("restart: ok=%v got %q", ok, got)
	}
}

func TestCreateConnParams(t *testing.T) {
	mac, _ := net.ParseMAC("AA:BB:CC:DD:EE:FF")
	p := createConnParams(mac, 1, 0, 30)
	if len(p) != 25 {
		t.Fatalf("len %d", len(p))
	}
	if p[5] != 1 || p[12] != 0 || !bytes.Equal(p[6:12], []byte{0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA}) {
		t.Fatalf("peer % x", p[5:12])
	}
	// 30ms is 24 units of 1.25ms, min and max alike.
	if !bytes.Equal(p[13:17], []byte{24, 0, 24, 0}) {
		t.Fatalf("interval % x", p[13:17])
	}
	// Clamped to the spec's range: 7.5ms to 4s.
	if lo := createConnParams(mac, 0, 1, 1); lo[13] != 6 || lo[14] != 0 || lo[12] != 1 {
		t.Fatalf("low clamp % x", lo[13:15])
	}
	if hi := createConnParams(mac, 0, 0, 60000); hi[13] != 0x80 || hi[14] != 0x0C {
		t.Fatalf("high clamp % x", hi[13:15])
	}
}

func TestParseConnComplete(t *testing.T) {
	pkt := []byte{h4TypeEvent, evtLEMeta, 19, leSubeventConnComplete,
		0x00,       // status
		0x40, 0x00, // handle
		0x00,                               // role: central
		0x01,                               // peer address type
		0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, // peer address
		0x18, 0x00, // interval 24 = 30ms
		0x00, 0x00, // latency
		0xF4, 0x01, // timeout 500 = 5s
		0x00}
	cc, ok := parseConnComplete(pkt)
	if !ok || cc.status != 0 || cc.handle != 0x40 || cc.intervalMs != 30 || cc.timeoutMs != 5000 {
		t.Fatalf("ok=%v %+v", ok, cc)
	}
	if _, ok := parseConnComplete(pkt[:20]); ok {
		t.Fatal("short packet parsed")
	}
}
