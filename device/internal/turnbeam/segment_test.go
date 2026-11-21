package turnbeam

import (
	"math"
	"testing"
)

const fs = 0.04 // the device's Silero step

// feed runs a Segmenter over a script of (from, to, prob, level) spans, in
// seconds around the crossing, starting at -OutLeadS; frames outside every
// span are silence at the floor.
type span struct{ from, to, prob, db float64 }

func run(floor, until float64, spans ...span) *Segmenter {
	g := NewSegmenter(fs, floor, -OutLeadS)
	for t := -OutLeadS; t < until; t += fs {
		mid := t + fs/2
		p, db := 0.0, floor
		for _, s := range spans {
			if mid >= s.from && mid < s.to {
				p, db = s.prob, s.db
			}
		}
		g.Feed(p, db)
	}
	return g
}

func near(a, b float64) bool { return math.Abs(a-b) <= fs+1e-9 }

func TestTheSegmentCoversTheCommandWithPreAndPostRoll(t *testing.T) {
	g := run(-50, 6, span{0.8, 1.9, 0.95, -30})
	a, b, ok := g.Bounds()
	if !ok || !g.Done() {
		t.Fatalf("no finished segment: ok=%v done=%v", ok, g.Done())
	}
	if !near(a, 0.8-segPreS) || !near(b, 1.9+segPostS) {
		t.Fatalf("segment %.2f..%.2f, want %.2f..%.2f", a, b, 0.8-segPreS, 1.9+segPostS)
	}
}

// A pause shorter than HangS inside the command stays in, at full level.
func TestAShortPauseStaysInTheSegment(t *testing.T) {
	g := run(-50, 6, span{0.8, 1.3, 0.9, -30}, span{1.3, 1.9, 0.05, -50}, span{1.9, 2.4, 0.9, -30})
	a, b, _ := g.Bounds()
	if !near(b, 2.4+segPostS) {
		t.Fatalf("segment %.2f..%.2f: the 0.6s pause ended it", a, b)
	}
	for tt := 1.35; tt < 1.85; tt += 0.05 {
		if g.Gain(tt) != 1 {
			t.Fatalf("gain %.2f at %.2fs inside the pause", g.Gain(tt), tt)
		}
	}
}

// Once ended, the segment never re-opens: speech after it (the TV, a second
// sentence) is not sent.
func TestTheSegmentNeverReopens(t *testing.T) {
	g := run(-50, 8, span{0.8, 1.5, 0.9, -30}, span{3.0, 4.0, 0.9, -30})
	_, b, _ := g.Bounds()
	if !near(b, 1.5+segPostS) {
		t.Fatalf("segment ends %.2f, want %.2f", b, 1.5+segPostS)
	}
	if g.Gain(3.5) != 0 {
		t.Fatal("speech after the segment ended was let through")
	}
}

func TestNothingAfterTheWakeSendsNothing(t *testing.T) {
	g := run(-50, 5)
	if !g.Done() {
		t.Fatal("not done after StartS with no speech")
	}
	if _, _, ok := g.Bounds(); ok {
		t.Fatal("a segment with no speech")
	}
	for tt := -OutLeadS; tt < 5; tt += 0.1 {
		if g.Gain(tt) != 0 {
			t.Fatalf("gain %.2f at %.2fs with no segment", g.Gain(tt), tt)
		}
	}
}

// Speech-like frames no louder than the pre-wake floor (the TV through the
// beam) do not open a segment; the wake word's own tail does not either.
func TestQuietSpeechAndTheWakeTailDoNotOpen(t *testing.T) {
	if g := run(-50, 5, span{0.5, 2.5, 0.9, -47}); g.Done() == false || func() bool { _, _, ok := g.Bounds(); return ok }() {
		t.Fatal("speech 3 dB over the floor opened a segment")
	}
	g := run(-50, 5, span{-0.3, 0.3, 0.9, -30})
	if _, _, ok := g.Bounds(); ok {
		t.Fatal("the wake word's tail opened a segment")
	}
}

func TestTheGainRampsAtBothEdges(t *testing.T) {
	g := run(-50, 6, span{0.8, 1.9, 0.95, -30})
	a, b, _ := g.Bounds()
	if g.Gain(a-0.001) != 0 || g.Gain(b+0.001) != 0 {
		t.Fatal("audio outside the segment")
	}
	if v := g.Gain(a + segRampS/2); math.Abs(v-0.5) > 0.01 {
		t.Fatalf("gain %.2f half way up the ramp", v)
	}
	if g.Gain((a+b)/2) != 1 {
		t.Fatal("not full level inside the segment")
	}
}
