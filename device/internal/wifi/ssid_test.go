package wifi

import (
	"bytes"
	"strings"
	"testing"
)

// Every case here is an SSID the standard allows and wpa_supplicant can join,
// which the previous code refused, truncated or wrote back wrongly.

func TestUnescapeSSIDReversesPrintfEncode(t *testing.T) {
	cases := []struct {
		in   string
		want []byte
	}{
		{"plain", []byte("plain")},
		{"My Home WiFi", []byte("My Home WiFi")},
		{" padded ", []byte(" padded ")},
		{"Bob's", []byte("Bob's")},
		{`say \"hi\"`, []byte(`say "hi"`)},
		{`back\\slash`, []byte(`back\slash`)},
		{`Caf\xc3\xa9`, []byte("Café")},
		{`\xf0\x9f\x8f\xa0 home`, []byte("🏠 home")},
		{`tab\there`, []byte("tab\there")},
		{`\e\n\r`, []byte{0x1b, '\n', '\r'}},
		{`\xff\xfe`, []byte{0xff, 0xfe}},
		{`\x00\x00`, []byte{0, 0}},
		{`trailing\`, []byte(`trailing\`)},
		{`bad\xZZ`, []byte(`bad\xZZ`)},
	}
	for _, c := range cases {
		if got := UnescapeSSID(c.in); !bytes.Equal(got, c.want) {
			t.Errorf("UnescapeSSID(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestParseScanKeepsEveryValidSSID(t *testing.T) {
	out := "bssid / frequency / signal level / flags / ssid\n" +
		"aa:aa\t2412\t-40\t[WPA2-PSK-CCMP][ESS]\tMy Home WiFi\r\n" +
		"bb:bb\t2437\t-50\t[WPA2-PSK-CCMP][ESS]\tCaf\\xc3\\xa9\n" +
		"cc:cc\t5180\t-60\t[WPA2-PSK-CCMP][ESS]\tBob's \\\"Net\\\"\n" +
		"dd:dd\t2462\t-70\t[ESS]\t\\x00\\x00\\x00\n" +
		"ee:ee\t2412\t-45\t[WPA2-PSK-CCMP][ESS]\tMy Home WiFi\n" +
		"ff:ff\t2412\t-80\t[ESS]\t\n"
	nets := parseScan(out)
	got := map[string]Network{}
	for _, n := range nets {
		got[n.SSID] = n
	}
	if len(nets) != 3 {
		t.Fatalf("want 3 networks (hidden and empty dropped, duplicate merged), got %d: %+v", len(nets), nets)
	}
	home, ok := got["My Home WiFi"]
	if !ok {
		t.Fatalf("an SSID with spaces was not kept whole: %+v", nets)
	}
	if home.Signal != -40 || home.SSIDHex != "4d7920486f6d652057694669" {
		t.Errorf("strongest AP and exact bytes expected, got %+v", home)
	}
	if n, ok := got["Café"]; !ok || n.SSIDHex != "436166c3a9" {
		t.Errorf("UTF-8 SSID not decoded: %+v", nets)
	}
	if _, ok := got[`Bob's "Net"`]; !ok {
		t.Errorf("SSID with quotes not decoded: %+v", nets)
	}
	if nets[0].SSID != "My Home WiFi" {
		t.Errorf("want strongest first, got %+v", nets)
	}
}

func TestSSIDTextShowsInvalidUTF8(t *testing.T) {
	if got := SSIDText([]byte{'a', 0xff, 'b'}); got != "a�b" {
		t.Errorf("SSIDText = %q", got)
	}
}

func TestValidateAllowsWhatWPA2Allows(t *testing.T) {
	ok := []struct {
		ssid []byte
		psk  string
	}{
		{[]byte("open"), ""},
		{[]byte(`Bob's "Home" \ Net`), `pa"ss\word'!`},
		{[]byte("Café 🏠"), "12345678"},
		{bytes.Repeat([]byte("x"), 32), strings.Repeat("p", 63)},
		{[]byte{0xff, 0x01}, strings.Repeat("a", 64)},
		{[]byte("hex"), strings.Repeat("AB", 32)},
	}
	for _, c := range ok {
		if err := validate(c.ssid, c.psk); err != nil {
			t.Errorf("validate(%q, %q) refused: %v", c.ssid, c.psk, err)
		}
	}
	bad := []struct {
		ssid []byte
		psk  string
	}{
		{nil, ""},
		{[]byte{0, 0}, ""},
		{bytes.Repeat([]byte("x"), 33), ""},
		{[]byte("net"), "short"},
		{[]byte("net"), strings.Repeat("p", 64) + "z"},
		{[]byte("net"), "has\nnewline"},
		{[]byte("net"), "café-pass"},
	}
	for _, c := range bad {
		if err := validate(c.ssid, c.psk); err == nil {
			t.Errorf("validate(%q, %q) accepted", c.ssid, c.psk)
		}
	}
}

func TestSSIDLineQuotesWhatItCanAndHexesTheRest(t *testing.T) {
	cases := []struct {
		ssid []byte
		want string
	}{
		{[]byte(`Bob's "Home"`), "\tssid=\"Bob's \"Home\"\""},
		{[]byte(`back\slash`), "\tssid=\"back\\slash\""},
		{[]byte("Café 🏠"), "\tssid=\"Café 🏠\""},
		{[]byte(" padded "), "\tssid=\" padded \""},
		{[]byte("tab\there"), "\tssid=7461620968657265"},
		{[]byte{0xff, 0xfe}, "\tssid=fffe"},
		{[]byte("new\nline"), "\tssid=6e65770a6c696e65"},
	}
	for _, c := range cases {
		if got := ssidLine(c.ssid); got != c.want {
			t.Errorf("ssidLine(%q) = %q, want %q", c.ssid, got, c.want)
		}
	}
}

func TestComposeConfWritesTheSSIDAndPassphraseVerbatim(t *testing.T) {
	onBase(t, "emos", "", "ctrl_interface="+emosDir+"\n")
	conf := composeConf([]byte(`Bob's "Home"`), `pa"ss\word`)
	if !strings.Contains(conf, "\tssid=\"Bob's \"Home\"\"\n") {
		t.Errorf("SSID not written verbatim:\n%s", conf)
	}
	// wpa_supplicant ends a quoted passphrase at its LAST quote, so the inner
	// quote and backslash are literal.
	if !strings.Contains(conf, "\tpsk=\"pa\"ss\\word\"\n") {
		t.Errorf("passphrase not written verbatim:\n%s", conf)
	}
	conf = composeConf([]byte("net"), strings.Repeat("AB", 32))
	if !strings.Contains(conf, "\tpsk="+strings.Repeat("ab", 32)+"\n") {
		t.Errorf("a 64-hex PSK must be written bare:\n%s", conf)
	}
}
