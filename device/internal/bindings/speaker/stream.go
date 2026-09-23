package speaker

import (
	"errors"
	"sync"
	"sync/atomic"
	"time"
)

// errSpeakerDead is returned when the ALSA pump loop has exited, so a caller
// blocked on a full buffer unblocks with an error instead of hanging forever.
var errSpeakerDead = errors.New("speaker: ALSA loop has died")

// StreamStats is the per-stream delivery report, emitted once at EOS.
//
// Periods/Underruns answer "did it break". The rest answer "how close did it
// come, and which side was late" — the questions we could not answer on
// 2026-07-20 because every available metric measured something else (the
// controller's "Streaming took 0.0s" times a socket write, not delivery).
//
//   - MinDepth: fewest periods left in the buffer at any point mid-stream.
//     The headline margin number. 0 means it starved (an underrun); a stream
//     that only ever reached 2 was one hiccup away.
//   - PrimeWaitMs: first frame arriving → first frame played. Long means the
//     sender could not fill the prime buffer promptly.
//   - RecvSpanMs vs audio duration: delivery slower than realtime is the
//     definitive "the wire could not keep up" signal.
//   - MaxGapMs: the worst single stall in arrivals, which distinguishes a
//     uniformly slow link from a briefly stalled one.
type StreamStats struct {
	Periods     uint64 `json:"periods"`
	Underruns   uint64 `json:"underruns"`
	MinDepth    int    `json:"minDepth"`
	PrimeWaitMs int64  `json:"primeWaitMs"`
	RecvSpanMs  int64  `json:"recvSpanMs"`
	MaxGapMs    int64  `json:"maxGapMs"`
	BytesRecv   uint64 `json:"bytesRecv"`
}

// audioStream is one buffered playback stream: the voice/TTS plane or the
// music plane. Both need the same machinery — a prime gate, discard-until-EOS
// on flush, underrun accounting and delivery instrumentation — so it is
// extracted rather than duplicated. Every comment here records behaviour that
// was arrived at the hard way on the single-stream version; none of it is new.
//
// Untagged on purpose (pcm_speaker.go is `//go:build server`): this is the
// buffering state machine, and it is worth being able to test on the host.
type audioStream struct {
	ch     chan []byte
	deadCh <-chan struct{} // closed when the ALSA loop exits

	// eosPending is set by endStream (the WS reader received an EOS frame)
	// and consumed by the pump loop when the channel drains, so a drain at
	// the natural end of a stream is not misreported as an underrun.
	eosPending atomic.Bool

	// mu guards active and discarding as one unit. They used to be
	// independent atomics, but flush's check-active-then-arm and endStream's
	// clear-both are compound transitions: a barge-in flush racing a
	// stream's natural end (control and data ride separate WebSockets) could
	// observe active just before endStream cleared it and then arm
	// discarding just after endStream consumed it — leaving discard armed
	// with no EOS ever coming, silently swallowing the whole NEXT response
	// up to its EOS.
	mu sync.Mutex
	// active tracks whether a stream is mid-flight (set by pump, cleared by
	// endStream — both on the WS read goroutine). Read by flush to decide
	// whether to arm discarding.
	active bool
	// lastWireNs is when pump last received a period, for arriving().
	lastWireNs atomic.Int64
	// discarding, when set, makes pump drop incoming periods until the
	// stream's EOS arrives. Armed by flush when a stream is mid-flight:
	// draining the channel alone is not enough, because the rest of the
	// cancelled stream is typically already in flight in the TCP buffers of
	// both ends — the WS reader would refill the channel straight after the
	// drain and playback would carry on after a ~1.3s skip (observed
	// 2026-07-08: barge-in cut the LED but the TTS kept talking, and the
	// interrupting turn transcribed the device's own voice). The controller
	// always terminates a stream with an EOS, on the cancel path included,
	// so discard-until-EOS consumes exactly the remainder of the cancelled
	// stream no matter how much was buffered.
	discarding bool

	// ── per-stream delivery instrumentation ───────────────────────────────
	// Underruns are a rare binary event; these give the *margin* on every
	// stream, so a link that is merely close to starving is visible before it
	// audibly breaks (2026-07-20: added after underruns appeared with no
	// measurable cause — every metric we had timed the wrong thing).
	//
	// Written only by pump, which the WS read loop calls sequentially —
	// single writer, so a plain load/compare/store needs no lock. The pump
	// loop reads them once per stream at EOS. Cost on the audio hot path is
	// one time.Now() plus an integer compare per ~42ms period (~23/s);
	// nothing here allocates or logs.
	recvFirstNs  atomic.Int64
	recvLastNs   atomic.Int64
	recvMaxGapNs atomic.Int64
	recvBytes    atomic.Uint64

	// When the ALSA goroutine last took a period to play. Atomic because
	// the mic side reads it (playedWithin) to know whether sound is coming
	// out, which isActive cannot say once a stream has fully arrived.
	lastTakeNs atomic.Int64

	// ── consumption-side accounting, pump-loop-local by contract ──────────
	// Only the ALSA goroutine touches these, so they need no synchronisation.
	playing      bool // mid-stream from the consumer's point of view
	periods      uint64
	underruns    uint64
	minDepth     int   // -1 = nothing consumed yet this stream
	firstPumpNs  int64 // first period actually played this stream
}

