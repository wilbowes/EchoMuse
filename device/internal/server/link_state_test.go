package server

import (
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// A muted device that lost its controller sat there showing the red mute
// ring — which does not merely say less than the orange link pulse, it says
// something false: red means "muted and working". Reported by Wil,
// 2026-09-02.
//
// The reapply half was always present: OnConnected calls RestoreMuteRing
// with the comment "orange pulse overwrote the red ring — restore it". The
// pulse never overwrote it, because the paint suppression could not tell a
// controller frame from the device's own link pulse.

func TestTheLinkPulsePaintsThroughTheMuteRing(t *testing.T) {
	cases := []struct {
		name                          string
		volumeActive, muted, linkDown bool
		want                          bool
	}{
		{"idle and connected", false, false, false, false},
		{"muted and connected — mute is sovereign", false, true, false, true},
		{"muted and disconnected — the pulse wins", false, true, true, false},
		{"unmuted and disconnected", false, false, true, false},
		// The arc outranks both. It cannot arise while link-down, since the
		// volume buttons are inert there, but the ordering must not depend
		// on that being true elsewhere.
		{"volume arc holds the ring", true, false, false, true},
		{"volume arc outranks link-down too", true, false, true, true},
	}
	for _, c := range cases {
		if got := suppressPaint(c.volumeActive, c.muted, c.linkDown); got != c.want {
			t.Errorf("%s: suppressPaint(%v,%v,%v) = %v, want %v",
				c.name, c.volumeActive, c.muted, c.linkDown, got, c.want)
		}
	}
}

func TestLinkDownDefaultsToConnected(t *testing.T) {
	// A fresh Server must not start link-down: that would hand the ring away
	// from mute before any connection callback has run.
	s := &Server{}
	if s.LinkDown() {
		t.Fatal("a fresh Server must not start in link-down")
	}
	s.SetLinkDown(true)
	if !s.LinkDown() {
		t.Fatal("SetLinkDown(true) not recorded")
	}
	s.SetLinkDown(false)
	if s.LinkDown() {
		t.Fatal("SetLinkDown(false) not recorded — the ring would never go " +
			"back to the controller after a reconnect")
	}
}

// RestoreMuteRing paints a FULL RED RING, and red on this device means one
// thing: muted. It was called unconditionally on every reconnect, so an
// unmuted device pending approval — which reconnects repeatedly — showed a red
// flash through its pending pulse, over and over, saying something false about
// a device that was working. Seen on 3611NF's first emOS boot, 2026-09-06.
//
// Every other painter of this ring already checks: the startup path in
// server.go gates on IsMuted(), and volume.go's display-expiry gates on
// isMuted(). This was the one that did not.
func TestRestoreMuteRingOnlyPaintsWhenMuted(t *testing.T) {
	reached := 0
	// showMuteLEDs calls ledCtrl() first, so counting that call counts
	// whether the paint was REACHED. Returning nil makes it a no-op after
	// that, which is what we want — the assertion is about the guard.
	s := &Server{mute: &muteController{
		ledCtrl: func() led.Controller { reached++; return nil },
	}}

	if s.mute.IsMuted() {
		t.Fatal("a fresh muteController must not report muted")
	}
	s.RestoreMuteRing()
	if reached != 0 {
		t.Errorf("unmuted: reached the paint %d times, want 0", reached)
	}

	s.mute.mu.Lock()
	s.mute.muted = true
	s.mute.mu.Unlock()
	s.RestoreMuteRing()
	if reached != 1 {
		t.Errorf("muted: reached the paint %d times, want 1", reached)
	}
}
