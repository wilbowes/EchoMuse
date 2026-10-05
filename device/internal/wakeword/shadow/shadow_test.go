package shadow

import (
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

// fakeInferer stands in for ONNX Runtime: it returns correctly shaped tensors
// and a score the test controls. This is the payoff of keeping inference behind
// an interface — every property below is about the scorer's behaviour under
// load, and none of it needs a model, a runtime, or a device.
type fakeInferer struct {
	mu    sync.Mutex
	score float32
	// delay simulates a slow device, which is how the drop path gets tested.
	delay time.Duration
	// failEmbed makes Embed return an error, to check the scorer survives it.
	failEmbed bool
	calls     int
}

func (f *fakeInferer) Melspec(samples []float32) ([]float32, int, error) {
	// Mirror the real model's frame count so the Detector's windowing behaves
	// as it does in production: ceil(samples/160 - 3).
	frames := len(samples)/160 - 3
	if frames < 0 {
		frames = 0
	}
	return make([]float32, frames*wakeword.MelBins), frames, nil
}

func (f *fakeInferer) Embed(window []float32) ([]float32, error) {
	f.mu.Lock()
	f.calls++
	d, fail := f.delay, f.failEmbed
	f.mu.Unlock()
	if fail {
		return nil, errors.New("synthetic embed failure")
	}
	if d > 0 {
		time.Sleep(d)
	}
	return make([]float32, wakeword.FeatDim), nil
}

func (f *fakeInferer) Classify(feats []float32) (float32, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.score, nil
}

func (f *fakeInferer) set(score float32, delay time.Duration) {
	f.mu.Lock()
	f.score, f.delay = score, delay
	f.mu.Unlock()
}

func frame() []int16 { return make([]int16, wakeword.ChunkSamples) }

// pushAll feeds n frames WITHOUT overflowing the queue, waiting for each to be
// accounted for before sending the next. A tight loop cannot be used where the
// test depends on frames actually being scored: the queue is only queueFrames
// deep, so pushing 16 frames at once drops half of them and the detector never
// reaches Ready. (The flood tests below want exactly that behaviour, and push
// in a tight loop on purpose.)
func pushAll(t *testing.T, s *Scorer, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		before := s.processed()
		s.Push(frame())
		waitFor(t, "a frame to be scored", func() bool { return s.processed() > before })
	}
	if st := s.peek(); st.Drops != 0 {
		t.Fatalf("warm-up dropped %d frames — the test is not measuring what it thinks", st.Drops)
	}
}

// waitFor polls until cond holds, so tests do not depend on a fixed sleep for
// work that happens on another goroutine.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// TestPushNeverBlocks is the property the whole design exists for: the mic
// goroutine must not be delayed by inference, no matter how slow the device is.
// With a scorer wedged on a very slow model, pushing far more frames than the
// queue holds must still return promptly.
func TestPushNeverBlocks(t *testing.T) {
	inf := &fakeInferer{}
	inf.set(0, 200*time.Millisecond) // pathologically slow
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	start := time.Now()
	for i := 0; i < queueFrames*10; i++ {
		s.Push(frame())
	}
	elapsed := time.Since(start)

	// Each frame would cost 200ms if Push waited; 80 frames would be 16s. A
	// generous ceiling still separates the two behaviours by two orders.
	if elapsed > time.Second {
		t.Fatalf("pushing %d frames took %v — Push is blocking on inference",
			queueFrames*10, elapsed)
	}
	st := s.Drain()
	if st.Drops == 0 {
		t.Error("expected drops when the scorer cannot keep up")
	}
	if st.Drops+st.Frames == 0 {
		t.Error("no frames accounted for at all")
	}
}

// TestDropsAreCountedNotSilent pins that a dropped frame is visible. A shadow
// comparison running on a subset of the audio is still useful, but only if you
// know that is what you are looking at.
func TestDropsAreCountedNotSilent(t *testing.T) {
	inf := &fakeInferer{}
	inf.set(0, 50*time.Millisecond)
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	const pushed = 40
	for i := 0; i < pushed; i++ {
		s.Push(frame())
	}
	// Let the queue drain so the accounting settles.
	waitFor(t, "scorer to finish the queued frames", func() bool {
		return s.processed()+s.peek().Drops >= pushed
	})
	st := s.Drain()
	if got := st.Frames + st.Drops; got != pushed {
		t.Errorf("accounted for %d frames (%d scored + %d dropped), pushed %d",
			got, st.Frames, st.Drops, pushed)
	}
}

