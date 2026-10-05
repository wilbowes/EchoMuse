//go:build server && bench

package speaker

import (
	"log"
	"time"
)

// writeLoopMeter reports how late the ALSA write loop gets, once a minute, for
// sizing the hardware buffer tier: the worst gap between writes (time the
// buffer drained unrefilled) and the loop's own work between them. Two
// time.Now per period. Bench builds only.
type writeLoopMeter struct {
	last, workDone, window time.Time
	maxGap, maxWork        time.Duration
	periods                int
}

func (m *writeLoopMeter) beforeWrite() { m.workDone = time.Now() }

func (m *writeLoopMeter) afterWrite() {
	now := time.Now()
	if m.window.IsZero() {
		m.window = now
	}
	if !m.last.IsZero() {
		if g := now.Sub(m.last); g > m.maxGap {
			m.maxGap = g
		}
		if w := m.workDone.Sub(m.last); w > m.maxWork {
			m.maxWork = w
		}
		m.periods++
	}
	m.last = now
	if now.Sub(m.window) >= time.Minute {
		log.Printf("[speaker] write loop: max gap %.1fms, max work %.1fms over %d periods (period %.1fms, hw buffer %.0fms)",
			float64(m.maxGap.Microseconds())/1000, float64(m.maxWork.Microseconds())/1000, m.periods,
			float64(periodSize)*1000/48000, float64(alsaBufferFrames)*1000/48000)
		m.window, m.maxGap, m.maxWork, m.periods = now, 0, 0, 0
	}
}