func newAudioStream(depth int, deadCh <-chan struct{}) *audioStream {
	return &audioStream{
		ch:       make(chan []byte, depth),
		deadCh:   deadCh,
		minDepth: -1,
	}
}

// pump queues one period, or reports that it was swallowed by a flush.
//
// Blocks until the ALSA loop has consumed a slot (rate-limiting to playback
// speed), or returns false with an error if that loop has died — preventing
// an infinite block on a dead consumer.
func (s *audioStream) pump(period []byte, wireBytes int) (bool, error) {
	s.mu.Lock()
	if s.discarding {
		// Flushed stream — swallow the network-buffered remainder without
		// queueing it (see the discarding field for why draining the channel
		// alone cannot do this).
		s.mu.Unlock()
		return false, nil
	}
	newStream := !s.active
	s.active = true
	s.mu.Unlock()

	now := time.Now().UnixNano()
	s.lastWireNs.Store(now)
	if newStream {
		s.recvFirstNs.Store(now)
		s.recvMaxGapNs.Store(0)
		s.recvBytes.Store(0)
	} else if last := s.recvLastNs.Load(); last > 0 {
		if gap := now - last; gap > s.recvMaxGapNs.Load() {
			s.recvMaxGapNs.Store(gap)
		}
	}
	s.recvLastNs.Store(now)
	s.recvBytes.Add(uint64(wireBytes))

	select {
	case s.ch <- period:
		return true, nil
	case <-s.deadCh:
		return false, errSpeakerDead
	}
}

// endStream marks the EOS. Called from the WS read goroutine the instant the
// frame arrives, so by the time the pump loop drains the channel the flag is
// already set and the drain is not counted as an underrun.
//
// A stream that was being DISCARDED is the exception, and it is load-bearing.
// flush() already set eosPending and the drain that followed already consumed
// it, so setting it again here leaves it armed with no stream behind it —
// and the NEXT stream then reports itself complete at its first buffer dip.
// Measured after this was briefly wrong: a 2800ms response reported complete
// after 15 periods (640ms), which ended the turn, cleared the ring and
// released the duck while the device was still holding most of the audio.
// arriving reports whether a stream is still coming in over the wire: begun
// and not yet ended. A stream whose EOS was lost with the link would read as
// arriving forever, so it also needs a period within stale.
func (s *audioStream) arriving(now time.Time, stale time.Duration) bool {
	s.mu.Lock()
	active := s.active
	s.mu.Unlock()
	return active && now.UnixNano()-s.lastWireNs.Load() < stale.Nanoseconds()
}

func (s *audioStream) endStream() {
	s.mu.Lock()
	s.active = false
	wasDiscarding := s.discarding
	s.discarding = false
	s.mu.Unlock()
	if wasDiscarding {
		return
	}
	s.eosPending.Store(true)
}

// flush drops everything queued and, if a stream is mid-flight, arms discard
// so the remainder still in TCP buffers is swallowed rather than played.
//
// eosPending is set so the drain the pump loop is about to see is accounted
// as an end of stream rather than an underrun.
func (s *audioStream) flush() {
	s.mu.Lock()
	if s.active {
		s.discarding = true
	}
	s.mu.Unlock()
	s.eosPending.Store(true)
	for {
		select {
		case <-s.ch:
		default:
			return
		}
	}
}