// TestCrossingFiresOncePerUtterance covers the refractory period. Wake scores
// stay above threshold for several consecutive frames, so an unguarded
// implementation reports a burst of crossings for one utterance — which floods
// the log and makes the controller's correlation match an arbitrary one of them.
func TestCrossingFiresOncePerUtterance(t *testing.T) {
	var (
		mu      sync.Mutex
		crosses []float32
	)
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(score, _ float32, at time.Time) {
		mu.Lock()
		crosses = append(crosses, score)
		mu.Unlock()
	})
	defer s.Close()

	// Warm the detector past the not-ready window with silence.
	inf.set(0.0, 0)
	pushAll(t, s, wakeword.FeatWindow)
	waitFor(t, "detector to become ready", func() bool { return s.Ready() })

	// Now a sustained "detection" spanning many frames.
	inf.set(0.9, 0)
	pushAll(t, s, 12)
	waitFor(t, "a crossing to fire", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(crosses) >= 1
	})
	// Give any spurious extra crossings a chance to appear.
	time.Sleep(50 * time.Millisecond)

	mu.Lock()
	n := len(crosses)
	mu.Unlock()
	if n != 1 {
		t.Errorf("got %d crossings for one sustained utterance, want 1", n)
	}
	if st := s.Drain(); st.Crossings != 1 {
		t.Errorf("stats recorded %d crossings, want 1", st.Crossings)
	}
}

// TestBelowThresholdDoesNotCross keeps the obvious direction honest, and checks
// MaxScore is reported — the number that distinguishes "nearly detecting" from
// "nowhere close", which is the whole point of shadow mode when a wake is
// missed.
func TestBelowThresholdDoesNotCross(t *testing.T) {
	var fired int
	var mu sync.Mutex
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) {
		mu.Lock()
		fired++
		mu.Unlock()
	})
	defer s.Close()

	inf.set(0.42, 0)
	pushAll(t, s, wakeword.FeatWindow+5)

	mu.Lock()
	got := fired
	mu.Unlock()
	if got != 0 {
		t.Errorf("%d crossings fired below threshold", got)
	}
	st := s.Drain()
	if st.Crossings != 0 {
		t.Errorf("recorded %d crossings below threshold", st.Crossings)
	}
	if st.MaxScore != 0.42 {
		t.Errorf("MaxScore %v, want 0.42 — a missed wake needs the near-miss score", st.MaxScore)
	}
}

// TestInferenceErrorsDoNotStopScoring: losing shadow data is always preferable
// to losing the audio path, so an inference failure must be counted and
// survived rather than killing the goroutine.
func TestInferenceErrorsDoNotStopScoring(t *testing.T) {
	inf := &fakeInferer{failEmbed: true}
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	for i := 0; i < 5; i++ {
		before := s.processed()
		s.Push(frame())
		waitFor(t, "a frame to be accounted for", func() bool { return s.processed() > before })
	}
	waitFor(t, "errors to be recorded", func() bool { return s.peek().Errors >= 5 })

	// Recover, and confirm the scorer is still alive and scoring.
	inf.mu.Lock()
	inf.failEmbed = false
	inf.mu.Unlock()
	st := s.Drain()
	if st.Errors < 5 || st.LastErr == "" {
		t.Errorf("errors not recorded: %+v", st)
	}

	pushAll(t, s, wakeword.FeatWindow+2)
	if st := s.peek(); st.Frames == 0 || st.Errors != 0 {
		t.Errorf("scoring did not resume cleanly after errors: %+v", st)
	}
}

// TestDrainZeroesCounters pins that stats are per-window, not cumulative since
// boot: a series of windows is readable, a series of running totals is not.
func TestDrainZeroesCounters(t *testing.T) {
	inf := &fakeInferer{}
	inf.set(0.3, 0)
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	pushAll(t, s, wakeword.FeatWindow+1)
	if first := s.Drain(); first.Frames == 0 {
		t.Fatal("first Drain returned no frames")
	}
	if second := s.Drain(); second.Frames != 0 || second.MaxScore != 0 {
		t.Errorf("second Drain returned %+v, want zeroed", second)
	}
}

