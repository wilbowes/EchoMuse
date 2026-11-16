package server

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// allFrames returns every frame the ring has been sent so far.
func (r *recordingRing) allFrames() [][]led.Led {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([][]led.Led(nil), r.frames...)
}

func isViolet(l led.Led) bool { return l.B > l.R && l.R > l.G && l.B > 0 }
func isRed(l led.Led) bool    { return l.R == 180 && l.G == 0 && l.B == 0 }
func isCyan(l led.Led) bool   { return l.R == 0 && l.G > 0 && l.G == l.B }

// anyFrame reports whether any LED of any frame from index `from` matches.
func anyFrame(r *recordingRing, from int, match func(led.Led) bool) bool {
	for _, f := range r.allFrames()[from:] {
		for _, l := range f {
			if match(l) {
				return true
			}
		}
	}
	return false
}

func TestIdentifyLastsThirtySeconds(t *testing.T) {
	if identifyDuration != 30*time.Second {
		t.Fatalf("identifyDuration = %v, want 30s", identifyDuration)
	}
}

func TestIdentifyPaintsTheWholeRingViolet(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.StartIdentify(200 * time.Millisecond)

	frames := ring.allFrames()
	if len(frames) == 0 {
		t.Fatal("StartIdentify painted nothing")
	}
	if len(frames[0]) != 12 {
		t.Fatalf("first frame has %d LEDs, want all 12", len(frames[0]))
	}
	for _, l := range frames[0] {
		if !isViolet(l) {
			t.Fatalf("LED %d is not violet: %+v", l.ID, l)
		}
	}
}

// A pulse, not a steady colour: the brightness must move, and repeat each period.
func TestIdentifyFrameIsAPulse(t *testing.T) {
	dim, bright := identifyFrame(0)[0], identifyFrame(0.5)[0]
	if bright.B <= dim.B+50 {
		t.Fatalf("no visible pulse: dim %+v bright %+v", dim, bright)
	}
	if again := identifyFrame(1.0)[0]; again != dim {
		t.Fatalf("pulse does not repeat each period: %+v vs %+v", again, dim)
	}
	for _, p := range []float64{0, 0.25, 0.5, 0.75} {
		if f := identifyFrame(p); len(f) != 12 || !isViolet(f[0]) {
			t.Fatalf("phase %v: bad frame %+v", p, f)
		}
	}
}

func TestIdentifyHoldsAgainstControllerPaintsButRecordsThem(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.StartIdentify(300 * time.Millisecond)
	from := len(ring.allFrames())

	s.SetLEDs(frame(1, 2, 3), nil) // a turn or reply frame
	time.Sleep(120 * time.Millisecond)

	if anyFrame(ring, from, func(l led.Led) bool { return l.R == 1 && l.G == 2 && l.B == 3 }) {
		t.Fatal("a controller frame painted over identify")
	}
	s.baseLEDsMu.Lock()
	got := s.baseLEDs[0]
	s.baseLEDsMu.Unlock()
	if got.R != 1 || got.G != 2 || got.B != 3 {
		t.Fatalf("suppressed frame was not recorded into baseLEDs: %+v", got)
	}
}

func TestIdentifyOverridesTheMuteRing(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.mute.muted = true
	s.StartIdentify(300 * time.Millisecond)
	from := len(ring.allFrames())

	s.mute.showMuteLEDs()
	s.RestoreMuteRing()
	time.Sleep(120 * time.Millisecond)

	if anyFrame(ring, from, isRed) {
		t.Fatal("the mute ring painted over identify")
	}
	if !s.mute.IsMuted() {
		t.Fatal("identify changed the mute state")
	}
}

func TestIdentifyOverridesTheVolumeArcButTheLevelStillApplies(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.StartIdentify(300 * time.Millisecond)
	from := len(ring.allFrames())

	s.volume.showLEDs(volumeMax)
	time.Sleep(120 * time.Millisecond)

	if anyFrame(ring, from, isCyan) {
		t.Fatal("the volume arc painted over identify")
	}
	if !s.volume.DisplayActive() {
		t.Fatal("the arc's display window was not recorded")
	}
}

func TestIdentifyOverridesFlashAndDirection(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.StartIdentify(300 * time.Millisecond)
	from := len(ring.allFrames())

	s.Flash(200, 200, 200, 20*time.Millisecond)
	s.SetLEDs(frame(0, 200, 0), nil) // listening ring, so direction would paint
	s.SetDirectionLEDs(90)
	time.Sleep(120 * time.Millisecond)

	if anyFrame(ring, from, func(l led.Led) bool { return l.R == 200 && l.G == 200 }) {
		t.Fatal("a flash painted over identify")
	}
	if anyFrame(ring, from, func(l led.Led) bool { return l.G == 200 && l.R == 0 }) {
		t.Fatal("the listening ring or direction arc painted over identify")
	}
}

