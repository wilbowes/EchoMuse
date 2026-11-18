package client

import "testing"

// Listening locally the beam lock lasts only as long as the listen session:
// held through the reply, barge-ins were heard through whichever edge mic the
// turn had locked, and a far-side one missed every one (2026-11-06).
func TestBeamLockFollowsTheListenSession(t *testing.T) {
	cases := []struct {
		name                 string
		req                  int32
		local, open, wasOpen bool
		lock, unlock         bool
	}{
		{"lock during a session", beamReqLock, true, true, true, true, false},
		{"lock as the session opens", beamReqLock, true, true, false, true, false},
		{"session closes: unlock before the reply", beamReqNone, true, false, true, false, true},
		{"late lock after the session closed", beamReqLock, true, false, false, false, false},
		{"lock racing the close loses", beamReqLock, true, false, true, false, true},
		{"nothing pending, no session", beamReqNone, true, false, false, false, false},
		{"nothing pending, session continues", beamReqNone, true, true, true, false, false},
		{"explicit unlock always applies", beamReqUnlock, true, true, true, false, true},
		{"streaming: lock as before", beamReqLock, false, false, false, true, false},
		{"streaming: a gate change means nothing", beamReqNone, false, false, true, false, false},
		{"turn stream (lock_mic): unaffected", beamReqUnlock, false, false, false, false, true},
	}
	for _, c := range cases {
		lock, unlock := beamStep(c.req, c.local, c.open, c.wasOpen)
		if lock != c.lock || unlock != c.unlock {
			t.Errorf("%s: got lock=%v unlock=%v, want lock=%v unlock=%v", c.name, lock, unlock, c.lock, c.unlock)
		}
	}
}
