package speaker

import (
	"math"
	"sync/atomic"
)

// responseGain is the one relative gain for the voice plane. The requested
// value is written by the config callback and read by the ALSA goroutine; cur
// belongs to that goroutine and makes a live setting change a one-period ramp
// instead of a click.
//
// A boosted voice can exceed S16 before the later device volume brings it back
// into range. Response periods therefore use a float64 mix buffer and do not
// quantise until after mixing, output processing and device volume. Low stays
// on the historical S16 path and is bit-identical to today's behaviour.
type responseGain struct {
	target atomic.Uint64 // math.Float64bits of the requested linear gain
	cur    float64
}

type responsePeriod struct {
	owner        *responseGain
	volume       *softVolume
	volumeTarget float64
	start        float64
	requested    float64
	frames       int
}

func gainFromDB(db float64) float64 {
	if db <= 0 {
		return 1
	}
	return math.Pow(10, db/20)
}

func (g *responseGain) setDB(db float64) {
	g.target.Store(math.Float64bits(gainFromDB(db)))
}

func (g *responseGain) requested() float64 {
	bits := g.target.Load()
	if bits == 0 {
		return 1 // safe zero-value: today's behaviour
	}
	return math.Float64frombits(bits)
}

func cappedResponseGain(requested, volume float64) float64 {
	if volume <= 0 {
		return requested
	}
	if ceiling := 1 / volume; requested > ceiling {
		return ceiling
	}
	return requested
}

func (g *responseGain) begin(volume *softVolume, frames int, volumeTarget float64) responsePeriod {
	start := g.cur
	if start == 0 {
		start = 1
	}
	return responsePeriod{
		owner:        g,
		volume:       volume,
		volumeTarget: volumeTarget,
		start:        start,
		requested:    g.requested(),
		frames:       frames,
	}
}

func (p responsePeriod) gainAtFrame(frame int) float64 {
	if p.frames <= 0 {
		return cappedResponseGain(p.requested, p.volumeTarget)
	}
	candidate := p.start + (p.requested-p.start)*float64(frame+1)/float64(p.frames)
	volume := p.volume.gainAtFrame(frame, p.frames, p.volumeTarget)
	return cappedResponseGain(candidate, volume)
}

func (p responsePeriod) fillGains(dst []float64) {
	for i := 0; i < min(len(dst), p.frames); i++ {
		dst[i] = p.gainAtFrame(i)
	}
}

func (p responsePeriod) boosted(gains []float64) bool {
	for i := 0; i < min(len(gains), p.frames); i++ {
		if gains[i] > 1+1e-12 {
			return true
		}
	}
	return false
}

func (p responsePeriod) finish(gains []float64) {
	if n := min(len(gains), p.frames); n > 0 {
		p.owner.cur = gains[n-1]
	}
}

// settle takes the gain immediately while there is no voice to click. It is
// called on music-only and silent periods so a later response starts at the
// correct gain rather than fading from a stale one.
func (g *responseGain) settle(volume *softVolume) {
	g.cur = cappedResponseGain(g.requested(), volume.targetGain())
}
