package speaker

import (
	"testing"
	"time"
)

// #1153: the amp's mute returns after idleMuteAfter of pure silence and comes
// off on the first period of audio, before it is written.
func TestIdleMuteAssertsAfterSilenceAndReleasesOnAudio(t *testing.T) {
	m := newIdleMute(idleMuteAfter)
	period := idlePeriod
	need := int((idleMuteAfter + period - 1) / period)
	for i := 1; i < need; i++ {
		if mute, rel := m.step(true); mute || rel {
			t.Fatalf("acted after %d silent periods (%v), want none before %v", i, time.Duration(i)*period, idleMuteAfter)
		}
	}
	if mute, _ := m.step(true); !mute {
		t.Fatalf("no mute after %v of silence", idleMuteAfter)
	}
	for i := 0; i < 100; i++ {
		if mute, rel := m.step(true); mute || rel {
			t.Fatal("asserted again while already muted")
		}
	}
	if _, rel := m.step(false); !rel {
		t.Fatal("audio did not release the mute")
	}
	if mute, rel := m.step(false); mute || rel {
		t.Fatal("released twice")
	}
}

// Silence inside a reply or between tracks restarts the count: one period of
// audio anywhere resets it, so the mute never chatters mid-programme.
func TestIdleMuteCountRestartsOnAudio(t *testing.T) {
	m := newIdleMute(idleMuteAfter)
	for round := 0; round < 3; round++ {
		for i := 0; i < m.after-1; i++ {
			if mute, _ := m.step(true); mute {
				t.Fatalf("round %d: muted after a gap shorter than %v", round, idleMuteAfter)
			}
		}
		if mute, rel := m.step(false); mute || rel {
			t.Fatalf("round %d: audio while unmuted acted", round)
		}
	}
}
