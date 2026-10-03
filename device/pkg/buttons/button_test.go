package buttons

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// These are the two contracts the other half of the project depends on, and
// both are wire contracts rather than internal detail. `ClickType.String` is
// what the dashboard's logs and the support bundle render, and the JSON tags
// are what `em_button.decide` and `em_tap_burst` parse on the controller. A
// change to either is a change to a protocol, and nothing else in the tree
// would notice.

// The click-type numbers are the evdev codes, and they are pinned because they
// are not ours to choose: the codes come off the hardware.
func TestClickTypeCodesAreTheEvdevOnes(t *testing.T) {
	cases := []struct {
		got  ClickType
		want uint16
	}{
		{DotClick, 138},
		{VolumeUpClick, 115},
		{VolumeDownClick, 114},
		{MuteClick, 113},
	}
	for _, c := range cases {
		if uint16(c.got) != c.want {
			t.Errorf("click type %d, want %d — these are evdev codes and are "+
				"not ours to renumber", uint16(c.got), c.want)
		}
	}
}

// Volume up must never read as volume down. They are adjacent codes (115/114),
// so a transposition is the easy mistake, and it moves a volume rather than
// breaking — which is the quiet kind.
func TestClickTypeStrings(t *testing.T) {
	cases := []struct {
		in   ClickType
		want string
	}{
		{DotClick, "dot"},
		{VolumeUpClick, "volume_up"},
		{VolumeDownClick, "volume_down"},
		{MuteClick, "mute"},
		{ClickType(0), "unknown"},
		{ClickType(9999), "unknown"},
	}
	for _, c := range cases {
		if got := c.in.String(); got != c.want {
			t.Errorf("ClickType(%d).String() = %q, want %q", c.in, got, c.want)
		}
	}
}

// Every click type the firmware can send must have a name. A new one without
// one renders as "unknown" in the log, which is the same string an unrecognised
// code produces — so a hardware change and a typo look identical.
func TestEveryClickTypeHasAName(t *testing.T) {
	for _, c := range []ClickType{DotClick, VolumeUpClick, VolumeDownClick, MuteClick} {
		if c.String() == "unknown" {
			t.Errorf("click type %d has no name", c)
		}
	}
}

// ─── The wire contract ───────────────────────────────────────────────────────

// `heldMs` carries omitempty so a PRESS does not send it: the controller reads
// an absent heldMs as a tap, and a press reporting heldMs:0 is the same thing
// on the wire — so this is about not sending a field that was never measured.
func TestHeldMsIsOmittedOnAPressAndPresentOnARelease(t *testing.T) {
	press, err := json.Marshal(ButtonClickEvent{
		Button: Button{Type: DotButton}, ClickType: DotClick, Down: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(press), "heldMs") {
		t.Errorf("press marshals heldMs: %s — it was not measured yet", press)
	}

	release, err := json.Marshal(ButtonClickEvent{
		Button: Button{Type: DotButton}, ClickType: DotClick, Down: false, HeldMs: 812,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(release), `"heldMs":812`) {
		t.Errorf("release dropped heldMs: %s", release)
	}
}

// `muted` is always sent, with no omitempty. It is the mic state at the instant
// of the press, and the controller judges the gesture against it — so a mute
// that was not reported leaves the controller deciding from a stale view. An
// absent `muted` and `muted: false` are different facts, which is the
// absence-is-not-zero rule seen from the other side.
func TestMutedIsAlwaysPresent(t *testing.T) {
	out, err := json.Marshal(ButtonClickEvent{
		Button: Button{Type: VolumeButton}, ClickType: VolumeUpClick, Down: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"muted":false`) {
		t.Errorf("muted is omitted when false: %s — the controller cannot tell "+
			"\"not muted\" from \"nobody said\"", out)
	}
}

// ─── Subscriptions ───────────────────────────────────────────────────────────

// Cancel must call the function it was given, exactly as many times as it is
// called — and never panic on a second cancel, since teardown paths can run more
// than once.
func TestSubscriptionCancelCallsItsFunc(t *testing.T) {
	n := 0
	sub := NewEventSubscription(func() { n++ })
	if n != 0 {
		t.Fatal("control: constructing a subscription must not cancel it")
	}
	sub.Cancel()
	if n != 1 {
		t.Fatalf("Cancel called the func %d times, want 1", n)
	}
	sub.Cancel()
	if n != 2 {
		t.Fatalf("a second Cancel called the func %d times total, want 2", n)
	}
}

// The func it wraps is a context.CancelFunc in production, so cancelling must
// actually cancel — this is the shape used when the evdev reader is torn down.
func TestSubscriptionCancelCancelsARealContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	sub := NewEventSubscription(cancel)

	select {
	case <-ctx.Done():
		t.Fatal("control: the context is already cancelled")
	default:
	}

	sub.Cancel()
	select {
	case <-ctx.Done():
	default:
		t.Fatal("Cancel did not cancel the context")
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

// The field names are camelCase and the controller parses them BY NAME. A
// snake_case rename unmarshals to zero values rather than erroring, so the
// click would arrive as clickType 0 — which String() renders as "unknown", and
// an unknown click type is the same log line as an unrecognised hardware code.
func TestFieldNamesAreCamelCase(t *testing.T) {
	out, err := json.Marshal(ButtonClickEvent{
		Button:    Button{Type: DotButton},
		ClickType: VolumeDownClick,
		Down:      true,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"button"`, `"clickType"`, `"down"`, `"muted"`} {
		if !strings.Contains(string(out), want) {
			t.Errorf("%s missing from %s — the controller reads these by name "+
				"and a rename unmarshals to a zero value rather than failing", want, out)
		}
	}
	// The button's own type field is nested and also camelCase-free (single
	// word), but the internal name must never leak — it is unexported for a
	// reason and the controller has no use for it.
	if strings.Contains(string(out), "internalName") {
		t.Errorf("the unexported internal name leaked onto the wire: %s", out)
	}
}
