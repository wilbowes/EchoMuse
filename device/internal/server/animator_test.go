package server

import "testing"

func TestSpinFrame(t *testing.T) {
	spec := AnimSpec{
		Pattern: "spin",
		Colors:  [][3]uint8{{0, 200, 0}, {0, 60, 0}},
	}
	frame := animFrame(spec, 3)
	if frame[3].G != 200 {
		t.Fatalf("head not at pos 3: %+v", frame[3])
	}
	if frame[2].G != 60 {
		t.Fatalf("trail not at pos 2: %+v", frame[2])
	}
	for i, l := range frame {
		if i == 3 || i == 2 {
			continue
		}
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark: %+v", i, l)
		}
		if l.ID != i {
			t.Fatalf("LED %d has wrong ID %d", i, l.ID)
		}
	}
	// Wraparound: head at 0 puts trail at 11.
	frame = animFrame(spec, 0)
	if frame[0].G != 200 || frame[11].G != 60 {
		t.Fatalf("wraparound wrong: head=%+v trail=%+v", frame[0], frame[11])
	}
}

func TestRotateFrame(t *testing.T) {
	palette := make([][3]uint8, 12)
	for i := range palette {
		palette[i] = [3]uint8{uint8(i), 0, 0}
	}
	spec := AnimSpec{Pattern: "rotate", Colors: palette}
	// pos=0 is the palette 1:1; pos=1 shifts every colour one LED clockwise.
	frame := animFrame(spec, 0)
	for i := range frame {
		if frame[i].R != uint8(i) {
			t.Fatalf("pos 0: LED %d = %d, want %d", i, frame[i].R, i)
		}
	}
	frame = animFrame(spec, 1)
	if frame[1].R != 0 || frame[0].R != 11 {
		t.Fatalf("pos 1 rotation wrong: led0=%d led1=%d", frame[0].R, frame[1].R)
	}
}

func TestRotateFrameEmptyPalette(t *testing.T) {
	frame := animFrame(AnimSpec{Pattern: "rotate"}, 5)
	for i, l := range frame {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark on empty palette: %+v", i, l)
		}
	}
}

func TestPaletteFrame(t *testing.T) {
	// Single colour fills the ring.
	frame := paletteFrame([][3]uint8{{10, 20, 30}})
	for i, l := range frame {
		if l.R != 10 || l.G != 20 || l.B != 30 {
			t.Fatalf("LED %d wrong: %+v", i, l)
		}
	}
	// Short multi-colour list leaves the rest dark.
	frame = paletteFrame([][3]uint8{{1, 0, 0}, {2, 0, 0}})
	if frame[0].R != 1 || frame[1].R != 2 || frame[2].R != 0 {
		t.Fatalf("partial palette wrong: %+v", frame[:3])
	}
}
