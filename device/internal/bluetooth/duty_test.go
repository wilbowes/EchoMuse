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

// sim plays a source that keeps lead seconds buffered through a link that
// stalls for the whole of every burst and for tail after it, the worst the
// scan was measured to do (2026-09-27: stalls to 2.1s). It returns the
// duty's account, how many times the buffer ran dry, and the longest gap between
// bursts.
func sim(d *MusicDuty, lead, tail, length time.Duration) (st DutyStats, dry int, worst time.Duration) {
	t0 := time.Now()
	step := 100 * time.Millisecond
	buf := lead
	var stoppedAt, lastScan time.Time
	was := false
	for now := t0; now.Sub(t0) < length; now = now.Add(step) {
		scanning := !d.Yield(now, true, buf)
		if was && !scanning {
			stoppedAt = now
		}
		if scanning {
			if !was && !lastScan.IsZero() {
				worst = max(worst, now.Sub(lastScan))
			}
			lastScan = now
		}
		was = scanning
		if scanning || (!stoppedAt.IsZero() && now.Sub(stoppedAt) < tail) {
			if buf > 0 && buf <= step {
				dry++
			}
			buf = max(0, buf-step)
		} else {
			buf = lead // the backlog arrives as soon as the link moves
		}
	}
	return d.Stats(), dry, worst
}

// What a live source buffers when the player asks for MusicHeadroom: 3812ms
// against aiosendspin 9.1.1 (tools/sendspin_interop), the rest being the
// player's own pipeline.
const asked = MusicHeadroom - 200*time.Millisecond

// The goals, for every source that buffers what the player asks for: the music
// never runs dry, and no device goes unheard past Bermuda's 10s area age.
func TestDutyMeetsBothGoals(t *testing.T) {
	for _, lead := range []time.Duration{asked, 5 * time.Second, 30 * time.Second} {
		for _, tail := range []time.Duration{0, 500 * time.Millisecond, time.Second} {
			st, dry, worst := sim(NewMusicDuty(), lead, tail, 10*time.Minute)
			if dry > 0 {
				t.Errorf("lead %v tail %v: ran dry %d times", lead, tail, dry)
			}
			if worst > 8*time.Second || st.Starved > 0 {
				t.Errorf("lead %v tail %v: gap %v, starved %d", lead, tail, worst, st.Starved)
			}
		}
	}
}

// A shallow buffer — the HA music plane after a dropout, or a server that
// ignores min_buffer_ms — still gets bursts, cut short before they cost the
// music. The fixed 2.5s floor this replaced gave it none (#1004).
func TestShallowBufferGetsShortBursts(t *testing.T) {
	st, dry, worst := sim(NewMusicDuty(), 2200*time.Millisecond, 500*time.Millisecond, 5*time.Minute)
	if dry > 0 {
		t.Fatalf("ran dry %d times", dry)
	}
	if st.Cut == 0 || worst > 8*time.Second || st.Starved > 0 {
		t.Fatalf("cut %d, gap %v, starved %d", st.Cut, worst, st.Starved)
	}
}

// MusicHeadroom is what the Sendspin player asks for, so it must hold a full
// burst through a full stall without the reserve cutting it short.
func TestHeadroomFitsAFullBurst(t *testing.T) {
	st, _, _ := sim(NewMusicDuty(), asked, 0, 5*time.Minute)
	if st.Cut > 0 || st.Bursts == 0 {
		t.Fatalf("bursts %d, cut short %d", st.Bursts, st.Cut)
	}
	if per := time.Duration(st.ScanMs/st.Bursts) * time.Millisecond; per < NewMusicDuty().On-100*time.Millisecond {
		t.Fatalf("average burst %v, want %v", per, NewMusicDuty().On)
	}
}

// A link whose stalls outlast the scan teaches the duty a bigger reserve
// before the music runs dry; a clean one gives it back.
func TestDutyLearnsTheReserve(t *testing.T) {
	d := NewMusicDuty()
	st, dry, _ := sim(d, asked, 1500*time.Millisecond, 3*time.Minute)
	if dry > 0 || st.NearMisses == 0 || st.ReserveMs <= 1500 {
		t.Fatalf("dry %d, near misses %d, reserve %dms", dry, st.NearMisses, st.ReserveMs)
	}
	st, _, _ = sim(d, 30*time.Second, 0, 10*time.Minute)
	if st.ReserveMs != d.ReserveMin.Milliseconds() {
		t.Fatalf("reserve %dms after a clean link, want %v", st.ReserveMs, d.ReserveMin)
	}
}

// A link that cannot afford the scan even at ReserveMax gets few bursts, not
// a dropout every cycle: the music wins, and the lost presence is counted.
func TestDutyBacksOffWhenTheReserveIsNotEnough(t *testing.T) {
	d := NewMusicDuty()
	st, dry, _ := sim(d, asked, 3*time.Second, 10*time.Minute)
	if st.ReserveMs != d.ReserveMax.Milliseconds() {
		t.Fatalf("reserve %dms, want the %v cap", st.ReserveMs, d.ReserveMax)
	}
	// A burst every 7s would be ~85 in 10 minutes, each one a dropout.
	if dry > 8 || st.Bursts > 8 || st.Starved == 0 {
		t.Fatalf("dry %d, bursts %d, starved %d", dry, st.Bursts, st.Starved)
	}
}

// A source too shallow for a burst cannot be helped, but it must be reported:
// that is the support case this account exists for.
func TestStarvationIsCounted(t *testing.T) {
	st, dry, _ := sim(NewMusicDuty(), 1800*time.Millisecond, 0, time.Minute)
	if dry > 0 || st.Bursts > 0 {
		t.Fatalf("dry %d, bursts %d", dry, st.Bursts)
	}
	if st.Starved != 1 || st.StarvedMs < 50_000 {
		t.Fatalf("starved %d for %dms, want one gap of ~52s", st.Starved, st.StarvedMs)
	}
}

// A flush or a seek empties the buffer faster than playback can. That is not
// the scan's doing, and must not grow the reserve.
func TestFlushDoesNotTrainTheReserve(t *testing.T) {
	d := NewMusicDuty()
	now := time.Now()
	step := 100 * time.Millisecond
	d.Yield(now, true, full)
	for d.Yield(now, true, full) {
		now = now.Add(step)
	}
	d.Yield(now.Add(step), true, 100*time.Millisecond)
	for i := 2; i < 100; i++ {
		d.Yield(now.Add(time.Duration(i)*step), true, full)
	}
	if st := d.Stats(); st.NearMisses != 0 || st.ReserveMs > 1500 {
		t.Fatalf("near misses %d, reserve %dms", st.NearMisses, st.ReserveMs)
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
