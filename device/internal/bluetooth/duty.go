package bluetooth

import (
	"log"
	"sync"
	"time"
)

// MusicDuty decides when the LE scan may run while music is streaming.
//
// Voice yields the scan outright (see Scanner.Yield), but music streams for
// hours and yielding for all of it would leave Bermuda blind. Scanning
// straight through it is worse: measured on VVV 2026-09-27, the scan stalled
// the link for up to 2.1s at a time and the music dropped out 15 times in a
// few minutes, stopping the moment the proxy was turned off.
//
// So the scan runs in bursts, sized from the buffer rather than the clock
// (#1004). The first version was a fixed 2s burst every 7s, never below 2.5s
// buffered, and it never ran on Sendspin: Music Assistant holds a live
// source (Spotify, radio) at exactly the buffer the player declares, which
// was 2s. How much a source buffers is the source's choice, so the duty
// takes whatever lead it is given:
//
//   - A burst starts once Off has passed and the lead covers the reserve
//     plus MinOn, and ends after On or as soon as the lead falls to the
//     reserve. The scan's damage stops when it stops (measured 2026-09-23),
//     so the reserve is what the music rides out the rest of a stall on.
//   - The reserve is learned. From a burst's start until the lead is back
//     where it started, the lowest lead is that burst's margin; a margin
//     under Safety raises the reserve by the shortfall, and a clean burst
//     lowers it a step. A link whose stalls run long gets a bigger reserve
//     and shorter bursts instead of dropouts.
//   - A margin still short with the reserve at ReserveMax means this link
//     cannot afford the scan during music at all, and the music wins: the
//     wait between bursts grows fourfold, to MaxBackoff, until a burst comes back
//     clean or the stream ends. Each probe at the cap can cost a dropout, so
//     the cap is long.
//   - A gap longer than MaxGap with music playing is counted and logged:
//     Bermuda ignores an advert older than 10s (AREA_MAX_AD_AGE in 0.8.7),
//     so that is where presence fails, and the log line says why.
type MusicDuty struct {
	On, Off, MinOn, MaxGap time.Duration
	Safety                 time.Duration
	ReserveMin, ReserveMax time.Duration
	MaxBackoff             time.Duration

	mu       sync.Mutex
	reserve  time.Duration
	backoff  time.Duration // added to Off after misses at ReserveMax
	scanning bool
	active   bool // music was streaming on the previous call
	since    time.Time
	lastScan time.Time // end of the last burst, or the stream's start
	starved  bool      // the current gap has passed MaxGap
	lastTick time.Time
	lastLead time.Duration

	// The open burst's window: from its start until the lead is back where
	// it started, or MaxGap later.
	win      bool
	winStart time.Time
	winLead  time.Duration
	winLow   time.Duration

	stats DutyStats
}

// DutyStats is reported as ble.music in the device stats.
type DutyStats struct {
	Bursts      uint64 `json:"bursts"`
	ScanMs      uint64 `json:"scanMs"`
	Cut         uint64 `json:"cut"`         // bursts ended early by a low lead
	NearMisses  uint64 `json:"nearMisses"`  // margins under Safety
	LowMarginMs int64  `json:"lowMarginMs"` // lowest margin since boot, -1 before any
	ReserveMs   int64  `json:"reserveMs"`
	Starved     uint64 `json:"starved"`   // gaps past MaxGap
	StarvedMs   uint64 `json:"starvedMs"` // music time spent past MaxGap
}

// MusicHeadroom is the lead a full burst needs at the starting reserve, with
// Safety to spare. The Sendspin player asks for it while the proxy is on.
const MusicHeadroom = 4 * time.Second

// NewMusicDuty returns the defaults: up to 2s of scan every 7s.
func NewMusicDuty() *MusicDuty {
	d := &MusicDuty{
		On: 2 * time.Second, Off: 5 * time.Second,
		MinOn: 500 * time.Millisecond, MaxGap: 8 * time.Second,
		Safety:     750 * time.Millisecond,
		ReserveMin: time.Second, ReserveMax: 3 * time.Second,
		MaxBackoff: 5 * time.Minute,
		reserve:    1500 * time.Millisecond,
	}
	d.stats.LowMarginMs = -1
	return d
}

