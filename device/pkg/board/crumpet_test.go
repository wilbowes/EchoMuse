package board

import "testing"

func TestMeasuredCrumpetProfile(t *testing.T) {
	const root = "testdata/crumpet-ram"
	b := Detect(root)
	if b != Crumpet {
		t.Fatalf("identity resolved to %s", IDOf(b))
	}
	l := Resolve(root, b)
	if l.Capture == nil || *l.Capture != (PCMAddr{0, 1}) ||
		l.Playback == nil || *l.Playback != (PCMAddr{0, 6}) ||
		l.Loopback == nil || *l.Loopback != (PCMAddr{0, 7}) {
		t.Fatalf("wrong measured PCM bindings: capture=%v playback=%v loopback=%v", l.Capture, l.Playback, l.Loopback)
	}
	if len(l.Keys) != 2 || l.Keys[0] != "/dev/input/event3" || l.Keys[1] != "/dev/input/event1" {
		t.Fatalf("wrong input bindings: %v", l.Keys)
	}
	if l.MuteLEDGPIO != "" || Crumpet.Tuning != nil || l.Mics.NumMics() != 4 {
		t.Fatal("wrong GPIO, tuning or measured microphone geometry")
	}
	angles := map[int]float64{0: 0, 1: 90, 2: 180, 3: 270}
	for _, mic := range l.Mics.Perimeter {
		if angle, ok := angles[mic.Ch]; !ok || angle != mic.Angle {
			t.Fatalf("channel %d has angle %v; tap test requires %v", mic.Ch, mic.Angle, angle)
		}
	}
}
