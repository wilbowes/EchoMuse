package speaker

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

// The values here are measurements off a stock FireOS Dot, not preferences.
// Pinning them means a later edit has to disagree with the hardware on
// purpose rather than by accident.

func TestJackRoutingMutesInternalDriverWhenSomethingIsPluggedIn(t *testing.T) {
	got := jackRouting(true)
	if len(got) != 2 {
		t.Fatalf("want 2 writes, got %d: %+v", len(got), got)
	}
	if got[0].Ctl != ctlSpeakerAmp || got[0].Args[0] != "Off" {
		t.Errorf("internal amp must be Off with a plug in, got ctl %s = %v", got[0].Ctl, got[0].Args)
	}
}

func TestJackRoutingEnablesInternalDriverWhenNothingIsPluggedIn(t *testing.T) {
	got := jackRouting(false)
	if got[0].Ctl != ctlSpeakerAmp || got[0].Args[0] != "On" {
		t.Errorf("internal amp must be On with the jack empty, got ctl %s = %v", got[0].Ctl, got[0].Args)
	}
}

// The regression this whole change exists for: accdet leaves the jack's
// output stage at the floor of its range on insert, and nothing of ours used
// to raise it. A gain that is not set, or set to 0, is silence.
func TestJackRoutingRaisesTheJackOutputStageOnInsert(t *testing.T) {
	in := jackRouting(true)
	out := jackRouting(false)

	find := func(ws []mixerWrite) *mixerWrite {
		for i := range ws {
			if ws[i].Ctl == ctlHPDriverGain {
				return &ws[i]
			}
		}
		return nil
	}

	gi, go_ := find(in), find(out)
	if gi == nil || go_ == nil {
		t.Fatal("HP driver gain must be set for BOTH positions — leaving it unset on either side is how the jack went silent")
	}
	if gi.Args[0] != hpGainJack {
		t.Errorf("plug in: want gain %s (stock's value), got %s", hpGainJack, gi.Args[0])
	}
	if go_.Args[0] != hpGainInternal {
		t.Errorf("plug out: want gain %s restored, got %s", hpGainInternal, go_.Args[0])
	}
	if gi.Args[0] == "0" {
		t.Error("gain 0 is the FLOOR of the 0..35 range, which is the bug, not a setting")
	}
}

// Both channels take the same value: the control is a pair (L, R) and
// tinymix writes it as two arguments. One argument sets only the left.
func TestJackRoutingWritesBothChannelsOfTheGainPair(t *testing.T) {
	for _, inserted := range []bool{true, false} {
		for _, w := range jackRouting(inserted) {
			if w.Ctl != ctlHPDriverGain {
				continue
			}
			if len(w.Args) != 2 || w.Args[0] != w.Args[1] {
				t.Errorf("inserted=%v: gain needs two equal args, got %v", inserted, w.Args)
			}
		}
	}
}

// Watch dispatches the state it booted into, so every write runs twice in a
// row on an ordinary device. Nothing here may depend on the previous state.
func TestJackRoutingIsIdempotent(t *testing.T) {
	for _, inserted := range []bool{true, false} {
		a, b := jackRouting(inserted), jackRouting(inserted)
		if len(a) != len(b) {
			t.Fatalf("inserted=%v: not deterministic", inserted)
		}
		for i := range a {
			if a[i].Ctl != b[i].Ctl || len(a[i].Args) != len(b[i].Args) {
				t.Errorf("inserted=%v: write %d differs between calls", inserted, i)
			}
		}
	}
}

func TestJackRoutingDriftRewritesOnlyWhatMoved(t *testing.T) {
	// Gain clobbered back to the floor, amp still correct — the exact state
	// measured after a mediaserver restart with a plug in.
	drift := jackRoutingDrift(true, map[string]string{
		ctlSpeakerAmp:   "Off",
		ctlHPDriverGain: "0",
	})
	if len(drift) != 1 || drift[0].Ctl != ctlHPDriverGain {
		t.Fatalf("want only the gain rewritten, got %+v", drift)
	}
}

func TestJackRoutingDriftIsSilentWhenNothingMoved(t *testing.T) {
	if d := jackRoutingDrift(true, map[string]string{
		ctlSpeakerAmp:   "Off",
		ctlHPDriverGain: hpGainJack,
	}); len(d) != 0 {
		t.Errorf("want no writes on a correct codec, got %+v", d)
	}
}

// A control we could not READ must not be treated as drifted. Rewriting on a
// failed read would spawn tinymix every interval forever on any device whose
// output we cannot parse — the same "failure to look is not evidence of
// absence" rule the controller's asset reconcile follows.
func TestJackRoutingDriftSkipsUnreadableControls(t *testing.T) {
	if d := jackRoutingDrift(true, map[string]string{}); len(d) != 0 {
		t.Errorf("want no writes when nothing could be read, got %+v", d)
	}
	d := jackRoutingDrift(true, map[string]string{ctlHPDriverGain: "0"})
	if len(d) != 1 || d[0].Ctl != ctlHPDriverGain {
		t.Errorf("want only the readable, drifted control, got %+v", d)
	}
}

// The bass guard is for the internal driver. With a plug in it must be off
// whatever the controller configured, and everything else must pass through
// untouched — in particular the limiter, which guards the EQ against clipping
// (#231) on either output.
func TestChainForJackBypassesOnlyTheGuardWithAPlugIn(t *testing.T) {
	in := outchain.DefaultParams()
	in.Bands[0] = 3
	in.Loudness = true
	in.GuardDb = -20

	got := chainForJack(in, true)
	if got.GuardEnabled {
		t.Errorf("guard must be bypassed with a plug in")
	}
	want := in
	want.GuardEnabled = false
	if got != want {
		t.Errorf("only GuardEnabled may change:\n got %+v\nwant %+v", got, want)
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
