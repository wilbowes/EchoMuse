package server

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDeviceStateRoundtrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "etc", "state.json")

	// Missing file → not ok.
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for missing file")
	}

	// Save creates parent dirs and persists the muted flag.
	saveDeviceState(path, deviceState{Muted: true})
	st, ok := loadDeviceState(path)
	if !ok || !st.Muted {
		t.Fatalf("roundtrip failed: ok=%v st=%+v", ok, st)
	}

	saveDeviceState(path, deviceState{Muted: false})
	st, ok = loadDeviceState(path)
	if !ok || st.Muted {
		t.Fatalf("overwrite failed: ok=%v st=%+v", ok, st)
	}

	// Corrupt file → not ok, no panic.
	if err := os.WriteFile(path, []byte("{truncated"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for corrupt file")
	}
}
