package bluetooth

import "time"

// MusicDuty decides when the LE scan may run while music is streaming.
//
// Voice yields the scan outright (see Scanner.Yield), but music streams for
// hours and yielding for all of it would leave Bermuda blind. Scanning
// straight through it is worse: measured on VVV 2026-09-27, the scan stalled
// the link for up to 2.1s at a time and the music dropped out 15 times in a
// few minutes, stopping the moment the proxy was turned off. Music Assistant
// only runs ~5s ahead of real time, so the buffer cannot absorb that.
//
// So the scan runs in bursts: On, then Off, and never while the buffer is
// below MinLead. Bermuda ignores an advert older than 10s when choosing an
// area (AREA_MAX_AD_AGE in 0.8.7) and our gate forwards the first sighting
// after 1s of silence, so a burst every On+Off seconds keeps every device
// that advertises within On in its area contest.
type MusicDuty struct {
	On      time.Duration
	Off     time.Duration
	MinLead time.Duration

	scanning bool
	since    time.Time
	active   bool // music was streaming on the previous call
}

// NewMusicDuty returns the defaults: a 2s burst every 7s, held off below
// 2.5s of buffered music.
func NewMusicDuty() *MusicDuty {
	return &MusicDuty{On: 2 * time.Second, Off: 5 * time.Second, MinLead: 2500 * time.Millisecond}
}

// Yield reports whether the scan should stop now. Call it on every tick,
// with music reporting whether a music stream is arriving and lead how much
// of it is buffered. Without music it never asks for a yield.
func (d *MusicDuty) Yield(now time.Time, music bool, lead time.Duration) bool {
	if !music {
		d.active = false
		return false
	}
	if !d.active {
		// A new stream starts with the scan off: the buffer is filling.
		d.active = true
		d.scanning = false
		d.since = now
	}
	switch {
	case lead < d.MinLead:
		if d.scanning {
			d.scanning = false
			d.since = now
		}
	case d.scanning && now.Sub(d.since) >= d.On:
		d.scanning = false
		d.since = now
	case !d.scanning && now.Sub(d.since) >= d.Off:
		d.scanning = true
		d.since = now
	}
	return !d.scanning
}

// Scanning reports whether the last Yield call let the scan run.
func (d *MusicDuty) Scanning() bool { return !d.active || d.scanning }
