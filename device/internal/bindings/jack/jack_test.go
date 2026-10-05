package jack

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// State reads a fixed path, so the tests point it at a temp file by swapping
// the package variable the read goes through.
func withStateFile(t *testing.T, content string) {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "state")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	old := statePath
	statePath = p
	t.Cleanup(func() { statePath = old })
}

// startWatch runs Watch and returns a channel closed when it has returned. A
// cleanup cancels it and waits, and runs before withStateFile's (cleanups are
// LIFO), so Watch can never read statePath while it is being restored.
func startWatch(t *testing.T, onChange func(bool)) (done <-chan struct{}) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan struct{})
	go func() {
		defer close(ch)
		Watch(ctx, onChange)
	}()
	t.Cleanup(func() {
		cancel()
		<-ch
	})
	return ch
}

func TestStateValues(t *testing.T) {
	// 0 none, 1 headset (with mic), 2 headphone — the three accdet reports.
	// Both 1 and 2 are "something is plugged in" as far as the speaker amp is
	// concerned; only 0 means the Dot should play to the room again.
	cases := []struct {
		content  string
		want     int
		inserted bool
	}{
		{"0\n", 0, false},
		{"1\n", 1, true},
		{"2\n", 2, true},
		{"1", 1, true}, // no trailing newline
	}
	for _, c := range cases {
		withStateFile(t, c.content)
		got, ok := State()
		if !ok {
			t.Fatalf("State() not ok for %q", c.content)
		}
		if got != c.want {
			t.Fatalf("State() = %d for %q, want %d", got, c.content, c.want)
		}
		if Inserted() != c.inserted {
			t.Fatalf("Inserted() = %v for %q, want %v", Inserted(), c.content, c.inserted)
		}
	}
}

// A device with no accdet must not read as "plug removed" — that would have us
// asserting the speaker amp on hardware we know nothing about.
func TestMissingSwitchIsNotAnUnplug(t *testing.T) {
	old := statePath
	statePath = filepath.Join(t.TempDir(), "does-not-exist")
	t.Cleanup(func() { statePath = old })

	if _, ok := State(); ok {
		t.Fatal("a missing state file must report not-ok")
	}
	if Present() {
		t.Fatal("Present() must be false with no state file")
	}
	if Inserted() {
		t.Fatal("a missing state file must not read as inserted")
	}
}

func TestUnparseableStateIsNotOk(t *testing.T) {
	withStateFile(t, "banana\n")
	if _, ok := State(); ok {
		t.Fatal("unparseable content must report not-ok, not a state")
	}
}

// Watch must report the state it STARTS in, not only later changes.
//
// A device booted with a cable in gets no accdet transition and no watcher
// callback, and PcmSpeaker.Init has already asserted the internal amp — so
// without this dispatch it plays to the room with a speaker plugged in, and
// the jack sits at minimum gain. Measured on hardware 2026-09-03; this is the
// regression test for it.
func TestWatchReportsTheStateItStartsIn(t *testing.T) {
	for _, tc := range []struct {
		name    string
		content string
		want    bool
	}{
		{"booted with a plug in", "1", true},
		{"booted with the jack empty", "0", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			withStateFile(t, tc.content)

			got := make(chan bool, 1)
			startWatch(t, func(inserted bool) {
				select {
				case got <- inserted:
				default:
				}
			})

			select {
			case v := <-got:
				if v != tc.want {
					t.Errorf("initial dispatch: got inserted=%v, want %v", v, tc.want)
				}
			case <-time.After(2 * time.Second):
				t.Error("Watch never reported its initial state")
			}
		})
	}
}

// A device with no detect switch must stay silent rather than assert a
// position it cannot know. Reporting "not inserted" here would drive the
// routing on hardware we know nothing about.
func TestWatchDispatchesNothingWithoutADetectSwitch(t *testing.T) {
	withStateFile(t, "0")
	statePath = filepath.Join(t.TempDir(), "absent")

	called := make(chan bool, 1)
	done := startWatch(t, func(inserted bool) { called <- inserted })

	// With no switch Watch returns at once, so wait for that rather than for
	// a timeout that can only prove nothing happened yet.
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Watch kept running on a device with no detect switch")
	}
	select {
	case <-called:
		t.Error("dispatched a jack position on a device with no detect switch")
	default:
	}
}