// TestResetClearsDetectorState guards the mic-restart case: a StopMic/StartMic
// pair happens after every voice turn, and audio from before the gap must not
// contribute to a score after it.
func TestResetClearsDetectorState(t *testing.T) {
	inf := &fakeInferer{}
	inf.set(0.1, 0)
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	pushAll(t, s, wakeword.FeatWindow)
	waitFor(t, "detector to become ready", func() bool { return s.Ready() })

	s.Reset()
	// The reset is applied by the scorer goroutine on the next frame.
	s.Push(frame())
	waitFor(t, "reset to take effect", func() bool { return !s.Ready() })
}

// TestCloseIsIdempotent — Close runs on config changes and on shutdown, and
// those can overlap.
func TestCloseIsIdempotent(t *testing.T) {
	s := NewScorer(&fakeInferer{}, 0.5, nil)
	s.Close()
	s.Close()
}

// processed and peek let tests observe the scorer without racing on state the
// scorer goroutine owns, and without consuming the counters the way Drain does.
func (s *Scorer) processed() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stats.Frames
}

func (s *Scorer) peek() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stats
}

// TestBargeThresholdAppliesOnlyWhileSpeaking is the fix for the bug that made
// every barge-in look like an on-device miss: the controller lowers its wake bar
// to bargeInThreshold during playback (echo at the mic is ~25dB louder than the
// person, so speech-over-TTS scores are depressed), and a device scoring against
// the normal threshold is not answering the same question.
func TestBargeThresholdAppliesOnlyWhileSpeaking(t *testing.T) {
	var speaking bool
	var mu sync.Mutex
	var crosses int

	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) {
		mu.Lock()
		crosses++
		mu.Unlock()
	})
	defer s.Close()
	s.SetBargeThreshold(0.10, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return speaking
	})

	// A score of 0.2 is below the normal bar and above the barge bar.
	inf.set(0.2, 0)
	pushAll(t, s, wakeword.FeatWindow+2)
	mu.Lock()
	got := crosses
	mu.Unlock()
	if got != 0 {
		t.Errorf("crossed at 0.2 with the speaker idle (bar should be 0.5)")
	}
	if thr := s.peek().Threshold; thr != 0.5 {
		t.Errorf("reported threshold %v while idle, want 0.5", thr)
	}

	// Now the speaker is streaming: the same score must cross.
	mu.Lock()
	speaking = true
	mu.Unlock()
	s.Drain()
	pushAll(t, s, 3)
	waitFor(t, "a barge crossing", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return crosses >= 1
	})
	if thr := s.peek().Threshold; thr != 0.10 {
		t.Errorf("reported threshold %v while speaking, want 0.10", thr)
	}
}

// TestBargeThresholdIgnoredWhenUnset keeps an un-configured scorer behaving
// exactly as before — a controller that never sends a barge threshold must not
// change the device's behaviour.
func TestBargeThresholdIgnoredWhenUnset(t *testing.T) {
	inf := &fakeInferer{}
	var fired int
	var mu sync.Mutex
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) {
		mu.Lock()
		fired++
		mu.Unlock()
	})
	defer s.Close()
	// Speaker "active", but no barge threshold configured.
	s.SetBargeThreshold(0, func() bool { return true })
	inf.set(0.2, 0)
	pushAll(t, s, wakeword.FeatWindow+2)
	mu.Lock()
	defer mu.Unlock()
	if fired != 0 {
		t.Errorf("crossed at 0.2 with no barge threshold set")
	}
}

// TestBargeThresholdNeverRaisesTheBar: if someone configures a barge threshold
// ABOVE the normal one, the normal one still wins. The barge threshold exists to
// make detection easier during playback, never harder.
func TestBargeThresholdNeverRaisesTheBar(t *testing.T) {
	inf := &fakeInferer{}
	var fired int
	var mu sync.Mutex
	s := NewScorer(inf, 0.3, func(float32, float32, time.Time) {
		mu.Lock()
		fired++
		mu.Unlock()
	})
	defer s.Close()
	s.SetBargeThreshold(0.9, func() bool { return true })
	inf.set(0.4, 0) // above the normal 0.3, below the misconfigured 0.9
	pushAll(t, s, wakeword.FeatWindow+2)
	waitFor(t, "a crossing at the normal threshold", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return fired >= 1
	})
}