// Yield reports whether the scan should stop now. Call it on every tick,
// with music reporting whether a music stream is arriving and lead how much
// of it is buffered. Without music it never asks for a yield.
func (d *MusicDuty) Yield(now time.Time, music bool, lead time.Duration) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !music {
		d.active, d.win, d.starved = false, false, false
		return false
	}
	if !d.active {
		// A new stream starts with the scan off: the buffer is filling.
		d.active, d.scanning, d.win, d.starved = true, false, false, false
		d.backoff = 0
		d.since, d.lastScan = now, now
		d.lastTick, d.lastLead = now, lead
	}
	dt := now.Sub(d.lastTick)
	// Playback drains the buffer at real time at most, so a faster fall is a
	// flush or a seek, not a stall, and says nothing about the scan.
	if d.lastLead-lead > dt+200*time.Millisecond {
		d.win = false
	}
	d.lastTick, d.lastLead = now, lead
	d.observe(now, lead)

	switch {
	case d.scanning && (lead <= d.reserve || now.Sub(d.since) >= d.On):
		if lead <= d.reserve {
			d.stats.Cut++
		}
		d.stats.ScanMs += uint64(now.Sub(d.since).Milliseconds())
		d.scanning, d.since, d.lastScan = false, now, now
	case !d.scanning && now.Sub(d.since) >= d.Off+d.backoff && lead >= d.reserve+d.MinOn:
		d.scanning, d.since, d.starved = true, now, false
		d.stats.Bursts++
		if !d.win {
			d.win, d.winStart, d.winLead, d.winLow = true, now, lead, lead
		}
	}
	if !d.scanning && now.Sub(d.lastScan) > d.MaxGap {
		if !d.starved {
			d.starved = true
			d.stats.Starved++
			log.Printf("[ble] no scan for %s of music: %dms buffered, a burst needs %dms",
				d.MaxGap, lead.Milliseconds(), (d.reserve + d.MinOn).Milliseconds())
		}
		d.stats.StarvedMs += uint64(dt.Milliseconds())
	}
	return !d.scanning
}

// observe follows a burst's window and adjusts the reserve when it closes.
func (d *MusicDuty) observe(now time.Time, lead time.Duration) {
	if !d.win {
		return
	}
	d.winLow = min(d.winLow, lead)
	if d.scanning || (lead < d.winLead && now.Sub(d.winStart) < d.MaxGap) {
		return
	}
	d.win = false
	margin := d.winLow
	if d.stats.LowMarginMs < 0 || margin.Milliseconds() < d.stats.LowMarginMs {
		d.stats.LowMarginMs = margin.Milliseconds()
	}
	switch {
	case margin < d.Safety && d.reserve < d.ReserveMax:
		d.stats.NearMisses++
		was := d.reserve
		d.reserve = min(d.ReserveMax, d.reserve+d.Safety-margin+250*time.Millisecond)
		log.Printf("[ble] music buffer fell to %dms around a scan burst: reserve %d -> %dms",
			margin.Milliseconds(), was.Milliseconds(), d.reserve.Milliseconds())
	case margin < d.Safety:
		d.stats.NearMisses++
		d.backoff = min(d.MaxBackoff, max(4*d.backoff, d.Off))
		log.Printf("[ble] music buffer fell to %dms around a scan burst at the %dms reserve: next burst in %s",
			margin.Milliseconds(), d.reserve.Milliseconds(), d.Off+d.backoff)
	default:
		d.backoff = 0
		if margin >= d.Safety+500*time.Millisecond {
			d.reserve = max(d.ReserveMin, d.reserve-100*time.Millisecond)
		}
	}
}

// Scanning reports whether the last Yield call let the scan run.
func (d *MusicDuty) Scanning() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return !d.active || d.scanning
}

// Stats is safe from any goroutine.
func (d *MusicDuty) Stats() DutyStats {
	d.mu.Lock()
	defer d.mu.Unlock()
	st := d.stats
	st.ReserveMs = d.reserve.Milliseconds()
	return st
}
