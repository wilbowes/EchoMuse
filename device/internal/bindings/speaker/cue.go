//go:build server

package speaker

import (
	"encoding/binary"
	"sync"
)

// One-shot audible cues, mixed into the ALSA write path.
//
// A cue is not a stream: it has no prime gate, no EOS, no underrun accounting
// and no flush semantics, because it is a couple of hundred milliseconds the
// device generated itself and can always deliver. Putting it through
// audioStream would mean the wake cue could be delayed by the prime gate or
// discarded by a barge-in flush, both of which are wrong for a confirmation
// that the device is listening.
//
// It is mixed BEFORE the output chain, so it is EQ'd and limited with
// everything else. That is a deliberate trade: a user who has cut the midrange
// makes the cue quieter along with speech, which is consistent, and in
// exchange the cue can never push the sum into clipping when it lands on top
// of loud music.

type cueState struct {
	mu  sync.Mutex
	buf []float64 // remaining samples, S16 units
	pos int

	// scratch holds the summed period. The mixer may hand back the SHARED
	// silencePeriod, which must never be written to.
	scratch []byte
}

// PlayCue arms a one-shot cue, replacing any still playing.
//
// Replacing rather than queueing is deliberate: a cue is a statement about
// what is happening RIGHT NOW, so two overlapping ones are never wanted and
// the newer is always the true one.
func (p *PcmSpeaker) PlayCue(samples []float64) {
	p.cue.mu.Lock()
	defer p.cue.mu.Unlock()
	p.cue.buf = samples
	p.cue.pos = 0
}

// cueActive reports whether a cue is mid-playback.
func (p *PcmSpeaker) cueActive() bool {
	p.cue.mu.Lock()
	defer p.cue.mu.Unlock()
	return p.cue.pos < len(p.cue.buf)
}

// mixCue sums any in-flight cue into one period, returning the period to play.
//
// Returns `out` untouched when nothing is playing, so the idle path costs one
// mutex read and no copy.
func (p *PcmSpeaker) mixCue(out []byte) []byte {
	p.cue.mu.Lock()
	defer p.cue.mu.Unlock()

	remaining := len(p.cue.buf) - p.cue.pos
	if remaining <= 0 {
		return out
	}

	frames := len(out) / 4
	if cap(p.cue.scratch) < len(out) {
		p.cue.scratch = make([]byte, len(out))
	}
	buf := p.cue.scratch[:len(out)]
	copy(buf, out)

	n := frames
	if remaining < n {
		n = remaining
	}
	for i := 0; i < n; i++ {
		v := p.cue.buf[p.cue.pos+i]
		// Both channels carry the same sample: the wire is mono and
		// PumpPeriod duplicates L=R, so a cue that landed on one channel
		// would be the only asymmetric thing on the path.
		for ch := 0; ch < 2; ch++ {
			off := i*4 + ch*2
			sum := float64(int16(binary.LittleEndian.Uint16(buf[off:]))) + v
			binary.LittleEndian.PutUint16(buf[off:], uint16(clampToInt16(sum)))
		}
	}
	p.cue.pos += n

	if p.cue.pos >= len(p.cue.buf) {
		p.cue.buf, p.cue.pos = nil, 0
	}
	return buf
}