func TestDrainReportsAndResetsTheTimingMaxima(t *testing.T) {
	// These exist to explain drops, which is a question about the TAIL — so
	// they must be maxima that survive being surrounded by fast frames, and
	// must reset per window, or one early stall would haunt every later report.
	s := NewScorer(&fakeInferer{}, 0.5, nil)
	defer s.Close()

	s.maxInferMs.Store(37)
	s.maxGapMs.Store(812)

	got := s.Drain()
	if got.MaxInferMs != 37 {
		t.Fatalf("MaxInferMs = %d, want 37", got.MaxInferMs)
	}
	if got.MaxGapMs != 812 {
		t.Fatalf("MaxGapMs = %d, want 812", got.MaxGapMs)
	}

	again := s.Drain()
	if again.MaxInferMs != 0 || again.MaxGapMs != 0 {
		t.Fatalf("maxima must reset per window, got infer=%d gap=%d",
			again.MaxInferMs, again.MaxGapMs)
	}
}

func TestTheFirstFrameRecordsNoProducerGap(t *testing.T) {
	// A zero-valued lastPush would make the first frame look like a gap of
	// however long the process had been running — an enormous MaxGapMs on
	// every startup, pointing at a producer stall that never happened.
	s := NewScorer(&fakeInferer{}, 0.5, nil)
	defer s.Close()

	s.Push(make([]int16, 1280))
	if got := s.maxGapMs.Load(); got != 0 {
		t.Fatalf("first frame must not record a gap, got %dms", got)
	}
}

func TestTheProducerGapIsMeasuredForBothEntryPoints(t *testing.T) {
	// Measured in enqueue rather than in Push/PushBytes so one site covers
	// both — the same reason drops are counted in exactly one place.
	s := NewScorer(&fakeInferer{}, 0.5, nil)
	defer s.Close()

	s.Push(make([]int16, 1280))
	time.Sleep(25 * time.Millisecond)
	s.PushBytes(make([]byte, 2560))

	if got := s.maxGapMs.Load(); got < 20 {
		t.Fatalf("gap across Push -> PushBytes should be ~25ms, got %dms", got)
	}
}

// TestCrossingReportsTheThresholdItCleared pins the value the controller
// records against the turn. During playback the bar drops to the barge-in
// threshold, and reporting the nominal one instead is what once produced rows
// claiming a wake had fired below its own threshold — self-contradictory data
// that made every barge-in look like an on-device miss.
//
// Reported from the scorer rather than re-derived by the callback because only
// the scorer knows: the bar depends on whether the speaker was streaming at the
// instant the frame was SCORED, and playback can end before the callback runs.
func TestCrossingReportsTheThresholdItCleared(t *testing.T) {
	var speaking bool
	var mu sync.Mutex
	var got []float32

	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(_, threshold float32, _ time.Time) {
		mu.Lock()
		got = append(got, threshold)
		mu.Unlock()
	})
	defer s.Close()
	s.SetBargeThreshold(0.10, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return speaking
	})

	// Idle: a strong score clears the normal bar, and must say so.
	inf.set(0.9, 0)
	pushAll(t, s, wakeword.FeatWindow+2)
	waitFor(t, "an idle crossing", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) >= 1
	})
	mu.Lock()
	if got[0] != 0.5 {
		t.Errorf("idle crossing reported threshold %v, want 0.5", got[0])
	}
	// Speaking: a weak score clears only the barge bar, and must report THAT.
	speaking = true
	mu.Unlock()
	inf.set(0.2, 0)
	// Past the refractory period, or the second crossing is suppressed.
	time.Sleep(DefaultRefractory + 50*time.Millisecond)
	pushAll(t, s, 3)
	waitFor(t, "a barge crossing", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) >= 2
	})
	mu.Lock()
	defer mu.Unlock()
	if got[1] != 0.10 {
		t.Errorf("barge crossing reported threshold %v, want 0.10 — the bar it "+
			"actually cleared, not the nominal one", got[1])
	}
}

// TestCloseDuringPushDoesNotPanic reproduces the crash a wake word change
// caused on hardware (2026-08-16).
//
// The mic goroutine captures the scorer pointer ONCE per stream and keeps
// pushing to it for the life of that stream. A config push replaces the
// scorer and closes the old one underneath it. While Close() closed the
// channel enqueue sends on, the next 80ms frame panicked the process with
// "send on closed channel" — the device died and start_server.sh restarted
// it seconds later, which read as "changing the wake word crashes the Dot".
//
// Present since shadow mode shipped and latent because it needs BOTH
// on-device scoring active AND the model changed.
func TestCloseDuringPushDoesNotPanic(t *testing.T) {
	for i := 0; i < 50; i++ {
		s := NewScorer(&fakeInferer{score: 0.1}, 0.5, func(float32, float32, time.Time) {})

		var wg sync.WaitGroup
		wg.Add(1)
		go func() { // the mic goroutine, holding the pointer it started with
			defer wg.Done()
			for j := 0; j < 200; j++ {
				s.Push(make([]int16, 1280))
			}
		}()

		time.Sleep(time.Duration(i%5) * time.Millisecond)
		s.Close() // the config push
		wg.Wait()

		s.Close() // documented as safe to call twice
	}
}

