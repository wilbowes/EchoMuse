package speaker

import (
	"testing"
	"time"
)

// The buffering state machine, testable on the host because audioStream is
// untagged. pcm_speaker.go can only be built on the device, which is why
// every one of these behaviours used to be verifiable only by listening.

func newTestStream(depth int) (*audioStream, chan struct{}) {
	dead := make(chan struct{})
	return newAudioStream(depth, dead), dead
}

func pumpN(t *testing.T, s *audioStream, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if _, err := s.pump([]byte{byte(i)}, 1); err != nil {
			t.Fatalf("pump %d: %v", i, err)
		}
	}
}

func TestPrimeGateHoldsUntilEnoughIsQueued(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 5)
	if s.ready(24) {
		t.Fatal("should hold on silence until primed")
	}
	pumpN(t, s, 19)
	if !s.ready(24) {
		t.Fatal("should start once primed")
	}
}

func TestAShortClipDoesNotWaitForThePrime(t *testing.T) {
	// Everything it will ever have is already queued; waiting for 24 periods
	// that are never coming would mean it never played.
	s, _ := newTestStream(64)
	pumpN(t, s, 3)
	s.endStream()
	if !s.ready(24) {
		t.Fatal("an ended short clip must play without priming")
	}
}

func TestOncePlayingTheGateStaysOutOfTheWay(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 24)
	s.ready(24)
	s.take()
	if !s.ready(24) {
		t.Fatal("mid-stream playback must not re-prime on every period")
	}
}

func TestANaturalEndReportsStatsRatherThanAnUnderrun(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 2)
	s.endStream()
	s.ready(24)
	s.take()
	s.take()
	st := s.drained()
	if st == nil {
		t.Fatal("a drain after EOS is the end of the stream, not an underrun")
	}
	if st.Periods != 2 {
		t.Fatalf("expected 2 periods, got %d", st.Periods)
	}
	if s.underruns != 0 {
		t.Fatalf("expected no underruns, got %d", s.underruns)
	}
}

func TestAMidStreamDrainIsAnUnderrunNotAnEnding(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 24)
	s.ready(24)
	for i := 0; i < 24; i++ {
		s.take()
	}
	if st := s.drained(); st != nil {
		t.Fatal("the sender falling behind is an underrun, not a completed stream")
	}
	if s.underruns != 1 {
		t.Fatalf("expected 1 underrun, got %d", s.underruns)
	}
}

func TestAFlushedStreamDoesNotLeaveEosArmedForTheNextOne(t *testing.T) {
	// The regression that shipped on 2026-08-03. flush() sets eosPending and
	// the drain that follows consumes it; endStream must NOT set it again, or
	// it stays armed with no stream behind it.
	//
	// Measured symptom: a 2800ms response reported complete after 15 periods
	// (640ms), which ended the turn, cleared the LED ring and released the
	// music duck while the device still held most of the audio.
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	s.ready(24)
	s.take()

	s.flush()     // barge-in
	s.drained()   // the pump loop sees the emptied channel
	s.endStream() // the cancelled stream's EOS finally arrives

	if s.eosPending.Load() {
		t.Fatal("eosPending must not survive a flushed stream — the next " +
			"stream would report itself complete at its first buffer dip")
	}
}

func TestTheStreamAfterAFlushBehavesNormally(t *testing.T) {
	// The consequence of the bug above, from the next stream's point of view.
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	s.ready(24)
	s.take()
	s.flush()
	s.drained()
	s.endStream()

	// A fresh response arrives.
	pumpN(t, s, 56)
	if !s.ready(24) {
		t.Fatal("the next stream should prime and play")
	}
	for i := 0; i < 15; i++ {
		s.take()
	}
	// It still has 41 periods queued; nothing should claim it is finished.
	if !s.ready(24) {
		t.Fatal("still has audio — must keep playing")
	}
	if s.eosPending.Load() {
		t.Fatal("no EOS has arrived for this stream")
	}
}

func TestFlushSwallowsTheRestOfTheStreamUntilItsEos(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 10)
	s.flush()
	// The rest of the cancelled response is already in TCP buffers and keeps
	// arriving; it must not refill the channel.
	queued, err := s.pump([]byte{9}, 1)
	if err != nil {
		t.Fatal(err)
	}
	if queued {
		t.Fatal("post-flush periods must be discarded, not queued")
	}
	s.endStream()
	queued, _ = s.pump([]byte{9}, 1)
	if !queued {
		t.Fatal("the EOS disarms the discard; the next stream must play")
	}
}

func TestMinDepthIgnoresTheTailOfAStream(t *testing.T) {
	// Every stream necessarily drains to zero at its end, so sampling across
	// the tail made this read 0 on 100% of streams, healthy ones included.
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	s.endStream() // EOS in: everything from here is the tail
	s.ready(24)
	for i := 0; i < 30; i++ {
		s.take()
	}
	if s.minDepth != -1 {
		t.Fatalf("tail periods must not be sampled, got minDepth=%d", s.minDepth)
	}
}

