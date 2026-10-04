package sendspin

import (
	"encoding/binary"
	"math"
	"testing"
	"time"
)

const periodFrames = 2048 // the speaker's mix period

// syncedFilter maps server time to client time as client = server - offset.
func syncedFilter(offset int64) *timeFilter {
	f := newTimeFilter()
	f.update(offset, 200, 1)
	f.update(offset, 200, 1_000_000)
	return f
}

// ramp is n stereo frames whose every frame says where it came from: the
// left channel counts up, the right is its negative, so a frame that loses
// its pairing or a channel that moves shows.
func ramp(start, n int) []int16 {
	s := make([]int16, 2*n)
	for i := 0; i < n; i++ {
		v := int16((start + i) % 30000)
		s[2*i], s[2*i+1] = v, -v
	}
	return s
}

func at(us int64) time.Time { return epoch.Add(time.Duration(us) * time.Microsecond) }

func sample(out []byte, i int) int16 { return int16(binary.LittleEndian.Uint16(out[i*4:])) }

func sampleR(out []byte, i int) int16 { return int16(binary.LittleEndian.Uint16(out[i*4+2:])) }

// pairsHold checks that every frame of out still has R == -L, as ramp made it.
func pairsHold(t *testing.T, out []byte) {
	t.Helper()
	for i := 0; i < len(out)/4; i++ {
		if sampleR(out, i) != -sample(out, i) {
			t.Fatalf("frame %d: L %d R %d, the channels came apart", i, sample(out, i), sampleR(out, i))
		}
	}
}

func newTestPlayer(offset int64) *player {
	p := newPlayer(syncedFilter(offset))
	// 10s of audio in 25ms chunks, first sample due at server 5s.
	for c := 0; c < 400; c++ {
		p.push(5_000_000+int64(c)*25_000, ramp(c*1200, 1200))
	}
	return p
}

func TestFirstPeriodLandsOnTheSampleDueAtThatInstant(t *testing.T) {
	const off = 777_000 // server clock ahead of ours
	out := make([]byte, periodFrames*4)
	for _, tc := range []struct {
		name      string
		playAtUs  int64
		wantFirst int16 // sample value at out[0], -1 for silence then data
		lead      int
	}{
		{"exactly due", 5_000_000 - off, 0, 0},
		{"10ms late: skip 480", 5_000_000 - off + 10_000, 480, 0},
		{"5ms early: 240 frames of silence", 5_000_000 - off - 5_000, 0, 240},
	} {
		p := newTestPlayer(off)
		if !p.Fill(out, at(tc.playAtUs)) {
			t.Fatalf("%s: nothing played", tc.name)
		}
		pairsHold(t, out)
		for i := 0; i < tc.lead; i++ {
			if sample(out, i) != 0 {
				t.Fatalf("%s: frame %d not silent", tc.name, i)
			}
		}
		if got := sample(out, tc.lead); got != tc.wantFirst {
			t.Errorf("%s: first sample %d, want %d", tc.name, got, tc.wantFirst)
		}
	}
}

func TestNotYetDueIsNothingToPlay(t *testing.T) {
	p := newTestPlayer(0)
	if p.Fill(make([]byte, periodFrames*4), at(5_000_000-200_000)) {
		t.Error("played a stream 200ms before its first sample was due")
	}
}