// What the ring shows when identify ends depends on who owns it underneath.
func TestIdentifyHandsTheRingBack(t *testing.T) {
	cases := []struct {
		name     string
		muted    bool
		linkDown bool
		volume   bool
		want     func(led.Led) bool
	}{
		{"idle goes back to the controller's frame", false, false, false,
			func(l led.Led) bool { return l.R == 60 && l.G == 10 }},
		{"muted goes back to the red ring", true, false, false, isRed},
		{"muted and link down goes back to the link's frame", true, true, false,
			func(l led.Led) bool { return l.R == 60 && l.G == 10 }},
		{"a volume arc still showing is repainted", false, false, true, isCyan},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ring := &recordingRing{}
			s := testServer(ring)
			s.mute.muted = c.muted
			s.SetLinkDown(c.linkDown)
			s.StartIdentify(60 * time.Millisecond)
			s.SetLEDs(frame(60, 10, 0), nil)
			if c.volume {
				s.volume.showLEDs(volumeMax)
			}
			time.Sleep(200 * time.Millisecond)

			if s.identifyActive() {
				t.Fatal("identify did not end")
			}
			if got := ring.last(); !c.want(got) {
				t.Fatalf("ring after identify: %+v", got)
			}
		})
	}
}

// Unmuting mid-identify clears the ring on a normal device. That clear must be
// held back and the ring must not come back red afterwards.
func TestUnmutingDuringIdentifyDoesNotLeaveTheRingRed(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.mute.muted = true
	s.StartIdentify(60 * time.Millisecond)

	s.mute.muted = false
	s.mute.clearLEDs()
	time.Sleep(200 * time.Millisecond)

	if got := ring.last(); got.R != 0 || got.G != 0 || got.B != 0 {
		t.Fatalf("ring is not clear after identify: %+v", got)
	}
}

// A second request extends the flash. Restarting would reset the pulse phase,
// which reads as a flicker (the repeating-gif bug the link pulse once had).
func TestIdentifyAgainExtendsWithoutRestarting(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.StartIdentify(100 * time.Millisecond)
	gen := s.identify.gen
	time.Sleep(60 * time.Millisecond)
	s.StartIdentify(100 * time.Millisecond)
	if s.identify.gen != gen {
		t.Fatal("a repeat request started a second flash")
	}

	time.Sleep(80 * time.Millisecond) // 140ms in: past the first deadline
	if !s.identifyActive() {
		t.Fatal("the repeat request did not extend the flash")
	}
	time.Sleep(150 * time.Millisecond)
	if s.identifyActive() {
		t.Fatal("identify never ended")
	}
}

func TestIdentifyStopsPaintingWhenItEnds(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.StartIdentify(40 * time.Millisecond)
	time.Sleep(150 * time.Millisecond)
	n := len(ring.allFrames())
	time.Sleep(150 * time.Millisecond)
	if got := len(ring.allFrames()); got != n {
		t.Fatalf("%d frames painted after identify ended", got-n)
	}

	// A fresh run after the first ended is its own flash, and also ends.
	s.StartIdentify(40 * time.Millisecond)
	if !s.identifyActive() {
		t.Fatal("second identify did not start")
	}
	time.Sleep(150 * time.Millisecond)
	if s.identifyActive() {
		t.Fatal("second identify did not end")
	}
}

// Nothing may be painted before the LED hardware exists, and a gate around a
// nil controller must read as nil to the callers that check for it.
func TestGatedLEDIsNilBeforeTheHardwareIsReady(t *testing.T) {
	s := &Server{}
	if lc := s.gatedLED(); lc != nil {
		t.Fatalf("gatedLED() = %v, want nil", lc)
	}
	s.StartIdentify(10 * time.Millisecond) // must not panic
}

func TestTheGateOnlyDropsWritesWhileIdentifyIsActive(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	if err := s.gatedLED().SetLEDs(frame(5, 5, 5)...); err != nil {
		t.Fatal(err)
	}
	if got := ring.last(); got.R != 5 {
		t.Fatalf("gate dropped a write with no identify running: %+v", got)
	}
	n, err := s.gatedLED().GetNumLEDs()
	if err != nil || n != 12 {
		t.Fatalf("GetNumLEDs = %d, %v", n, err)
	}
}
