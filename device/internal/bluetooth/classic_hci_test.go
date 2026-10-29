package bluetooth

import (
	"bytes"
	"testing"
)

func evt(code byte, params ...byte) []byte {
	return append([]byte{h4TypeEvent, code, byte(len(params))}, params...)
}

func TestBdaddrLE(t *testing.T) {
	b, err := bdaddrLE("00:11:22:33:44:55")
	if err != nil || !bytes.Equal(b, []byte{0x55, 0x44, 0x33, 0x22, 0x11, 0x00}) {
		t.Fatalf("%x %v", b, err)
	}
	if formatBdAddr(b) != "00:11:22:33:44:55" {
		t.Fatalf("round trip: %s", formatBdAddr(b))
	}
	for _, bad := range []string{"", "00:11:22:33:44", "00:11:22:33:44:55:66:77", "zz:11:22:33:44:55"} {
		if _, err := bdaddrLE(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestInquiryParamsUseGIAC(t *testing.T) {
	if p := inquiryParams(8); !bytes.Equal(p, []byte{0x33, 0x8B, 0x9E, 8, 0}) {
		t.Fatalf("%x", p)
	}
}

func TestCreateConnParamsBREDR(t *testing.T) {
	addr := []byte{1, 2, 3, 4, 5, 6}
	p := createConnParamsBREDR(addr, 0x1234)
	if len(p) != 13 || !bytes.Equal(p[0:6], addr) || p[10] != 0x34 || p[11] != 0x92 || p[12] != 1 {
		t.Fatalf("%x", p)
	}
	if p := createConnParamsBREDR(addr, 0); p[10] != 0 || p[11] != 0 {
		t.Fatalf("a zero clock offset must not be marked valid: %x", p)
	}
}

func TestLocalNameParams(t *testing.T) {
	p := localNameParams("Kitchen")
	if len(p) != 248 || string(p[:7]) != "Kitchen" || p[7] != 0 {
		t.Fatalf("%q", p[:10])
	}
	long := bytes.Repeat([]byte("é"), 200) // 400 bytes
	p = localNameParams(string(long))
	if p[247] != 0 {
		t.Fatal("the name must leave a NUL in the field")
	}
	if n := bytes.IndexByte(p, 0); n%2 != 0 {
		t.Fatalf("cut inside a rune at %d", n)
	}
}

func TestParseInquiryResultRSSI(t *testing.T) {
	// Two responses, laid out field by field (§7.7.33).
	p := []byte{2,
		0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x16, 0x15, 0x14, 0x13, 0x12, 0x11, // addrs
		1, 1, // psrm
		0, 0, // reserved
		0x14, 0x04, 0x24, 0x0C, 0x01, 0x5A, // class: speaker, then a phone-ish
		0x34, 0x12, 0x00, 0x80, // clock offsets
		0xC4, 0xB0} // rssi -60, -80
	r := parseInquiryResults(evt(evtInquiryResultRSSI, p...))
	if len(r) != 2 {
		t.Fatalf("%d results", len(r))
	}
	if r[0].Addr != "01:02:03:04:05:06" || r[0].Class != 0x240414 || r[0].RSSI != -60 || r[0].clockOffset != 0x1234 {
		t.Fatalf("first: %+v", r[0])
	}
	if r[1].Addr != "11:12:13:14:15:16" || r[1].RSSI != -80 || r[1].clockOffset != 0 {
		t.Fatalf("second: %+v", r[1])
	}
	if !r[0].IsAudioSink() || r[1].IsAudioSink() {
		t.Fatalf("audio sink classification: %v %v", r[0].IsAudioSink(), r[1].IsAudioSink())
	}
	// A length that does not match the count is refused, not over-read.
	if parseInquiryResults(evt(evtInquiryResultRSSI, p[:len(p)-1]...)) != nil {
		t.Fatal("short event parsed")
	}
}

func TestParseInquiryResultStandard(t *testing.T) {
	p := []byte{1, 6, 5, 4, 3, 2, 1, 1, 0, 0, 0x14, 0x04, 0x24, 0x00, 0x00}
	r := parseInquiryResults(evt(evtInquiryResult, p...))
	if len(r) != 1 || r[0].Class != 0x240414 || r[0].RSSI != 0 {
		t.Fatalf("%+v", r)
	}
}

func TestParseExtendedInquiryResult(t *testing.T) {
	p := make([]byte, 255)
	p[0] = 1
	copy(p[1:7], []byte{6, 5, 4, 3, 2, 1})
	p[9], p[10], p[11] = 0x14, 0x04, 0x24
	p[14] = 0xD8 // -40
	eir := []byte{0x02, 0x01, 0x06, 0x04, 0x08, 'S', 'p', 'k', 0x08, 0x09, 'S', 'p', 'e', 'a', 'k', 'e', 'r'}
	copy(p[15:], eir)
	r := parseInquiryResults(evt(evtExtInquiryResult, p...))
	if len(r) != 1 || r[0].Name != "Speaker" || r[0].RSSI != -40 || r[0].Addr != "01:02:03:04:05:06" {
		t.Fatalf("%+v", r)
	}
	if parseInquiryResults(evt(evtExtInquiryResult, p[:200]...)) != nil {
		t.Fatal("short extended result parsed")
	}
}

func TestEIRName(t *testing.T) {
	cases := []struct {
		eir  []byte
		want string
	}{
		{nil, ""},
		{[]byte{0x04, 0x08, 'a', 'b', 'c'}, "abc"},
		{[]byte{0x04, 0x08, 'a', 'b', 'c', 0x03, 0x09, 'x', 'y'}, "xy"}, // complete wins
		{[]byte{0x00, 0x09, 'x'}, ""},                                   // zero length ends it
		{[]byte{0x09, 0x09, 'x'}, ""},                                   // overruns the buffer
		{[]byte{0x03, 0x09, 0xFF, 'a'}, "a"},                            // invalid UTF-8 dropped
	}
	for _, tc := range cases {
		if got := eirName(tc.eir); got != tc.want {
			t.Errorf("%x: %q want %q", tc.eir, got, tc.want)
		}
	}
}

func TestParseRemoteName(t *testing.T) {
	p := make([]byte, 255)
	copy(p[1:7], []byte{6, 5, 4, 3, 2, 1})
	copy(p[7:], "Lounge Soundbar")
	addr, name, ok := parseRemoteName(evt(evtRemoteNameComplete, p...))
	if !ok || addr != "01:02:03:04:05:06" || name != "Lounge Soundbar" {
		t.Fatalf("%v %s %q", ok, addr, name)
	}
	p[0] = 0x04 // page timeout
	if _, _, ok := parseRemoteName(evt(evtRemoteNameComplete, p...)); ok {
		t.Fatal("a failed request parsed")
	}
}

func TestParseClassicConnComplete(t *testing.T) {
	cc, ok := parseClassicConnComplete(evt(evtConnComplete, 0, 0x0B, 0x10, 6, 5, 4, 3, 2, 1, 1, 0))
	if !ok || cc.status != 0 || cc.handle != 0x00B || cc.addr != "01:02:03:04:05:06" || cc.linkType != linkTypeACL || cc.encrypt {
		t.Fatalf("%+v", cc)
	}
}

func TestParseLinkKeyNotification(t *testing.T) {
	p := append([]byte{6, 5, 4, 3, 2, 1}, bytes.Repeat([]byte{0xAB}, 16)...)
	p = append(p, 0x04)
	addr, key, typ, ok := parseLinkKeyNotification(evt(evtLinkKeyNotification, p...))
	if !ok || addr != "01:02:03:04:05:06" || key[0] != 0xAB || key[15] != 0xAB || typ != 4 {
		t.Fatalf("%v %s %x %d", ok, addr, key, typ)
	}
}

func TestPairingReplies(t *testing.T) {
	addr := []byte{1, 2, 3, 4, 5, 6}
	if p := ioCapReplyParams(addr); !bytes.Equal(p, []byte{1, 2, 3, 4, 5, 6, 0x03, 0x00, 0x04}) {
		t.Fatalf("io cap: %x", p)
	}
	p := pinReplyParams(addr, "0000")
	if len(p) != 23 || p[6] != 4 || string(p[7:11]) != "0000" || p[11] != 0 {
		t.Fatalf("pin: %x", p)
	}
	if p := pinReplyParams(addr, "01234567890123456789"); p[6] != 16 {
		t.Fatalf("a PIN longer than 16 must be cut to the field: %d", p[6])
	}
}

func TestEventMaskEnablesPairingAndLE(t *testing.T) {
	m := uint64(eventMaskClassic)
	for _, code := range []byte{evtConnComplete, evtConnRequest, evtAuthComplete, evtEncryptionChange,
		evtLinkKeyRequest, evtLinkKeyNotification, evtPinCodeRequest, evtExtInquiryResult,
		evtIOCapRequest, evtIOCapResponse, evtUserConfirmRequest, evtSimplePairingDone, evtLEMeta} {
		if m&(1<<(code-1)) == 0 {
			t.Errorf("event 0x%02x masked off", code)
		}
	}
}
