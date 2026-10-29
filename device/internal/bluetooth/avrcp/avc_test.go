package avrcp

import (
	"bytes"
	"testing"
)

func TestParseFrame(t *testing.T) {
	f, err := parseFrame([]byte{CtypeStatus, subunitUnit, opUnitInfo, 0xFF})
	if err != nil || f.code != CtypeStatus || f.subunit != 0xFF || f.opcode != opUnitInfo || !bytes.Equal(f.operands, []byte{0xFF}) {
		t.Fatalf("got %+v %v", f, err)
	}
	if !bytes.Equal(f.encode(), []byte{1, 0xFF, 0x30, 0xFF}) {
		t.Errorf("encode: % x", f.encode())
	}
	for name, b := range map[string][]byte{
		"short":              {0, 0x48},
		"reserved nibble":    {0x10, 0x48, 0x7C},
		"extended type":      {0, 0x1E << 3, 0x7C},
		"extended ID":        {0, 0x09<<3 | 5, 0x7C},
		"over an AV/C frame": append([]byte{0, 0x48, 0}, seq(MaxFrame)...),
	} {
		if _, err := parseFrame(b); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// Panel Subunit §9.4: the state flag is bit 7 (1 released), and the
// operation data length must match what follows.
func TestPassThroughOperands(t *testing.T) {
	if b := passThroughOperands(OpPlay, false); !bytes.Equal(b, []byte{0x44, 0}) {
		t.Errorf("press: % x", b)
	}
	if b := passThroughOperands(OpPlay, true); !bytes.Equal(b, []byte{0xC4, 0}) {
		t.Errorf("release: % x", b)
	}
	op, rel, err := parsePassThrough([]byte{0xC6, 2, 9, 9})
	if err != nil || op != OpPause || !rel {
		t.Errorf("got %v %v %v", op, rel, err)
	}
	for _, b := range [][]byte{{0x44}, {0x44, 1}, {0x44, 0, 0}} {
		if _, _, err := parsePassThrough(b); err == nil {
			t.Errorf("% x accepted", b)
		}
	}
}

// AVRCP 1.6 §6.3.1: company ID, PDU ID, packet type, 2-byte length.
func TestVendorPDU(t *testing.T) {
	b := vendorPDU{id: pduSetAbsoluteVolume, params: []byte{0x40}}.operands()
	if want := []byte{0x00, 0x19, 0x58, 0x50, 0x00, 0x00, 0x01, 0x40}; !bytes.Equal(b, want) {
		t.Fatalf("got % x, want % x", b, want)
	}
	c, v, err := parseVendor(b)
	if err != nil || c != CompanyBTSIG || v.id != pduSetAbsoluteVolume || !bytes.Equal(v.params, []byte{0x40}) {
		t.Fatalf("got %06x %+v %v", c, v, err)
	}
	for _, bad := range [][]byte{b[:2], b[:6], append(b[:7:7], 1, 2), b[:7]} {
		if _, _, err := parseVendor(bad); err == nil {
			t.Errorf("% x accepted", bad)
		}
	}
	// The company is still reported when the rest is unreadable.
	if c, _, _ := parseVendor([]byte{0x12, 0x34, 0x56}); c != 0x123456 {
		t.Errorf("company %06x", c)
	}
}

func TestVolumeConversion(t *testing.T) {
	for _, c := range []struct {
		pct int
		abs uint8
	}{{-5, 0}, {0, 0}, {1, 1}, {50, 64}, {99, 126}, {100, 127}, {150, 127}} {
		if got := VolumeToAbs(c.pct); got != c.abs {
			t.Errorf("VolumeToAbs(%d) = %d, want %d", c.pct, got, c.abs)
		}
	}
	for _, c := range []struct {
		abs uint8
		pct int
	}{{0, 0}, {1, 1}, {64, 50}, {126, 99}, {127, 100}, {0x80, 0}, {0xFF, 100}} {
		if got := AbsToVolume(c.abs); got != c.pct {
			t.Errorf("AbsToVolume(%#x) = %d, want %d", c.abs, got, c.pct)
		}
	}
	// Every one of our levels survives the round trip, and the map is monotonic.
	prev := -1
	for p := 0; p <= 100; p++ {
		a := VolumeToAbs(p)
		if AbsToVolume(a) != p {
			t.Errorf("%d -> %d -> %d", p, a, AbsToVolume(a))
		}
		if int(a) <= prev {
			t.Errorf("not increasing at %d", p)
		}
		prev = int(a)
	}
}