// The DAC's crystal is not the server's. Measured on this hardware at
// -0.056% against the system clock; run a DAC 600ppm slow and check the
// correction keeps the error inside the spec's 1ms floor using only small
// steps — never a second snap, never more than 0.5% speed in a period.
func TestDriftIsCorrectedWithSmallStepsWithinOneMillisecond(t *testing.T) {
	for _, ppm := range []float64{-600, -56, 0, 56, 600} {
		p := newTestPlayer(0)
		out := make([]byte, periodFrames*4)
		periodUs := float64(periodFrames) * 1e6 / 48000 * (1 + ppm/1e6)
		playAt := 5_000_000.0
		var prevOut int16 = -1
		for k := 0; k < 180; k++ { // ~7.7s
			if !p.Fill(out, at(int64(playAt))) {
				t.Fatalf("ppm %v period %d: ran dry", ppm, k)
			}
			// Drops and repeats move whole frames, never one channel.
			pairsHold(t, out)
			// Continuity: consecutive periods join with at most a step.
			if prevOut >= 0 {
				if d := int(sample(out, 0)) - int(prevOut); d < 0 || d > 1+maxStepFrames {
					t.Fatalf("ppm %v period %d: discontinuity %d at the boundary", ppm, k, d)
				}
			}
			prevOut = sample(out, periodFrames-1)
			if k > 20 {
				if e := p.stats().LastErrorUs; e > 1000 || e < -1000 {
					t.Fatalf("ppm %v period %d: error %dus outside ±1ms", ppm, k, e)
				}
			}
			playAt += periodUs
		}
		st := p.stats()
		if st.Snaps != 1 {
			t.Errorf("ppm %v: %d snaps, want only the start", ppm, st.Snaps)
		}
		if ppm == 0 && st.Corrections != 0 {
			t.Errorf("no drift, yet %d corrections", st.Corrections)
		}
	}
}

// A disturbance past 5ms (a stall, a clock step) is a one-shot resync, not a
// second of audible speed change.
func TestALargeErrorSnaps(t *testing.T) {
	p := newTestPlayer(0)
	out := make([]byte, periodFrames*4)
	p.Fill(out, at(5_000_000))
	next := int64(5_000_000) + int64(periodFrames)*1_000_000/48000 + 20_000 // 20ms jump
	p.Fill(out, at(next))
	if st := p.stats(); st.Snaps != 2 {
		t.Errorf("snaps %d, want 2", st.Snaps)
	}
	want := int16((2048 + 960) % 30000)
	if got := sample(out, 0); got < want-1 || got > want+1 {
		t.Errorf("after the jump, first sample %d, want ~%d", got, want)
	}
}

func TestRunningDryMidPeriodPadsWithSilenceAndCountsAnUnderrun(t *testing.T) {
	p := newPlayer(syncedFilter(0))
	p.push(5_000_000, ramp(0, 1000))
	out := make([]byte, periodFrames*4)
	if !p.Fill(out, at(5_000_000)) {
		t.Fatal("nothing played")
	}
	if sample(out, 999) != 999 || sample(out, 1000) != 0 || sample(out, periodFrames-1) != 0 {
		t.Error("tail after the data is not silence")
	}
	if p.stats().Underruns != 1 {
		t.Errorf("underruns %d", p.stats().Underruns)
	}
	if p.Fill(out, at(5_100_000)) {
		t.Error("an empty queue played")
	}
}

func TestClearDropsEverything(t *testing.T) {
	p := newTestPlayer(0)
	p.clear()
	if p.active() || p.buffered() != 0 {
		t.Error("clear left audio queued")
	}
}

func TestUnsynchronisedClockPlaysNothing(t *testing.T) {
	p := newPlayer(newTimeFilter())
	p.push(5_000_000, ramp(0, 48000))
	if p.Fill(make([]byte, periodFrames*4), at(5_000_000)) {
		t.Error("played before the clock was synchronised")
	}
}

func TestVolumeFollowsThePerceptualCurve(t *testing.T) {
	if volumeGain(100, false) != unity || volumeGain(0, false) != 0 || volumeGain(80, true) != 0 {
		t.Error("endpoints or mute wrong")
	}
	want := math.Pow(0.5, 1.5) * float64(unity)
	if g := float64(volumeGain(50, false)); math.Abs(g-want) > 1 {
		t.Errorf("volume 50: gain %v, want %v", g, want)
	}
	// Applied with a ramp across the period: no step at the boundary.
	p := newPlayer(syncedFilter(0))
	full := make([]int16, 2*48000)
	for i := range full {
		full[i] = 20000
	}
	p.push(5_000_000, full)
	p.setGain(50, false)
	out := make([]byte, periodFrames*4)
	p.Fill(out, at(5_000_000))
	if sample(out, 0) != 20000 {
		t.Errorf("ramp did not start from the old gain: %d", sample(out, 0))
	}
	if s := sample(out, periodFrames-1); s > 7200 || s < 7000 {
		t.Errorf("ramp did not reach the new gain: %d", s)
	}
}
