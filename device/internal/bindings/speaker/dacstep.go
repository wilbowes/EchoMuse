package speaker

import (
	"log"
	"strings"
	"time"
)

// dacStepLog logs the raw ALSA status around a DAC reading that is out of
// step with the one before it, to find what the early readings in #707 are.
//
// Consecutive readings should differ by a whole number of periods, since
// the write loop adds one period per iteration. A step more than
// dacStepGate off that is logged with both status blocks (hw_ptr and
// appl_ptr are the evidence), at most dacStepMax a minute so a device that
// does this constantly cannot flood the log.
type dacStepLog struct {
	prevAt     time.Time
	prevStatus string
	window     time.Time
	logged     int
}

const (
	dacStepGate = 3 * time.Millisecond
	dacStepMax  = 20
)

func (l *dacStepLog) note(playAt time.Time, read time.Duration, status string, period time.Duration) {
	prevAt, prevStatus := l.prevAt, l.prevStatus
	l.prevAt, l.prevStatus = playAt, status
	if prevAt.IsZero() || period <= 0 {
		return
	}
	dev, ok := stepDeviation(playAt.Sub(prevAt), period)
	if !ok || (dev < dacStepGate && dev > -dacStepGate) {
		return
	}
	if now := time.Now(); now.Sub(l.window) >= time.Minute {
		l.window, l.logged = now, 0
	}
	if l.logged >= dacStepMax {
		return
	}
	l.logged++
	log.Printf("[speaker] dac step %+dus (read %dus) prev{%s} now{%s}",
		dev.Microseconds(), read.Microseconds(), compactStatus(prevStatus), compactStatus(status))
}

// stepDeviation is how far a step between two readings is from the nearest
// whole number of periods. Not ok past a second: the source was idle in
// between and the step says nothing.
func stepDeviation(step, period time.Duration) (time.Duration, bool) {
	if step < 0 || step > time.Second {
		return 0, false
	}
	n := (step + period/2) / period
	return step - n*period, true
}

// compactStatus puts an ALSA status block on one line.
func compactStatus(s string) string {
	var parts []string
	for _, line := range strings.Split(s, "\n") {
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		parts = append(parts, strings.TrimSpace(k)+"="+strings.TrimSpace(v))
	}
	return strings.Join(parts, " ")
}
