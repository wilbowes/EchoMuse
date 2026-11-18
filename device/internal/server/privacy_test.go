package server

import "testing"

// Before the latch has once agreed, its unmuted state under our mute (a
// restore it has not taken, or a failed entry) re-enters it, never unmutes.
func TestDecideLatchNeverOpensMicBeforeAgreement(t *testing.T) {
	cases := []struct {
		ours, latch, trusted bool
		want                 privacyAction
	}{
		{true, false, false, privacyEnterDriver},
		{true, false, true, privacyUnmuteOurs},
		{false, true, false, privacyMuteOurs},
		{false, true, true, privacyMuteOurs},
		{true, true, false, privacyInSync},
		{false, false, true, privacyInSync},
	}
	for _, c := range cases {
		if got := decideLatch(c.ours, c.latch, c.trusted); got != c.want {
			t.Errorf("decideLatch(%v, %v, %v) = %v, want %v", c.ours, c.latch, c.trusted, got, c.want)
		}
	}
}

func TestDecidePrivacyAlwaysLandsOnMuted(t *testing.T) {
	cases := []struct {
		ours, driver bool
		want         privacyAction
	}{
		{false, false, privacyInSync},
		{true, true, privacyInSync},
		// A reboot while muted: we restore muted, the driver boots unmuted.
		{true, false, privacyEnterDriver},
		// Opposite states the other way round: a lit mute button over a live
		// mic. Software cannot unmute the driver, so we mute.
		{false, true, privacyMuteOurs},
	}
	for _, c := range cases {
		if got := decidePrivacy(c.ours, c.driver); got != c.want {
			t.Errorf("decidePrivacy(ours=%v, driver=%v) = %v, want %v", c.ours, c.driver, got, c.want)
		}
	}
}
