package bluetooth

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/platform"
)

// Bluedroid is disabled only where the Android framework can run it: FireOS
// with app_process. emOS carries Amazon's /system, app_process included, and
// starts no zygote; a FireOS patched so zygote never starts has no
// app_process at all. From @Starker3's #1044.
func TestBluedroidIsOnlyDisabledWhereItCanRun(t *testing.T) {
	withFramework := t.TempDir()
	if err := os.MkdirAll(filepath.Join(withFramework, "system/bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(withFramework, "system/bin/app_process32"), nil, 0o755); err != nil {
		t.Fatal(err)
	}
	without := t.TempDir()
	for _, c := range []struct {
		base string
		root string
		want bool
	}{
		{platform.FireOS, withFramework, true},
		{platform.FireOS, without, false},
		{platform.EmOS, withFramework, false},
		{platform.Unknown, withFramework, false},
	} {
		if got := androidFramework(c.base, c.root); got != c.want {
			t.Errorf("%s, framework present %v: %v, want %v", c.base, c.root == withFramework, got, c.want)
		}
	}
}
