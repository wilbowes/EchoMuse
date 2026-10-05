package listen

import "testing"

func TestResolve(t *testing.T) {
	cases := []struct {
		name                   string
		onDevice, sess, scorer bool
		want                   string
	}{
		{"controller mode streams", false, true, true, StateStream},
		{"old controller keeps streaming", true, false, true, StateStream},
		{"private listening", true, true, true, StateLocal},
		// The rule the whole design turns on: never fall back to streaming.
		{"no scorer is degraded, not streaming", true, true, false, StateDegraded},
	}
	for _, c := range cases {
		got, reason := Resolve(c.onDevice, c.sess, c.scorer, "")
		if got != c.want {
			t.Errorf("%s: %s, want %s", c.name, got, c.want)
		}
		if got == StateDegraded && reason == "" {
			t.Errorf("%s: degraded with no reason", c.name)
		}
	}
}
