package bluetooth

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// Live capture from the 2026-07-12 raw-HCI validation on Office: an Apple
// device advert (31-byte payload, RSSI -62) as it came off /dev/stpbt.
var liveAppleAdvert = []byte{
	0x04, 0x3e, 0x2b, 0x02, 0x01, 0x00, 0x00, 0x5e, 0xd8, 0xee, 0xc0,
	0xbf, 0x74, 0x1f, 0x02, 0x01, 0x06, 0x1b, 0xff, 0x4c, 0x00, 0x03,
	0x16, 0x11, 0x00, 0x00, 0x02, 0x77, 0x0a, 0x0a, 0x01, 0x81, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0xd1, 0xc2,
}

func TestParseAdvReportsLiveCapture(t *testing.T) {
	adverts := parseAdvReports(liveAppleAdvert)
	if len(adverts) != 1 {
		t.Fatalf("got %d adverts, want 1", len(adverts))
	}
	a := adverts[0]
	if a.Addr != "74:bf:c0:ee:d8:5e" {
		t.Errorf("addr = %q, want 74:bf:c0:ee:d8:5e", a.Addr)
	}
	if a.AddrType != 0 {
		t.Errorf("addrType = %d, want 0 (public)", a.AddrType)
	}
	if a.Rssi != -62 {
		t.Errorf("rssi = %d, want -62 (0xc2)", a.Rssi)
	}
	if len(a.Data) != 31 {
		t.Fatalf("data len = %d, want 31", len(a.Data))
	}
	// AD structure: flags (02 01 06) then Apple manufacturer data.
	if !bytes.Equal(a.Data[:5], []byte{0x02, 0x01, 0x06, 0x1b, 0xff}) {
		t.Errorf("data head = % x", a.Data[:5])
	}
}

func TestParseAdvReportsRandomAddrNegativeRssi(t *testing.T) {
	// Minimal random-address report, empty payload, RSSI -93 (0xa3).
	pkt := []byte{
		0x04, 0x3e, 0x0b, 0x02, 0x01,
		0x03,                               // event_type ADV_NONCONN_IND
		0x01,                               // addr_type random
		0x01, 0x02, 0x03, 0x04, 0x05, 0x06, // addr LE
		0x00, // data_len
		0xa3, // rssi
	}
	adverts := parseAdvReports(pkt)
	if len(adverts) != 1 {
		t.Fatalf("got %d adverts, want 1", len(adverts))
	}
	if adverts[0].Addr != "06:05:04:03:02:01" {
		t.Errorf("addr = %q", adverts[0].Addr)
	}
	if adverts[0].AddrType != 1 || adverts[0].Rssi != -93 || len(adverts[0].Data) != 0 {
		t.Errorf("got %+v", adverts[0])
	}
}

func TestParseAdvReportsIgnoresOtherEvents(t *testing.T) {
	cc := []byte{0x04, 0x0e, 0x04, 0x01, 0x03, 0x0c, 0x00} // Reset CC
	if got := parseAdvReports(cc); got != nil {
		t.Errorf("command complete parsed as adverts: %+v", got)
	}
	truncated := liveAppleAdvert[:20]
	if got := parseAdvReports(truncated); len(got) != 0 {
		t.Errorf("truncated report yielded adverts: %+v", got)
	}
}

func TestH4ParserFragmentationAndCoalescing(t *testing.T) {
	stream := append([]byte{}, liveAppleAdvert...)
	stream = append(stream, 0x04, 0x0e, 0x04, 0x01, 0x03, 0x0c, 0x00) // Reset CC
	stream = append(stream, liveAppleAdvert...)

	// Feed one byte at a time — worst-case fragmentation.
	var p h4Parser
	var pkts [][]byte
	for _, b := range stream {
		pkts = append(pkts, p.Feed([]byte{b})...)
	}
	if len(pkts) != 3 {
		t.Fatalf("byte-wise feed: got %d packets, want 3", len(pkts))
	}
	if !bytes.Equal(pkts[0], liveAppleAdvert) || !bytes.Equal(pkts[2], liveAppleAdvert) {
		t.Error("reassembled packets don't match input")
	}

	// Whole stream in one read — coalesced packets.
	var p2 h4Parser
	pkts = p2.Feed(stream)
	if len(pkts) != 3 {
		t.Fatalf("coalesced feed: got %d packets, want 3", len(pkts))
	}
}

func TestH4ParserDropsOnUnknownType(t *testing.T) {
	var p h4Parser
	if pkts := p.Feed([]byte{0x77, 0x01, 0x02}); len(pkts) != 0 {
		t.Errorf("unknown type produced packets: %v", pkts)
	}
	// Parser must recover for subsequent clean input.
	if pkts := p.Feed(liveAppleAdvert); len(pkts) != 1 {
		t.Errorf("parser did not recover after desync: %d packets", len(pkts))
	}
}

func TestParseCommandComplete(t *testing.T) {
	// Read_BD_ADDR complete with addr 00:71:47:96:8F:FA (LE on the wire).
	pkt := []byte{0x04, 0x0e, 0x0a, 0x01, 0x09, 0x10, 0x00,
		0xfa, 0x8f, 0x96, 0x47, 0x71, 0x00}
	cc, ok := parseCommandComplete(pkt)
	if !ok {
		t.Fatal("not parsed")
	}
	if cc.opcode != opReadBdAddr {
		t.Errorf("opcode = %04x, want %04x", cc.opcode, opReadBdAddr)
	}
	if cc.status != 0 {
		t.Errorf("status = %d", cc.status)
	}
	if got := formatBdAddr(cc.params); got != "00:71:47:96:8F:FA" {
		t.Errorf("bdaddr = %q", got)
	}
}

func TestScanParamsUnits(t *testing.T) {
	p := scanParams(100, 50)
	if p[0] != 0x00 {
		t.Error("scan type not passive")
	}
	if got := binary.LittleEndian.Uint16(p[1:3]); got != 160 { // 100ms / 0.625
		t.Errorf("interval units = %d, want 160", got)
	}
	if got := binary.LittleEndian.Uint16(p[3:5]); got != 80 {
		t.Errorf("window units = %d, want 80", got)
	}
	// Clamping
	if got := binary.LittleEndian.Uint16(scanParams(0, 0)[1:3]); got != 0x0004 {
		t.Errorf("low clamp = %d", got)
	}
	if got := binary.LittleEndian.Uint16(scanParams(100000, 100000)[1:3]); got != 0x4000 {
		t.Errorf("high clamp = %d", got)
	}
}

func TestBuildCommand(t *testing.T) {
	pkt := buildCommand(opLESetScanEnable, []byte{0x01, 0x00})
	want := []byte{0x01, 0x0c, 0x20, 0x02, 0x01, 0x00}
	if !bytes.Equal(pkt, want) {
		t.Errorf("got % x, want % x", pkt, want)
	}
}
