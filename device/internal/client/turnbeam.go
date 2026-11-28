package client

import (
	"encoding/binary"
	"log"
	"math"
	"os"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/turnbeam"
)

// The turn stage (internal/turnbeam) on a private-listening session: once a
// wake opens a session, the session carries the wake-anchored, cleaned beam,
// but only the one segment of it that holds the command (turnbeam.Segmenter,
// driven by Silero on the beam itself); everything else is silence, so HA's
// own end of speech fires as it would in a quiet room.
//
// The session's audio runs turnbeam.SegDelayS behind the microphones so that
// each sample's gain is final when it is sent: a 0.64s later start, and a
// turn that ends that much later. It can only ever send less than the plain
// path: the beam replaces the audio one for one, silence replaces the rest,
// and every device limit (ack timeout, max open, mute, link) is unchanged.
// The wake scorer keeps scoring the plain stream.
//
// Without Silero (model not installed) the stage stays off for the turn and
// the plain stream goes out, as before the stage existed.
//
// Off unless EM_TURNBEAM=on (bench, 2026-11-21). A config key with a
// capability is owed before this is anything but a bench option.
type turnBeam struct {
	st *turnbeam.Stage
	// newVAD returns a fresh speech detector for one turn, or nil.
	newVAD func() speechScorer

	mu      sync.Mutex // guards lockReq/crossAt (set by OpenListen)
	lockReq bool
	crossAt time.Time

	active bool
	vad    speechScorer
	seg    *turnbeam.Segmenter
	// fifo holds the cleaned beam not yet sent; fifo[0] is at time fifoT
	// (seconds around the crossing). scored counts how much of it, from
	// fifo[0], the detector has seen.
	fifo     []int16
	fifoT    float64
	scored   int
	sent     int // samples sent since the lock (observability)
	reported bool
}

const vadFrame = turnbeam.FloorFrame // the device's Silero step (ort.VADChunk)

// TurnBeamBenchOn, set only by bench builds (turnbeam_bench.go), switches
// the stage on without the env var. nil in production.
var TurnBeamBenchOn func() bool

func newTurnBeam(mics int) *turnBeam {
	on := os.Getenv("EM_TURNBEAM") == "on" || (TurnBeamBenchOn != nil && TurnBeamBenchOn())
	if !on || mics < 2 {
		return nil
	}
	log.Printf("[turnbeam] on: wake-anchored beam, post-filter and one speech segment per turn over %d mics", mics)
	return &turnBeam{st: turnbeam.New(turnbeam.Config{Mics: mics})}
}

// requestLock is called when a session opens for a wake whose crossing frame
// was captured at crossAt (any goroutine).
func (t *turnBeam) requestLock(crossAt time.Time) {
	if t == nil {
		return
	}
	t.mu.Lock()
	t.lockReq, t.crossAt = true, crossAt
	t.mu.Unlock()
}

func (t *turnBeam) takeLock() (time.Time, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	req := t.lockReq
	t.lockReq = false
	return t.crossAt, req
}

// step runs on the capture goroutine once per batch with every microphone's
// cancelled (or, while nothing plays, raw) audio at the overall mic gain. It
// returns what the session should carry instead of the plain stream for this
// batch (always len(plain) bytes), or nil to leave the plain stream alone.
func (t *turnBeam) step(chans [][]int16, plain []byte, sessionOpen bool) []byte {
	if t == nil || len(chans) == 0 {
		return nil
	}
	if crossAt, req := t.takeLock(); req && sessionOpen {
		t.lock(time.Since(crossAt).Seconds())
	}
	if t.active && !sessionOpen {
		t.st.Unlock()
		t.active = false
		log.Printf("[turnbeam] session closed after %.2fs", float64(t.sent)/16000)
	}
	y := t.st.Push(chans)
	if !t.active {
		return nil
	}
	t.fifo = append(t.fifo, y...)
	t.score()
	return t.emit(len(plain) / 2)
}

