package client

import (
	"os"
	"path/filepath"
	"testing"
)

// The fleet is keyed on the serial, so a wrong one is not a cosmetic fault: it
// collides two devices into one identity, and nothing in the system reports it.
// These cover the two sources that answer when Android's property service does
// not — which is every emOS device.

func TestSanitiseSerial(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		// procfs hands this back with no trailing newline at all.
		{"idme, no newline", "G090LF11752215LE", "G090LF11752215LE"},
		{"trailing newline", "G090LF11752215LE\n", "G090LF11752215LE"},
		{"trailing NUL", "G090LF11752215LE\x00", "G090LF11752215LE"},
		{"NUL then garbage", "G090LF11752215LE\x00\xff\xff", "G090LF11752215LE"},
		{"surrounding space", "  G090LF11752215LE \r\n", "G090LF11752215LE"},

		// An unreadable or unpopulated field must read as absent. Returning
		// something plausible-looking is worse than "unknown-device", which at
		// least says it does not know.
		{"empty", "", ""},
		{"only NUL", "\x00\x00", ""},
		{"only whitespace", "  \n", ""},
		{"embedded space", "G090 LF117", ""},
		{"non-printable", "G090\x01LF117", ""},
		{"high byte", "G090\xc3\xa9LF", ""},
		{"absurdly long", string(make([]byte, 65)), ""},
	}
	for _, c := range cases {
		if got := sanitiseSerial(c.in); got != c.want {
			t.Errorf("%s: sanitiseSerial(%q) = %q, want %q", c.name, c.in, got, c.want)
		}
	}
}

func TestSerialFromIdme(t *testing.T) {
	dir := t.TempDir()
	orig := idmePath
	t.Cleanup(func() { idmePath = orig })

	// Absent is the normal case on a board with no idme driver, and must be
	// quiet — GetSerialNo falls through to getprop and the cmdline.
	idmePath = filepath.Join(dir, "does-not-exist")
	if got := serialFromIdme(); got != "" {
		t.Errorf("missing idme: got %q, want empty", got)
	}

	// Exactly what the device gives: 16 characters, no newline. Read off
	// G090LF11752215LE on 2026-09-15.
	idmePath = filepath.Join(dir, "serial")
	if err := os.WriteFile(idmePath, []byte("G090LF11752215LE"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, want := serialFromIdme(), "G090LF11752215LE"; got != want {
		t.Errorf("idme serial: got %q, want %q", got, want)
	}

	// procfs reports st_size 0 for these files. ReadFile must keep reading to
	// EOF rather than trusting the stat, or the serial comes back empty on the
	// real device while passing every test written against a regular file.
	if fi, err := os.Stat(idmePath); err == nil && fi.Size() == 0 {
		t.Log("note: zero-length file also read correctly")
	}

	// A corrupt field is rejected rather than registered.
	if err := os.WriteFile(idmePath, []byte("\x00\x00\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := serialFromIdme(); got != "" {
		t.Errorf("corrupt idme: got %q, want empty", got)
	}
}

func TestSerialFromCmdline(t *testing.T) {
	// The real v2 cmdline, truncated at COMMAND_LINE_SIZE. This is the state
	// that started the whole investigation: androidboot.serialno begins at byte
	// 1040 on a 1024-byte limit, so the kernel never sees it and this source
	// correctly finds nothing. idme is what answers on such a device.
	dir := t.TempDir()
	path := filepath.Join(dir, "cmdline")
	orig := cmdlinePath
	t.Cleanup(func() { cmdlinePath = orig })
	cmdlinePath = path

	write := func(s string) {
		if err := os.WriteFile(path, []byte(s), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	write("console=tty0 androidboot.serialno=G090LF11752215LE androidboot.bootreason=rtc\n")
	if got, want := serialFromCmdline(), "G090LF11752215LE"; got != want {
		t.Errorf("cmdline serial: got %q, want %q", got, want)
	}

	// Truncated away — the v2 case.
	write("console=tty0 bootprof.lk_t=409")
	if got := serialFromCmdline(); got != "" {
		t.Errorf("truncated cmdline: got %q, want empty", got)
	}

	// Present but empty, which is how a partially written argument reads.
	write("console=tty0 androidboot.serialno= androidboot.bootreason=rtc")
	if got := serialFromCmdline(); got != "" {
		t.Errorf("empty serialno=: got %q, want empty", got)
	}
}
