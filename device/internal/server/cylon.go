package server

import "github.com/wilbowes/EchoMuse/pkg/led"

// CylonFrame renders one frame of the mirrored hunting sweep (#316): two
// comets at pos and its mirror (12-pos) sweep 0..6 and back, converging at
// the bottom of the ring and reuniting at the top. The direction reversal
// is what distinguishes it from the controller-driven spin pattern; the
// blue-violet keeps it clear of red (mute), orange (link down), cyan
// (volume arc) and white (pending approval).
//
// dir is the sweep direction (+1/-1); the trail folds at the turning
// points, which is how a comet behaves there anyway.
//
// Pure so the frame math is unit-testable — the goroutine that drives it
// lives in cmd/server.go beside the two link-state pulses (layer 3 of the
// ring ladder, docs/led-ring-states.md).
func CylonFrame(pos, dir int) []led.Led {
	const (
		head      = uint8(140)
		trailByte = uint8(50)
		r         = uint8(30)
		g         = uint8(55)
	)
	frame := make([]led.Led, 12)
	dot := func(i int, b uint8) {
		i = ((i % 12) + 12) % 12
		frame[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	trail := pos - dir
	if trail < 0 || trail > 6 {
		trail = pos + dir // fold at the turning point
	}
	for _, p := range []int{pos, (12 - pos) % 12} {
		dot(p, head)
	}
	for _, p := range []int{trail, (12 - trail) % 12} {
		if p == (12-pos)%12 || p == pos {
			continue // converging: head already covers the mirror dot
		}
		dot(p, trailByte)
	}
	return frame
}
