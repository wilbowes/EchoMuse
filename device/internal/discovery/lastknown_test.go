package discovery

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func useTempLastKnown(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "last_controller.json")
	orig := lastKnownPath
	lastKnownPath = path
	t.Cleanup(func() { lastKnownPath = orig })
	return path
}

// #106: the endpoint that registered is what the next start reads, so a
// restart begins with the address that worked instead of mDNS alone.
func TestASuccessfulRegistrationIsWhatTheNextStartReads(t *testing.T) {
	useTempLastKnown(t)
	want := endpoint("10.20.40.110", 8767, 8770)

	if err := SaveLastKnown(want); err != nil {
		t.Fatalf("SaveLastKnown: %v", err)
	}
	got := LoadLastKnown()
	if got == nil {
		t.Fatal("LoadLastKnown returned nil after a save")
	}
	if *got != *want {
		t.Fatalf("LoadLastKnown = %#v, want %#v", got, want)
	}
}

func TestAnAbsentLastKnownFileIsNotAnError(t *testing.T) {
	useTempLastKnown(t)
	if got := LoadLastKnown(); got != nil {
		t.Fatalf("LoadLastKnown = %#v, want nil before the first registration", got)
	}
}

// A torn or hand-edited file must never keep the device from rediscovering a
// controller: it reads as "no last-known address", with a log line.
func TestAMalformedLastKnownFileIsIgnored(t *testing.T) {
	path := useTempLastKnown(t)
	for _, raw := range []string{"", "{not json", `{"host":"","port":8767}`} {
		if err := os.WriteFile(path, []byte(raw), 0o644); err != nil {
			t.Fatal(err)
		}
		if got := LoadLastKnown(); got != nil {
			t.Fatalf("LoadLastKnown(%q) = %#v, want nil", raw, got)
		}
	}
}

// A reconnect saves the same endpoint again on every controller restart. The
// flash must not take a write for each one.
func TestSavingTheSameEndpointDoesNotRewriteTheFile(t *testing.T) {
	path := useTempLastKnown(t)
	ep := endpoint("10.20.40.110", 8767, 8770)
	if err := SaveLastKnown(ep); err != nil {
		t.Fatalf("SaveLastKnown: %v", err)
	}

	past := time.Unix(1_600_000_000, 0)
	if err := os.Chtimes(path, past, past); err != nil {
		t.Fatal(err)
	}
	if err := SaveLastKnown(ep); err != nil {
		t.Fatalf("second SaveLastKnown: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !info.ModTime().Equal(past) {
		t.Fatalf("the file was rewritten for an unchanged endpoint")
	}
}
