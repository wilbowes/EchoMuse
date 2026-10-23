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

func TestTheFirstPeriodOfAStreamIsReportedOnce(t *testing.T) {
	// #203: playback_started. A controller only ever sees audio ARRIVING, so
	// it estimates the start from the first period it put on the socket and
	// leads the speaker by up to the prime window. This is the moment audio
	// actually begins, and it must be reported once per STREAM: per period
	// would be a message every ~42ms of every response, and again after each
	// drain would claim a second start mid-reply.
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	if !s.ready(24) {
		t.Fatal("precondition: primed")
	}
	if _, first := s.take(); !first {
		t.Fatal("the first period of a stream is where playback starts")
	}
	for i := 0; i < 10; i++ {
		if _, first := s.take(); first {
			t.Fatal("playback start reported again mid-stream")
		}
	}
	// The sender fell behind: this drains to injected silence mid-response
	// and the loop picks up again — still one start.
	if s.drained() != nil {
		t.Fatal("precondition: this drain is an underrun, not the end of the stream")
	}
	if _, first := s.take(); first {
		t.Fatal("an underrun mid-response must not report a second start")
	}
}

func TestTheNextStreamStartsAgain(t *testing.T) {
	s, _ := newTestStream(64)
	pumpN(t, s, 30)
	s.ready(24)
	s.take()
	s.take()
	s.endStream()
	for s.ready(24) {
		s.take()
	}
	if st := s.drained(); st == nil {
		t.Fatal("precondition: EOS ends the stream")
	}
	pumpN(t, s, 30)
	if !s.ready(24) {
		t.Fatal("precondition: the next stream primes")
	}
	if _, first := s.take(); !first {
		t.Fatal("the next response never reported that it started playing")
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
