package bluetooth

// The factory Bluetooth address, loaded into the chip on every session.
//
// Amazon writes each Echo's address into idme (/proc/idme/bt_mac_addr, twelve
// hex digits); on FireOS its own stack loads it after power-on. Nothing did
// under emOS or under our scanner, so every Dot 2 reported its NVRAM default
// (00:00:46:81:63:01, seen fleet-wide 2026-09-22) and the donut a different
// default. A passive scan does not care; a connection, pairing or an A2DP
// sink does, and two Echoes with one address cannot both be peers.
//
// The write is MediaTek's vendor command 0xFC1A, six bytes little-endian
// (the one Linux's btmtk driver uses for set_bdaddr). It does not persist:
// an HCI Reset or power cycle restores the default, so it is sent after the
// reset that opens every session, and read back to say whether it took.

import (
	"encoding/hex"
	"os"
	"strings"
)

const opMTKWriteBdAddr = 0x3F<<10 | 0x001A // 0xFC1A

var idmeBtMacPath = "/proc/idme/bt_mac_addr"

// factoryBdAddr reads the factory address, or nil when there is none worth
// loading.
func factoryBdAddr() []byte {
	b, err := os.ReadFile(idmeBtMacPath)
	if err != nil {
		return nil
	}
	return parseBdAddr(string(b))
}

// parseBdAddr takes idme's twelve hex digits (colons tolerated, trailing
// whitespace and NULs dropped) and returns them most-significant octet first,
// or nil for anything that is not a usable public address: the wrong length,
// a non-hex digit, all zeros or all ones (unprogrammed flash), or a first
// octet with the group bit set.
func parseBdAddr(s string) []byte {
	s = strings.TrimRight(s, "\x00 \t\r\n")
	s = strings.ReplaceAll(s, ":", "")
	if len(s) != 12 {
		return nil
	}
	a, err := hex.DecodeString(s)
	if err != nil {
		return nil
	}
	zero, ones := true, true
	for _, o := range a {
		zero = zero && o == 0
		ones = ones && o == 0xFF
	}
	if zero || ones || a[0]&0x01 != 0 {
		return nil
	}
	return a
}

// bdAddrParams is the address in HCI's byte order: least-significant octet
// first, as Read_BD_ADDR returns it.
func bdAddrParams(a []byte) []byte {
	p := make([]byte, 6)
	for i := range p {
		p[i] = a[5-i]
	}
	return p
}