func TestAudibleUntilTheLastPeriodHasPlayed(t *testing.T) {
	// A reply arrives far faster than it plays (recvSpan 112ms for ~3s,
	// 2026-09-22). isActive clears at EOS, which dropped the barge bar for
	// nearly the whole reply; playedWithin must hold until the queue is
	// played out, and for the hold after it.
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	s.endStream()
	now := time.Now()
	if s.isActive() {
		t.Fatal("precondition: EOS clears isActive")
	}
	if !s.playedWithin(now, 0) {
		t.Fatal("a fully arrived, unplayed reply must count as audible")
	}
	for s.ready(24) {
		s.take()
	}
	if !s.playedWithin(time.Now(), 2*time.Second) {
		t.Fatal("audible within the hold after the last period")
	}
	if s.playedWithin(time.Now().Add(3*time.Second), 2*time.Second) {
		t.Fatal("must stop counting once the hold has passed")
	}
}

func TestNothingPlayedIsNotAudible(t *testing.T) {
	s, _ := newTestStream(64)
	if s.playedWithin(time.Now(), 2*time.Second) {
		t.Fatal("a plane that never played must not hold the barge bar down")
	}
}

// A stream whose EOS was lost with the link never calls endStream, so it
// stays active. Once its queue has played out it must stop counting as
// audible, or the device keeps its wake bar at bargeInThreshold until it is
// rebooted (2026-10-23: a Dot's link dropped mid-song; four days of false
// wakes at 0.25 with nothing playing).
func TestALostEosDoesNotHoldTheBargeBarDown(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	for s.ready(24) {
		s.take()
	}
	if !s.isActive() {
		t.Fatal("precondition: no EOS arrived, so the stream is still active")
	}
	if !s.playedWithin(time.Now(), time.Second) {
		t.Fatal("audible within the hold after the last period")
	}
	if s.playedWithin(time.Now().Add(3*time.Second), time.Second) {
		t.Fatal("no period and nothing queued for longer than eosLostAfter: not audible")
	}
}

// The data link closed mid-stream: its EOS can never arrive, so the stream
// must end with the link rather than stay active until some later EOS.
func TestALinkLostMidStreamEndsTheStream(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 10)
	if !s.abandonIdleSince(time.Now()) {
		t.Fatal("a stream last fed before the link closed must be abandoned")
	}
	if s.isActive() {
		t.Fatal("an abandoned stream is not active")
	}
	if len(s.ch) != 10 {
		t.Fatalf("what arrived before the drop still plays out: %d queued, want 10", len(s.ch))
	}
}

// A replacement connection may already be feeding the plane when the old
// one's cleanup runs. That stream is the new link's and must survive.
func TestAStreamFedAfterTheLinkClosedIsNotAbandoned(t *testing.T) {
	s, _ := newTestStream(64)
	linkLost := time.Now().Add(-time.Second)
	pumpN(t, s, 3)
	if s.abandonIdleSince(linkLost) {
		t.Fatal("a period arrived after linkLost: this stream rides the replacement")
	}
	if !s.isActive() {
		t.Fatal("the replacement's stream must stay active")
	}
}

// 2026-10-27, reproduced on a Dot: the link dropped mid-song, then a stop
// flushed the music plane, arming discard on a stream whose EOS was gone.
// Every later song was swallowed. Abandoning at the drop means the flush
// finds nothing active; abandoning after it must still disarm the discard.
func TestAStopAfterALostLinkDoesNotSwallowTheNextSong(t *testing.T) {
	for _, stopFirst := range []bool{false, true} {
		s, _ := newTestStream(64)
		pumpN(t, s, 10)
		if stopFirst {
			s.flush()
			s.abandonIdleSince(time.Now())
		} else {
			s.abandonIdleSince(time.Now())
			s.flush()
		}
		s.drained()
		queued, err := s.pump([]byte{9}, 1)
		if err != nil {
			t.Fatal(err)
		}
		if !queued {
			t.Fatalf("stopFirst=%v: the next song was discarded", stopFirst)
		}
		if s.eosPending.Load() {
			t.Fatalf("stopFirst=%v: eosPending left armed for the next song", stopFirst)
		}
	}
}

// arriving is what the BLE scanner yields for: a reply still coming in over
// the wire. It must end at the EOS, and must also end when the periods stop
// without one — an EOS lost with the link would otherwise hold the scan off
// until the next reply, which is a proxy gone quiet for no reason.
func TestArrivingSpansTheWireAndNeverOutlivesIt(t *testing.T) {
	s, _ := newTestStream(64)
	now := time.Now()
	if s.arriving(now, 2*time.Second) {
		t.Fatal("nothing has arrived yet")
	}
	pumpN(t, s, 3)
	if !s.arriving(time.Now(), 2*time.Second) {
		t.Fatal("a stream mid-flight is arriving")
	}
	if s.arriving(time.Now().Add(3*time.Second), 2*time.Second) {
		t.Fatal("no period for longer than stale: the EOS was lost, stop yielding")
	}
	s.endStream()
	if s.arriving(time.Now(), 2*time.Second) {
		t.Fatal("the EOS ends it, even while the buffer plays on")
	}
}
