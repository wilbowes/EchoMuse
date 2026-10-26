package speaker

import (
	"reflect"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The table's values are pinned in pkg/board. These test what the speaker
// does with a table, on biscuit's, the one measured against a stock Dot.

const (
	amp  = "Ext_Speaker_Amp_Switch"
	gain = "HP Driver Gain Volume"
	mux  = "Audio_DacMux_Setting"
)

var biscuitJack = board.Biscuit.Hardware.Speaker.Jack

// In the board's order: the amp off first with a plug in, on last without,
// so nothing is switched under a playing speaker.
func TestJackRoutingFollowsTheBoardsOrder(t *testing.T) {
	in := jackRouting(biscuitJack, true)
	if len(in) != 3 || in[0].Ctl != amp || in[0].Args[0] != "Off" {
		t.Errorf("insert %+v", in)
	}
	out := jackRouting(biscuitJack, false)
	if last := out[len(out)-1]; last.Ctl != amp || last.Args[0] != "On" {
		t.Errorf("removal %+v", out)
	}
}

// Watch dispatches the state it booted into, so every write runs twice in a
// row on an ordinary device. Nothing here may depend on the previous state.
func TestJackRoutingIsIdempotent(t *testing.T) {
	for _, inserted := range []bool{true, false} {
		if a, b := jackRouting(biscuitJack, inserted), jackRouting(biscuitJack, inserted); !reflect.DeepEqual(a, b) {
			t.Errorf("inserted=%v: %+v then %+v", inserted, a, b)
		}
	}
}

// Every control either side writes is read back, once.
func TestJackControlsAreBothSidesOnce(t *testing.T) {
	if got, want := jackControls(biscuitJack), []string{amp, gain, mux}; !reflect.DeepEqual(got, want) {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestJackRoutingDriftRewritesOnlyWhatMoved(t *testing.T) {
	// Gain clobbered back to the floor, amp still correct — the exact state
	// measured after a mediaserver restart with a plug in.
	drift := jackRoutingDrift(biscuitJack, true, map[string]string{amp: "Off", gain: "0"})
	if len(drift) != 1 || drift[0].Ctl != gain {
		t.Fatalf("want only the gain rewritten, got %+v", drift)
	}
	// FireOS 5's mediaserver puts the mux back too.
	drift = jackRoutingDrift(biscuitJack, true, map[string]string{amp: "Off", gain: "11", mux: "Off"})
	if len(drift) != 1 || drift[0].Ctl != mux || drift[0].Args[0] != "On" {
		t.Errorf("want only the mux rewritten On, got %+v", drift)
	}
}

func TestJackRoutingDriftIsSilentWhenNothingMoved(t *testing.T) {
	if d := jackRoutingDrift(biscuitJack, true, map[string]string{amp: "Off", gain: "11", mux: "On"}); len(d) != 0 {
		t.Errorf("want no writes on a correct codec, got %+v", d)
	}
}

// A control we could not READ must not be treated as drifted: rewriting on a
// failed read would rewrite it every interval forever ("failure to look is
// not evidence of absence", as in the controller's asset reconcile).
func TestJackRoutingDriftSkipsUnreadableControls(t *testing.T) {
	if d := jackRoutingDrift(biscuitJack, true, map[string]string{}); len(d) != 0 {
		t.Errorf("want no writes when nothing could be read, got %+v", d)
	}
	d := jackRoutingDrift(biscuitJack, true, map[string]string{gain: "0"})
	if len(d) != 1 || d[0].Ctl != gain {
		t.Errorf("want only the readable, drifted control, got %+v", d)
	}
}

// The bass guard and the stock-sound correction are for the internal driver.
// With a plug in both must be off whatever the controller configured, and
// everything else must pass through untouched — in particular the limiter,
// which guards the EQ against clipping (#231) on either output.
func TestChainForJackBypassesOnlyTheDriverStagesWithAPlugIn(t *testing.T) {
	in := outchain.DefaultParams()
	in.Bands[0] = 3
	in.Loudness = true
	in.GuardDb = -20

	got := chainForJack(in, true)
	if got.GuardEnabled {
		t.Errorf("guard must be bypassed with a plug in")
	}
	if got.StockSound {
		t.Errorf("the stock-sound correction must be off with a plug in")
	}
	want := in
	want.GuardEnabled = false
	want.StockSound = false
	if got != want {
		t.Errorf("only GuardEnabled and StockSound may change:\n got %+v\nwant %+v", got, want)
	}
	if !got.LimiterEnabled {
		t.Errorf("limiter must stay on with a plug in")
	}
}

// Nothing in the jack: the controller's params, exactly — including a guard
// the user switched off, which must not be switched back on.
func TestChainForJackPassesParamsThroughWithNothingPluggedIn(t *testing.T) {
	for _, guard := range []bool{true, false} {
		in := outchain.DefaultParams()
		in.GuardEnabled = guard
		if got := chainForJack(in, false); got != in {
			t.Errorf("guard=%v: want params unchanged, got %+v", guard, got)
		}
	}
}

// The caller's params are not modified: PcmSpeaker keeps them to re-derive
// the chain when the plug comes out.
func TestChainForJackDoesNotModifyTheStoredParams(t *testing.T) {
	in := outchain.DefaultParams()
	chainForJack(in, true)
	if !in.GuardEnabled {
		t.Errorf("stored params were modified")
	}
}