// TestBargeBarNeedsTwoConsecutiveFrames: at the barge-in bar one frame is not a
// wake. That bar is ~10x below the wake threshold and scores the assistant's
// own voice, and a single-frame rule cut long answers off (em_barge.decide).
func TestBargeBarNeedsTwoConsecutiveFrames(t *testing.T) {
	var mu sync.Mutex
	var crosses int
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) {
		mu.Lock()
		crosses++
		mu.Unlock()
	})
	defer s.Close()
	s.SetBargeThreshold(0.10, func() bool { return true })

	inf.set(0.0, 0)
	pushAll(t, s, wakeword.FeatWindow+1)
	// Alternate above and below the bar: never two in a row.
	for i := 0; i < 6; i++ {
		inf.set(0.2, 0)
		pushAll(t, s, 1)
		inf.set(0.0, 0)
		pushAll(t, s, 1)
	}
	mu.Lock()
	got := crosses
	mu.Unlock()
	if got != 0 {
		t.Fatalf("crossed %d times on isolated frames at the barge bar", got)
	}
	inf.set(0.2, 0)
	pushAll(t, s, 2)
	waitFor(t, "a two-frame barge crossing", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return crosses == 1
	})
}

// TestNormalBarStillFiresOnOneFrame: the two-frame rule is for the barge bar
// only. At the wake threshold one frame is a wake, as it always was.
func TestNormalBarStillFiresOnOneFrame(t *testing.T) {
	var mu sync.Mutex
	var crosses int
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) {
		mu.Lock()
		crosses++
		mu.Unlock()
	})
	defer s.Close()
	inf.set(0.0, 0)
	pushAll(t, s, wakeword.FeatWindow+1)
	inf.set(0.9, 0)
	pushAll(t, s, 1)
	waitFor(t, "a one-frame crossing", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return crosses == 1
	})
}

// TestCrossingReportsCaptureTime: the time handed to the callback is when the
// crossing frame was captured, not when inference got round to it. The queue
// holds up to 640ms, and arbitration and the private-listening session both
// measure from capture.
func TestCrossingReportsCaptureTime(t *testing.T) {
	got := make(chan time.Time, 1)
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(_, _ float32, at time.Time) { got <- at })
	defer s.Close()
	inf.set(0.0, 0)
	pushAll(t, s, wakeword.FeatWindow+1)
	inf.set(0.9, 0)
	captured := time.Now().Add(-500 * time.Millisecond)
	b := make([]byte, wakeword.ChunkSamples*2)
	s.PushBytesAt(b, captured)
	select {
	case at := <-got:
		if !at.Equal(captured) {
			t.Fatalf("callback got %v, want the capture time %v", at, captured)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no crossing")
	}
}

// TestBargeWindowReportsThePeakAtTheBargeBar: the private path's replacement
// for the controller barge watcher's "peak=" line.
func TestBargeWindowReportsThePeakAtTheBargeBar(t *testing.T) {
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()
	playing := false
	var mu sync.Mutex
	s.SetBargeThreshold(0.25, func() bool { mu.Lock(); defer mu.Unlock(); return playing })
	inf.set(0.0, 0)
	pushAll(t, s, wakeword.FeatWindow+1)
	inf.set(0.9, 0) // idle: a wake, but not a barge-window frame
	pushAll(t, s, 1)
	if _, _, n := s.TakeBargeWindow(); n != 0 {
		t.Fatalf("counted %d barge frames while idle", n)
	}
	mu.Lock()
	playing = true
	mu.Unlock()
	inf.set(0.18, 0)
	pushAll(t, s, 3)
	peak, bar, n := s.TakeBargeWindow()
	if n != 3 || bar != 0.25 || peak < 0.17 || peak > 0.19 {
		t.Fatalf("window = peak %.3f bar %.2f frames %d", peak, bar, n)
	}
	if _, _, n := s.TakeBargeWindow(); n != 0 {
		t.Fatal("window not reset")
	}
}
