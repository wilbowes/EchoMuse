package client

import (
	"encoding/binary"
	"log"
	"math"
	"os"
	"sync/atomic"

	"github.com/wilbowes/EchoMuse/internal/turnbeam"
)

// The turn stage (internal/turnbeam) on a private-listening session: once a
// wake opens a session, the session's audio is the wake-anchored beam, and
// when the stage says the talker has stopped the rest of the session is
// silence, so HA's own end of speech fires as it would in a quiet room.
//
// It can only ever send less than the plain path: the beam replaces the
// audio one for one, silence replaces the rest, and every device limit
// (ack timeout, max open, mute, link) is unchanged. The wake scorer keeps
// scoring the plain stream; only what the gate sends changes.
//
// Off unless EM_TURNBEAM=on (bench, 2026-11-21). A config key with a
// capability is owed before this is anything but a bench option.
type turnBeam struct {
	st      *turnbeam.Stage
	lockReq atomic.Bool // set by OpenListen, taken by the capture goroutine
	active  bool        // locked for the open session
	quiet   bool        // the stage ended the talker: send silence
	frames  int
	// The plain stream one hop late, so the switch to the beam (which lags
	// its input by one STFT hop) crossfades between aligned audio. A hard cut
	// with a 16ms slip and a half-window first hop landed on the start of the
	// command and Parakeet heard "January" for "and what time" (41C8 live,
	// 2026-11-21; the same lock replayed without the splice was clean).
	plainTail []int16
	fade      int // samples of crossfade left
}

const (
	beamLagSamples = 256 + turnbeam.GateDelay // turnbeam's STFT hop and its gate delay
	crossfadeN     = 1280 // 80ms
)

// TurnBeamBenchOn, set only by bench builds (turnbeam_bench.go), switches
// the stage on without the env var. nil in production.
var TurnBeamBenchOn func() bool

func newTurnBeam(mics int) *turnBeam {
	on := os.Getenv("EM_TURNBEAM") == "on" || (TurnBeamBenchOn != nil && TurnBeamBenchOn())
	if !on || mics < 2 {
		return nil
	}
	log.Printf("[turnbeam] on: wake-anchored beam and endpoint over %d mics", mics)
	return &turnBeam{st: turnbeam.New(turnbeam.Config{Mics: mics})}
}

// requestLock is called when a session opens (any goroutine).
func (t *turnBeam) requestLock() {
	if t != nil {
		t.lockReq.Store(true)
	}
}

// step runs on the capture goroutine once per batch with every microphone's
// cancelled (or, while nothing plays, raw) audio at the overall mic gain. It
// returns what the session should carry instead of the plain stream for this
// batch, or nil to leave the plain stream alone.
func (t *turnBeam) step(chans [][]int16, plain []byte, sessionOpen bool) []byte {
	if t == nil || len(chans) == 0 {
		return nil
	}
	// The plain stream delayed by the beam's lag, kept every batch.
	delayed := make([]int16, len(plain)/2)
	if t.plainTail == nil {
		t.plainTail = make([]int16, beamLagSamples)
	}
	cat := append(append([]int16(nil), t.plainTail...), bytesToS16(plain)...)
	copy(delayed, cat[:len(delayed)])
	t.plainTail = append(t.plainTail[:0], cat[len(delayed):]...)
	if t.lockReq.Swap(false) && sessionOpen {
		if err := t.st.Lock(); err != nil {
			log.Printf("[turnbeam] no beam this turn: %v", err)
		} else {
			t.active, t.quiet, t.frames, t.fade = true, false, 0, crossfadeN
			log.Printf("[turnbeam] locked: ref ch%d, wake score %.2f", t.st.RefMic(), t.st.WakeScore())
		}
	}
	if t.active && !sessionOpen {
		t.st.Unlock()
		t.active = false
		log.Printf("[turnbeam] session closed after %.2fs", float64(t.frames)/16000)
	}
	y := t.st.Push(chans)
	if !t.active || len(y) == 0 {
		return nil
	}
	t.frames += len(y)
	if !t.quiet && t.st.Ended() {
		t.quiet = true
		log.Printf("[turnbeam] talker done %.2fs after the wake: silence from here (score %.2f, threshold %.2f)",
			float64(t.frames)/16000, t.st.Score(), t.st.Threshold())
	}
	out := make([]byte, 2*len(y))
	if t.quiet {
		return out
	}
	for i, v := range y {
		if t.fade > 0 && i < len(delayed) {
			g := 1 - float64(t.fade)/crossfadeN
			v = int16(math.Round(g*float64(v) + (1-g)*float64(delayed[i])))
			t.fade--
		}
		binary.LittleEndian.PutUint16(out[2*i:], uint16(v))
	}
	return out
}

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
