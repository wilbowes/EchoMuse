package bluetooth

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/platform"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

func TestSafeServiceName(t *testing.T) {
	for n, want := range map[string]bool{
		"btmanagerd": true, "btclient_ble": true, "a.b-c_d": true,
		"": false, "a b": false, "x;rm": false, "$(id)": false,
	} {
		if got := safeServiceName(n); got != want {
			t.Errorf("safeServiceName(%q) = %v, want %v", n, got, want)
		}
	}
}

// The daemons are Android init services: stopped on FireOS, where the board
// states them, and on no other base. A board stating none stops none.
func TestDaemonsStopOnFireOSOnly(t *testing.T) {
	donut := board.Donut.Hardware.HCIDaemons
	if len(donut) == 0 {
		t.Fatal("the Dot 3 states no HCI daemons")
	}
	for _, c := range []struct {
		base   string
		stated []string
		want   []string
	}{
		{platform.FireOS, donut, donut},
		{platform.EmOS, donut, nil},
		{platform.Unknown, donut, nil},
		{platform.FireOS, board.Biscuit.Hardware.HCIDaemons, nil},
		{platform.FireOS, []string{"btmanagerd", "x;rm", ""}, []string{"btmanagerd"}},
	} {
		if got := hciDaemonsToStop(c.base, c.stated); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s, %q: stop %q, want %q", c.base, c.stated, got, c.want)
		}
	}
}

// Bluedroid is disabled only where the Android framework can run it: FireOS
// with app_process. emOS carries Amazon's /system, app_process included, and
// starts no zygote; a FireOS patched so zygote never starts has no
// app_process at all.
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
