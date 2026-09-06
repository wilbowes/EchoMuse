package platform

import (
	"os"
	"path/filepath"
	"testing"
)

// mkroot builds a fake filesystem root. Files are given as path → contents;
// an empty content string makes a directory instead, which is what
// /dev/__properties__ is on a real device.
func mkroot(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for p, content := range files {
		full := filepath.Join(root, p)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if content == "" {
			if err := os.MkdirAll(full, 0o755); err != nil {
				t.Fatal(err)
			}
			continue
		}
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

// The os-release emOS actually writes, from emos/build.sh. Kept verbatim so
// this test fails if that format changes shape.
const emosRelease = `NAME="emOS"
ID=emos
PRETTY_NAME="emOS 0.1.3"
VERSION="0.1.3"
VERSION_ID="0.1.3"
BUILD_ID="20260904T071342Z"
`

func TestDetect(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		want  string
	}{
		{
			name:  "emOS: os-release stamped at build time",
			files: map[string]string{"etc/os-release": emosRelease},
			want:  EmOS,
		},
		{
			name:  "FireOS: Android's property service",
			files: map[string]string{"dev/__properties__": ""},
			want:  FireOS,
		},
		{
			// Neither marker. Must NOT guess: a wrong answer here sends a
			// device payloads it cannot use.
			name:  "neither marker present",
			files: map[string]string{},
			want:  Unknown,
		},
		{
			// Some other distribution's os-release must not read as emOS.
			name:  "os-release from something else",
			files: map[string]string{"etc/os-release": "NAME=\"Debian\"\nID=debian\n"},
			want:  Unknown,
		},
		{
			// ID=emos wins over the property service. They are mutually
			// exclusive on real hardware, so this only fires if something has
			// gone strange — and claiming emOS is the safe half, since the
			// consequence is skipping Android payloads rather than pushing
			// Android payloads at a device with no package manager.
			name: "both markers: emOS wins",
			files: map[string]string{
				"etc/os-release":     emosRelease,
				"dev/__properties__": "",
			},
			want: EmOS,
		},
		{
			// The ID must be the whole value, not a prefix match — an
			// "emosaic" distribution is not ours.
			name:  "ID that merely starts with emos",
			files: map[string]string{"etc/os-release": "ID=emosaic\n"},
			want:  Unknown,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Detect(mkroot(t, tc.files)); got != tc.want {
				t.Errorf("Detect() = %q, want %q", got, tc.want)
			}
		})
	}
}

// Base caches, and the value must be stable for the life of the process: a
// field that flapped would read as a device changing platform underneath the
// controller.
func TestBaseIsStable(t *testing.T) {
	first := Base()
	for i := 0; i < 3; i++ {
		if got := Base(); got != first {
			t.Fatalf("Base() returned %q then %q", first, got)
		}
	}
	switch first {
	case EmOS, FireOS, Unknown:
	default:
		t.Errorf("Base() = %q, not one of the three defined answers", first)
	}
}
