package speaker

import "math"

// Declarations shared by every speaker backend. They live here rather than in
// pcm_speaker.go so they are visible in builds without the `server` tag, which
// is what lets the profile-driven backend compile with CGO_ENABLED=0.

// audioChanDepth — the WS sender delivers at ~2× realtime, so its lead over
// playback grows ~1s per second played until it hits this cap. At 32
// (~1.3s) any WiFi stall longer than the accumulated lead drained the
// channel mid-stream (audible stutter on the far-AP device). 128 periods
// ≈ 5.5s (~1MB queued as stereo): most responses land on-device entirely
// within the first half of playback.
const audioChanDepth = 128

// primePeriods — playback holds on silence until this many periods are
// queued (or the stream's EOS has arrived, for clips shorter than the
// prime). Protects the opening seconds of playback, when the sender's
// lead is still ~zero and a single WiFi stall used to stutter. 24 periods
// ≈ 1s of audio ≈ ~0.5s added start latency at 2× realtime delivery
// (accepted trade, 2026-07-14). The controller's post-playback drain
// sleep allows for the delayed start (SPEAKER_PRIME_SECONDS).
const primePeriods = 24

// StreamStats is the per-stream delivery report, emitted once at EOS.
//
// Periods/Underruns answer "did it break". The rest answer "how close did
// it come, and which side was late" — the questions we could not answer on
// 2026-07-20 because every available metric measured something else (the
// controller's "Streaming took 0.0s" times a socket write, not delivery).
//
//   - MinDepth: fewest periods left in the buffer at any point mid-stream.
//     The headline margin number. 0 means it starved (an underrun); a
//     stream that only ever reached 2 was one hiccup away.
//   - PrimeWaitMs: first frame arriving → first frame played. Long means
//     the sender could not fill the prime buffer promptly.
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

// periodRMS computes the RMS level of a stereo S16LE period, normalized to
// 0..1 of int16 full-scale. Left channel only, every 4th frame — the wire
// is mono duplicated L=R and the LED meter needs ~2 significant digits at
// ~23Hz, so 512 of 2048 frames is plenty at a quarter of the cost. Runs on
// the ALSA pump goroutine: no allocation, integer accumulate.
func periodRMS(period []byte) float64 {
	if len(period) < 4 {
		return 0
	}
	var sum uint64
	n := 0
	// Stereo frame = 4 bytes (L16+R16); step 4 frames = 16 bytes.
	for i := 0; i+1 < len(period); i += 16 {
		s := int64(int16(uint16(period[i]) | uint16(period[i+1])<<8))
		sum += uint64(s * s)
		n++
	}
	if n == 0 {
		return 0
	}
	return math.Sqrt(float64(sum)/float64(n)) / 32768.0
}
