package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Point the record at a temp directory for the duration of one test.
// Restored via t.Cleanup rather than at the end of the test body: a failing
// assertion returns early, and a leaked override would make every later test
// write into the previous one's directory.
func withTempPath(t *testing.T) string {
	t.Helper()
	saved := consoleTimeoutPath
	dir := t.TempDir()
	consoleTimeoutPath = filepath.Join(dir, "console.timeout")
	t.Cleanup(func() { consoleTimeoutPath = saved })
	return consoleTimeoutPath
}

func TestWriteConsoleTimeoutStoresMinutes(t *testing.T) {
	path := withTempPath(t)

	changed, err := WriteConsoleTimeout(15)
	if err != nil {
		t.Fatalf("write: %v", err)
	}
	if !changed {
		t.Fatal("writing a new timeout must report a change")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	// MINUTES on disk. init multiplies to seconds for TMOUT at the one point
	// of use, so a value of 900 here would be a factor-of-sixty bug that only
	// shows up as a console that never times out.
	if got := strings.TrimSpace(string(b)); got != "15" {
		t.Fatalf("stored %q, want %q", got, "15")
	}
}

func TestWriteConsoleTimeoutIsIdempotent(t *testing.T) {
	withTempPath(t)

	if _, err := WriteConsoleTimeout(30); err != nil {
		t.Fatalf("first write: %v", err)
	}
	// The config push repeats every setting on every reconnect, and this
	// device runs for years on eMMC that cannot be replaced. An unconditional
	// write would spend a flash write per reconnect to store bytes already
	// there.
	changed, err := WriteConsoleTimeout(30)
	if err != nil {
		t.Fatalf("second write: %v", err)
	}
	if changed {
		t.Fatal("rewriting the same value must not report a change")
	}
}

func TestWriteConsoleTimeoutZeroRemovesTheRecord(t *testing.T) {
	path := withTempPath(t)

	if _, err := WriteConsoleTimeout(45); err != nil {
		t.Fatalf("write: %v", err)
	}
	changed, err := WriteConsoleTimeout(0)
	if err != nil {
		t.Fatalf("clear: %v", err)
	}
	if !changed {
		t.Fatal("clearing an existing timeout must report a change")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("record still present after clearing: %v", err)
	}

	// Zero when there was nothing is not a change, and must not error: the
	// push repeats on every reconnect and the default is 0, so this is the
	// ordinary path for every device that never sets a timeout.
	changed, err = WriteConsoleTimeout(0)
	if err != nil {
		t.Fatalf("clear again: %v", err)
	}
	if changed {
		t.Fatal("clearing an absent timeout must not report a change")
	}
}

func TestWriteConsoleTimeoutRefusesOutOfRange(t *testing.T) {
	path := withTempPath(t)

	// Refused rather than clamped. 600 is somebody who meant seconds, and
	// silently giving them ten minutes is a console that logs them out all day
	// from a setting that looked accepted.
	for _, bad := range []int{-1, ConsoleTimeoutMaxMin + 1, 600, 100000} {
		if _, err := WriteConsoleTimeout(bad); err == nil {
			t.Fatalf("%d minutes was accepted; it is outside 0-%d",
				bad, ConsoleTimeoutMaxMin)
		}
	}
	// And nothing was written on the way to refusing.
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("a refused value still touched the record")
	}
}

func TestWriteConsoleTimeoutAcceptsTheBoundaries(t *testing.T) {
	withTempPath(t)

	for _, ok := range []int{1, ConsoleTimeoutMaxMin} {
		if _, err := WriteConsoleTimeout(ok); err != nil {
			t.Fatalf("%d minutes should be accepted: %v", ok, err)
		}
	}
}

func TestWriteConsoleTimeoutLeavesNoTempFile(t *testing.T) {
	path := withTempPath(t)

	if _, err := WriteConsoleTimeout(5); err != nil {
		t.Fatalf("write: %v", err)
	}
	// The record is written to a temp file and renamed so init can never read
	// a half-written value — a truncated number parses as a SHORTER timeout,
	// which presents as the device dropping the link.
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("the temp file survived a successful write")
	}
}
