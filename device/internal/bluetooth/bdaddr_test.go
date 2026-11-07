package bluetooth

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestParseBdAddrTakesIdmesFormAndRefusesUnusableOnes(t *testing.T) {
	want := []byte{0x68, 0x9A, 0x87, 0x01, 0x02, 0x03}
	for _, s := range []string{"689A87010203", "689a87010203\n", "689A87010203\x00",
		"68:9A:87:01:02:03", "689A87010203 \r\n"} {
		if got := parseBdAddr(s); !bytes.Equal(got, want) {
			t.Errorf("%q: %x, want %x", s, got, want)
		}
	}
	for _, s := range []string{"", "689A8701020", "689A870102030", "689A8701020G",
		"000000000000", "FFFFFFFFFFFF", "ffffffffffff",
		"019A87010203", // group bit: not a device address
		"689A87 010203"} {
		if got := parseBdAddr(s); got != nil {
			t.Errorf("%q: %x, want nil", s, got)
		}
	}
}

// HCI carries BD_ADDR least-significant octet first; formatBdAddr reads it
// that way, so the round trip is the proof.
func TestBdAddrParamsAreHCIByteOrder(t *testing.T) {
	a := []byte{0x68, 0x9A, 0x87, 0x01, 0x02, 0x03}
	p := bdAddrParams(a)
	if !bytes.Equal(p, []byte{0x03, 0x02, 0x01, 0x87, 0x9A, 0x68}) {
		t.Fatalf("params %x", p)
	}
	if got := formatBdAddr(p); got != "68:9A:87:01:02:03" {
		t.Errorf("read back as %s", got)
	}
	if opMTKWriteBdAddr != 0xFC1A {
		t.Errorf("opcode %#x", opMTKWriteBdAddr)
	}
}

func TestFactoryBdAddrIsNilWithoutIdme(t *testing.T) {
	old := idmeBtMacPath
	defer func() { idmeBtMacPath = old }()
	idmeBtMacPath = filepath.Join(t.TempDir(), "missing")
	if factoryBdAddr() != nil {
		t.Error("an address from nowhere")
	}
	idmeBtMacPath = filepath.Join(t.TempDir(), "bt_mac_addr")
	os.WriteFile(idmeBtMacPath, []byte("689A87010203\n"), 0o644)
	if a := factoryBdAddr(); len(a) != 6 {
		t.Errorf("got %x", a)
	}
}