func (t *turnBeam) lock(leadS float64) {
	var vad speechScorer
	if t.newVAD != nil {
		vad = t.newVAD()
	}
	if vad == nil {
		log.Printf("[turnbeam] no speech detector: plain stream this turn")
		return
	}
	y, t0, err := t.st.Lock(leadS)
	if err != nil {
		log.Printf("[turnbeam] no beam this turn: %v", err)
		return
	}
	// Warm the detector (it is recurrent) on the replayed pre-wake beam, then
	// queue from OutLeadS before the crossing.
	k := int(math.Round((-turnbeam.OutLeadS - t0) * 16000))
	if k < 0 || k > len(y) {
		k = 0
	}
	buf := make([]float32, vadFrame)
	for i := k % vadFrame; i+vadFrame <= k; i += vadFrame {
		for j, v := range y[i : i+vadFrame] {
			buf[j] = float32(v) / 32767
		}
		vad.Prob(buf)
	}
	t.active, t.vad, t.reported, t.sent = true, vad, false, 0
	t.fifo, t.fifoT, t.scored = append(t.fifo[:0], y[k:]...), -turnbeam.OutLeadS, 0
	t.seg = turnbeam.NewSegmenter(float64(vadFrame)/16000, t.st.FloorDB(), -turnbeam.OutLeadS)
	log.Printf("[turnbeam] locked %.2fs after the crossing: ref ch%d, floor %.1f dB",
		leadS, t.st.RefMic(), t.st.FloorDB())
}

// score runs the detector over every whole frame of the FIFO it has not seen.
func (t *turnBeam) score() {
	buf := make([]float32, vadFrame)
	for t.scored+vadFrame <= len(t.fifo) {
		fr := t.fifo[t.scored : t.scored+vadFrame]
		for i, v := range fr {
			buf[i] = float32(v) / 32767
		}
		p, err := t.vad.Prob(buf)
		if err != nil {
			log.Printf("[turnbeam] speech detector: %v — treating the rest of the turn as silence", err)
			p = 0
			t.vad = zeroScorer{}
		}
		t.seg.Feed(float64(p), turnbeam.LevelDB(fr))
		t.scored += vadFrame
	}
}

// emit sends n samples: the oldest in the FIFO once they are SegDelayS old,
// weighted by the segment; silence until then.
func (t *turnBeam) emit(n int) []byte {
	out := make([]byte, 2*n)
	delay := int(turnbeam.SegDelayS * 16000)
	// A late lock starts with a backlog: drop what is
	// already known to be silence, so the session catches up with the room.
	drop := 0
	for len(t.fifo)-drop-n > delay && t.seg.FinalZero(t.fifoT+float64(drop)/16000) {
		drop++
	}
	if drop > 0 {
		t.fifo, t.fifoT, t.scored = t.fifo[drop:], t.fifoT+float64(drop)/16000, t.scored-drop
	}
	if len(t.fifo)-n < delay {
		return out
	}
	for i := 0; i < n; i++ {
		g := t.seg.Gain(t.fifoT + float64(i)/16000)
		if g > 0 {
			v := int16(math.Round(float64(t.fifo[i]) * g))
			binary.LittleEndian.PutUint16(out[2*i:], uint16(v))
		}
	}
	t.fifo = t.fifo[n:]
	t.fifoT += float64(n) / 16000
	t.scored -= n
	t.sent += n
	if cap(t.fifo) > 4*16000 && len(t.fifo) < 16000 {
		t.fifo = append([]int16(nil), t.fifo...)
	}
	if !t.reported && t.seg.Done() {
		if a, b, ok := t.seg.Bounds(); ok {
			if t.fifoT >= b {
				t.reported = true
				log.Printf("[turnbeam] command %+.2f..%+.2fs around the wake: silence from here", a, b)
			}
		} else {
			t.reported = true
			log.Printf("[turnbeam] no speech within %.0fs of the wake: nothing sent", turnbeam.StartS)
		}
	}
	return out
}

type zeroScorer struct{}

func (zeroScorer) Prob([]float32) (float32, error) { return 0, nil }

func bytesToS16(b []byte) []int16 {
	v := make([]int16, len(b)/2)
	for i := range v {
		v[i] = int16(binary.LittleEndian.Uint16(b[2*i:]))
	}
	return v
}

// turnBeamChans builds the stage's input from per-microphone S16 buffers
// extracted at extractGain, giving back the AEC headroom (makeup) so the
// beam's level matches the plain stream's. A nil entry in cancelled falls
// back to raw[ch] (a resting microphone: nothing is playing).
func turnBeamChans(raw, cancelled [][]byte, makeup float64) [][]int16 {
	out := make([][]int16, len(raw))
	for ch := range raw {
		src := raw[ch]
		if ch < len(cancelled) && cancelled[ch] != nil {
			src = cancelled[ch]
		}
		v := make([]int16, len(src)/2)
		for i := range v {
			s := math.Round(float64(int16(binary.LittleEndian.Uint16(src[2*i:]))) * makeup)
			v[i] = int16(math.Max(-32768, math.Min(32767, s)))
		}
		out[ch] = v
	}
	return out
}

func allChannels(n int) []int {
	c := make([]int, n)
	for i := range c {
		c[i] = i
	}
	return c
}
