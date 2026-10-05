package discovery

import "testing"

// parseTLSPort decides whether a device dials `wss` or does not dial at all.
//
// It is ten lines of string and integer parsing with no test, in a package
// measuring 24%, and the answer it produces is not a hint — it is a branch the
// whole link depends on. `dialPlan` (internal/client/pairing.go) takes
// `tlsPort > 0` as licence to dial wss, and control.go:550 refuses to fall back
// to plain when a CA is installed and no tls_port was advertised:
//
//     CA installed but %s advertises no tls_port — not dialling plain
//
// So a false 0 is not "use the default port": it is a device that cannot reach
// its controller by either path, and the error names a pairing gesture that is
// not what is wrong. A false positive is the mirror — a wss dial to a port
// nothing is listening on.
//
// The parser is written against TXT records, which are `key=value` strings
// zeroconf hands over whole, so the shape it must survive is a key that merely
// CONTAINS or RESEMBLES `tls_port`.

func TestParseTLSPortReadsTheValue(t *testing.T) {
	if got := parseTLSPort([]string{"tls_port=8770"}); got != 8770 {
		t.Errorf("tls_port=8770 parsed as %d", got)
	}
}

// Absent is the ordinary answer from a pre-TLS controller, and it must come
// back as exactly 0 rather than something that looks like a port.
func TestParseTLSPortAbsentIsZero(t *testing.T) {
	for _, txt := range [][]string{
		nil,
		{},
		{"path=/api", "version=2.25.0"},
		{"tls_port"},      // no '='
		{"tls_port="},     // empty value
		{"tlsport=8770"},  // different key entirely
		{"TLS_PORT=8770"}, // zeroconf keys are case-sensitive
	} {
		if got := parseTLSPort(txt); got != 0 {
			t.Errorf("parseTLSPort(%q) = %d, want 0", txt, got)
		}
	}
}

// A key that merely CONTAINS the prefix must not match. `tls_port_backup` and
// `xtls_port` are plausible names, and matching either would take a port from a
// field whose meaning nobody here agreed on.
func TestParseTLSPortDoesNotMatchANearMissKey(t *testing.T) {
	for _, txt := range []string{
		"xtls_port=8770",
		"tls_port_backup=8770",
		"tls_ports=8770",
		"tls_port_extra=8770",
	} {
		if got := parseTLSPort([]string{txt}); got != 0 {
			t.Errorf("parseTLSPort(%q) = %d, want 0 — the key must match exactly", txt, got)
		}
	}
}

// 0 and 65536 are the two ends a range check gets wrong. 0 is excluded because
// it is the sentinel for "no TLS"; 65536 is out of range for a uint16 and would
// be an invalid dial.
func TestParseTLSPortRejectsTheEndsOfTheInvalidRange(t *testing.T) {
	for _, v := range []string{"0", "-1", "65536", "70000", "4294967296"} {
		if got := parseTLSPort([]string{"tls_port=" + v}); got != 0 {
			t.Errorf("tls_port=%s parsed as %d, want 0", v, got)
		}
	}
}

func TestParseTLSPortAcceptsTheEndsOfTheValidRange(t *testing.T) {
	for _, c := range []struct {
		in   string
		want int
	}{{"1", 1}, {"8770", 8770}, {"65535", 65535}} {
		if got := parseTLSPort([]string{"tls_port=" + c.in}); got != c.want {
			t.Errorf("tls_port=%s parsed as %d, want %d", c.in, got, c.want)
		}
	}
}

// Non-numeric values are the shape a controller bug actually takes — an empty
// string, a hostname, a float from a JSON encoder, whitespace from a
// hand-edited record.
func TestParseTLSPortRejectsNonNumbers(t *testing.T) {
	for _, v := range []string{"abc", "8770.0", "0x1F", " 8770", "8770 ", "8 770", "8,770"} {
		if got := parseTLSPort([]string{"tls_port=" + v}); got != 0 {
			t.Errorf("tls_port=%q parsed as %d, want 0", v, got)
		}
	}
}

// A malformed entry must not stop the search. One bad value followed by a good
// one still has to find it, or a controller advertising a stray first entry
// turns off TLS for every device on the network.
func TestParseTLSPortSkipsMalformedAndKeepsLooking(t *testing.T) {
	txt := []string{"tls_port=", "tls_port=abc", "path=/api", "tls_port=8770"}
	if got := parseTLSPort(txt); got != 8770 {
		t.Errorf("parseTLSPort(%q) = %d, want 8770 — one bad entry must not "+
			"end the search", txt, got)
	}
}

// Two valid entries: the first wins. Present because a duplicated TXT key is
// malformed zeroconf rather than something to define behaviour for, and
// whichever way it goes it must be the SAME way every time — a device picking
// between them differently from a controller's own reading would dial a
// different port than the one being advertised.
func TestParseTLSPortTakesTheFirstValidEntry(t *testing.T) {
	first := parseTLSPort([]string{"tls_port=8770", "tls_port=9999"})
	second := parseTLSPort([]string{"tls_port=8770", "tls_port=9999"})
	if first != second {
		t.Fatalf("unstable: %d then %d", first, second)
	}
	if first != 8770 {
		t.Errorf("got %d, want 8770 (the first valid entry)", first)
	}
}

// Order must not matter for the normal case: the key sits among the other TXT
// properties and where it falls is up to zeroconf.
func TestParseTLSPortIgnoresPosition(t *testing.T) {
	want := 8770
	for _, txt := range [][]string{
		{"tls_port=8770"},
		{"path=/api", "tls_port=8770"},
		{"tls_port=8770", "path=/api"},
		{"a=1", "tls_port=8770", "z=9"},
	} {
		if got := parseTLSPort(txt); got != want {
			t.Errorf("parseTLSPort(%q) = %d, want %d", txt, got, want)
		}
	}
}
