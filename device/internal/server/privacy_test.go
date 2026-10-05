package server

import "testing"

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
