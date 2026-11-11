package client

import (
	"os"
	"path/filepath"
	"testing"
)

func TestThermalReadable(t *testing.T) {
	dir := t.TempDir()
	saved := thermalZones
	t.Cleanup(func() { thermalZones = saved })
	thermalZones = filepath.Join(dir, "thermal_zone*", "temp")

	if thermalReadable() {
		t.Fatal("no zones: the capability must not be announced")
	}
	zone := filepath.Join(dir, "thermal_zone0")
	if err := os.Mkdir(zone, 0o755); err != nil {
		t.Fatal(err)
	}
	if thermalReadable() {
		t.Fatal("a zone with no temp file cannot produce a reading")
	}
	if err := os.WriteFile(filepath.Join(zone, "temp"), []byte("42000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !thermalReadable() {
		t.Fatal("a readable zone must announce the capability")
	}
}
