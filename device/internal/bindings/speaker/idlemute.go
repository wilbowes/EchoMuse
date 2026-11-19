package speaker

import "time"

// idleMuteAfter is how long the speaker must have played pure silence before
// the amplifier's hardware mute goes back on. The radar keeps its stream open
// between sounds, so the codec driver's own mute (set only at stream open and
// close) never returns, and the powered, unmuted amp's noise floor is a hiss
// heard at a metre in a quiet room (#1153). Long enough that the mute does
// not chatter in the gaps of a reply or between tracks.
const idleMuteAfter = 5 * time.Second

// idleMute decides, one period at a time, when the board's AmpMute goes back
// on and when it comes off. It releases on the first period carrying audio,
// before that period is written: the ALSA ring still holds ~170ms of silence
// ahead of it, which covers the release. Pure; the write loop applies it.
type idleMute struct {
	after  int // silent periods before muting
	silent int
	muted  bool
}

// idlePeriod is one write-loop period: 2048 frames at 48kHz (periodSize in
// the cgo half of this package).
const idlePeriod = 2048 * time.Second / 48000

func newIdleMute(after time.Duration) *idleMute {
	return &idleMute{after: int((after + idlePeriod - 1) / idlePeriod)}
}

// step takes whether this period is pure silence and returns whether to
// assert the mute now, or release it now. At most one is true.
func (m *idleMute) step(silent bool) (mute, release bool) {
	if !silent {
		m.silent = 0
		if m.muted {
			m.muted = false
			return false, true
		}
		return false, false
	}
	if m.muted {
		return false, false
	}
	m.silent++
	if m.silent >= m.after {
		m.muted = true
		return true, false
	}
	return false, false
}