// isActive reports whether a stream is mid-flight on the wire.
func (s *audioStream) isActive() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.active
}

// playedWithin reports whether this plane is audible: audio still arriving,
// queued to play, or a period played within hold.
//
// isActive alone clears at EOS, and a reply arrives far faster than it plays
// (recvSpan 112ms for a ~3s answer, measured 2026-09-22), so it was false for
// nearly all of the time the reply was actually heard.
func (s *audioStream) playedWithin(now time.Time, hold time.Duration) bool {
	if s.isActive() || len(s.ch) > 0 {
		return true
	}
	last := s.lastTakeNs.Load()
	return last > 0 && now.UnixNano()-last < int64(hold)
}

// ready reports whether the pump loop should take a period this round.
//
// The prime gate: while not yet playing, hold on silence until the buffer has
// primePeriods queued, or the stream's EOS is already in (a clip shorter than
// the prime — everything it will ever have is queued). This protects the
// opening seconds, when the sender's lead is still ~zero and a single WiFi
// stall used to stutter. Once playing, the gate stays out of the way and
// mid-stream drains are accounted as underruns instead.
func (s *audioStream) ready(prime int) bool {
	n := len(s.ch)
	if n == 0 {
		return false
	}
	if s.playing {
		return true
	}
	return n >= prime || s.eosPending.Load()
}

// take removes one period, updating the consumption-side accounting.
// Only called when ready() said so, and only from the ALSA goroutine.
func (s *audioStream) take() []byte {
	select {
	case period := <-s.ch:
		s.playing = true
		s.periods++
		s.lastTakeNs.Store(time.Now().UnixNano())
		// Buffer margin: occupancy remaining *after* taking this period.
		// len() on a channel is O(1); no allocation, no log.
		//
		// Sampled ONLY while the sender still has audio to send. The last
		// periods of every stream necessarily drain the buffer to zero, so
		// measuring across the tail made this read 0 on 100% of streams —
		// healthy ones included — which is how it shipped in v2.9.6 and told
		// us nothing (caught on first field data, 2026-07-20). eosPending is
		// set the instant the EOS arrives, so !eosPending means "more audio
		// is still expected" and a low buffer *there* is a real margin
		// warning.
		if !s.eosPending.Load() {
			if d := len(s.ch); s.minDepth < 0 || d < s.minDepth {
				s.minDepth = d
			}
		}
		if s.firstPumpNs == 0 {
			s.firstPumpNs = time.Now().UnixNano()
		}
		return period
	default:
		return nil
	}
}

// drained is called when the stream was playing and had nothing to give this
// round. It reports the completed stream's stats when the drain is an EOS,
// and counts an underrun when it is not.
//
// Returns the stats to report, or nil for an underrun.
func (s *audioStream) drained() *StreamStats {
	s.playing = false
	if !s.eosPending.Swap(false) {
		// Mid-stream drain: the WS sender fell behind realtime playback and
		// a silence gap is being injected — the audible stutter on weak WiFi
		// links. One count per drain event, not per silence period.
		s.underruns++
		return nil
	}
	// Natural end of stream, or a flush (which sets eosPending so its drain
	// is not miscounted as an underrun).
	st := &StreamStats{
		Periods:   s.periods,
		Underruns: s.underruns,
		MinDepth:  s.minDepth,
		MaxGapMs:  s.recvMaxGapNs.Load() / 1e6,
		BytesRecv: s.recvBytes.Load(),
	}
	firstRecv, lastRecv := s.recvFirstNs.Load(), s.recvLastNs.Load()
	if firstRecv > 0 && lastRecv > firstRecv {
		st.RecvSpanMs = (lastRecv - firstRecv) / 1e6
	}
	if firstRecv > 0 && s.firstPumpNs > firstRecv {
		st.PrimeWaitMs = (s.firstPumpNs - firstRecv) / 1e6
	}
	s.periods, s.underruns = 0, 0
	s.minDepth, s.firstPumpNs = -1, 0
	return st
}
