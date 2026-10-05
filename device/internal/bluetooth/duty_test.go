package bluetooth

import (
	"testing"
	"time"
)

const full = 5 * time.Second

func TestDutyNeverYieldsWithoutMusic(t *testing.T) {
	d := NewMusicDuty()
	now := time.Now()
	for i := 0; i < 100; i++ {
		if d.Yield(now.Add(time.Duration(i)*100*time.Millisecond), false, 0) {
			t.Fatal("yielded with no music streaming")
		}
	}
}

func TestDutyCyclesDuringMusic(t *testing.T) {
	d := NewMusicDuty()
	t0 := time.Now()
	var on, off time.Duration
	step := 100 * time.Millisecond
	for i := 0; i < 700; i++ { // 70s
		if d.Yield(t0.Add(time.Duration(i)*step), true, full) {
			off += step
		} else {
			on += step
		}
	}
	// 2s of every 7s, give or take a tick per phase.
	if got := float64(on) / float64(on+off); got < 0.26 || got > 0.31 {
		t.Fatalf("scan duty %.2f, want ~2/7", got)
	}
}

func TestDutyStartsWithScanOff(t *testing.T) {
	d := NewMusicDuty()
	now := time.Now()
	if !d.Yield(now, true, full) {
		t.Fatal("scan ran at the start of a stream")
	}
	if d.Yield(now.Add(d.Off), true, full) {
		t.Fatal("scan did not run after the off phase")
	}
}

// The longest a device can go unheard is Off plus a whole On of low buffer;
// with a healthy buffer it must stay under Bermuda's 10s area age.
func TestDutyGapFitsBermuda(t *testing.T) {
	d := NewMusicDuty()
	t0 := time.Now()
	var lastScan time.Time
	worst := time.Duration(0)
	for i := 0; i < 1000; i++ {
		now := t0.Add(time.Duration(i) * 100 * time.Millisecond)
		if !d.Yield(now, true, full) {
			if !lastScan.IsZero() {
				if g := now.Sub(lastScan); g > worst {
					worst = g
				}
			}
			lastScan = now
		}
	}
	if worst >= 10*time.Second || worst == 0 {
		t.Fatalf("longest gap between scans %v", worst)
	}
}

func TestDutyStopsScanOnLowBuffer(t *testing.T) {
	d := NewMusicDuty()
	now := time.Now()
	d.Yield(now, true, full)
	now = now.Add(d.Off)
	if d.Yield(now, true, full) {
		t.Fatal("expected the scan to be running")
	}
	if !d.Yield(now.Add(100*time.Millisecond), true, time.Second) {
		t.Fatal("scan kept running with 1s buffered")
	}
	// And it does not resume until the buffer recovers, however long it waits.
	if !d.Yield(now.Add(time.Minute), true, 2*time.Second) {
		t.Fatal("scan resumed below MinLead")
	}
}

func TestDutyResetsBetweenStreams(t *testing.T) {
	d := NewMusicDuty()
	now := time.Now()
	d.Yield(now, true, full)
	d.Yield(now.Add(d.Off), true, full) // scanning
	d.Yield(now.Add(d.Off+time.Second), false, 0)
	if !d.Yield(now.Add(d.Off+2*time.Second), true, full) {
		t.Fatal("a new stream did not start with the scan off")
	}
}
