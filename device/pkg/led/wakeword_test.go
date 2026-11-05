package led

import "testing"

func TestWakeWordOffFrameAlternatesDimRed(t *testing.T) {
	frame := WakeWordOffFrame()
	if len(frame) != ledCount {
		t.Fatalf("got %d LEDs, want %d", len(frame), ledCount)
	}
	for i, l := range frame {
		if l.ID != i {
			t.Fatalf("LED %d has ID %d", i, l.ID)
		}
		if i%2 == 0 {
			if l.R == 0 || l.G != 0 || l.B != 0 {
				t.Fatalf("LED %d = %+v, want dim red", i, l)
			}
			continue
		}
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d = %+v, want dark", i, l)
		}
	}
	if frame[0].R >= 180 {
		t.Fatalf("idle red %d is not dim next to the mute ring's 180", frame[0].R)
	}
}
