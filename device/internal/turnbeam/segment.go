package turnbeam

import "math"

// Segmenter finds the ONE stretch of a turn that holds the command, from a
// speech detector run on the stage's cleaned beam, frame by frame. Nothing
// before its start or after its end is sent, and nothing inside it is ever
// cut: a pause shorter than HangS stays in, and once the segment has ended
// it never re-opens.
//
// A frame counts as speech when the detector's probability is at least
// SpeechProb and the frame is 6 dB over the beam's own pre-wake floor
// (Stage.FloorDB), the energy endpointing of Rabiner & Sambur 1975. The
// detector alone let TV words just after a command open the segment
// further; the energy condition keeps those out (41C8 offline, 2026-11-21).
//
// It replaces an in-turn gate that switched the beam between full level and
// -26 dB on a spatial presence score every 128ms: a soft word or a pause cut
// the command into pieces and a TV word inside a hold was spliced in between
// them ("What's going on?" for "what's the time", 41C8 turn 1478).
//
// Times are seconds around the wake word's crossing.
type Segmenter struct {
	FrameS float64 // detector frame length
	floor  float64 // dB
	t      float64 // end of the last frame fed
	on     float64
	last   float64
	run    int
	state  segState
}

type segState int

const (
	segWaiting segState = iota
	segOpen
	segEnded
	segNone // no speech within StartS
)

const (
	SpeechProb = 0.5
	segNeed    = 3     // consecutive speech frames to open
	segOverDB  = 6.0   // speech frames are this far over the pre-wake floor
	segTailS   = 0.384 // the wake word's own tail: no onset before this
	StartS     = 3.0   // no onset by then: nothing is sent
	HangS      = 0.75  // this long without speech ends the segment
	segPreS    = 0.2   // kept before the onset
	segPostS   = 0.25  // kept after the last speech frame
	segRampS   = 0.03
	// SegDelayS is how far behind the newest audio a sample must be before
	// its gain is final: the end is known HangS after the last speech, and
	// covers segPostS after it; the onset is known segNeed frames after it,
	// and covers segPreS before it. Plus a detector frame and a little slack.
	SegDelayS = 0.64
)

// NewSegmenter starts a turn whose first frame begins at t0 (seconds around
// the crossing), with the stage's pre-wake floor.
func NewSegmenter(frameS, floorDB, t0 float64) *Segmenter {
	return &Segmenter{FrameS: frameS, floor: floorDB, t: t0}
}

// Feed takes one frame's speech probability and level (dB, S16 scale).
func (g *Segmenter) Feed(prob, levelDB float64) {
	g.t += g.FrameS
	if g.state == segEnded || g.state == segNone {
		return
	}
	speech := prob >= SpeechProb && levelDB > g.floor+segOverDB
	switch g.state {
	case segWaiting:
		if g.t < segTailS {
			return
		}
		if speech {
			g.run++
		} else {
			g.run = 0
		}
		if g.run >= segNeed {
			g.on, g.last, g.state = g.t-float64(segNeed)*g.FrameS, g.t, segOpen
		} else if g.t >= StartS {
			g.state = segNone
		}
	case segOpen:
		if speech {
			g.last = g.t
		} else if g.t-g.last >= HangS {
			g.state = segEnded
		}
	}
}

// Done reports that the turn's outcome is known: the segment has ended, or
// no speech came by StartS.
func (g *Segmenter) Done() bool { return g.state == segEnded || g.state == segNone }

// Bounds returns the segment once it has opened; end is +Inf until it ends.
func (g *Segmenter) Bounds() (start, end float64, ok bool) {
	switch g.state {
	case segOpen:
		return math.Max(-OutLeadS, g.on-segPreS), math.Inf(1), true
	case segEnded:
		return math.Max(-OutLeadS, g.on-segPreS), g.last + segPostS, true
	}
	return 0, 0, false
}

// Gain is the weight of the sample at time t: 0 outside the segment, 1
// inside, with segRampS ramps at both edges. Only final for t at least
// SegDelayS behind the newest frame fed.
func (g *Segmenter) Gain(t float64) float64 {
	a, b, ok := g.Bounds()
	if !ok || t < a || t >= b {
		return 0
	}
	w := 1.0
	if r := (t - a) / segRampS; r < 1 {
		w = r
	}
	if r := (b - t) / segRampS; r < w {
		w = r
	}
	return w
}
