// Package cue gives wake-word acknowledgement to devices without an LED ring.
//
// On biscuit the 12-LED ring is the feedback: it turns green the instant the
// controller starts a voice turn, so you know you were heard before you start
// speaking. A device with a screen and no ring has nothing, and without any
// acknowledgement you end up talking over a device that is not listening.
//
// Rather than invent a new control message, this hangs off the signal the
// device already receives: the controller's explicit "this frame is the
// listening ring" hint: and renders it with whatever the hardware does have:
// a rising chime when listening starts, a falling one when it ends, and the
// screen backlight held bright in between.
package cue

import (
	"log"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/profile"
)

// Player is the slice of the speaker the chime needs. PlaySystemSound keeps
// the cue off the reply stream, so a chime cannot disturb a reply's
// end-of-stream accounting or interleave with its audio.
type Player interface {
	PlaySystemSound(mono []byte)
}

// Cue renders wake acknowledgement.
type Cue struct {
	prof *profile.Profile
	spk  Player

	// Tones are pre-rendered once at construction: they never change, and
	// synthesising one on the wake path would add latency to the moment that
	// has to feel instant.
	chimeUp   []byte
	chimeDown []byte

	mu            sync.Mutex
	listening     bool
	prevBacklight string
	// safety cancels the stuck-screen timer when a turn ends normally.
	safety *time.Timer
	// turn distinguishes successive turns so a late safety-net timer from an
	// earlier turn cannot restore the screen during a later one.
	turn uint64
}

const (
	// maxTurn bounds how long the screen may stay held bright without an
	// end-of-listening signal.
	maxTurn = 60 * time.Second

	// flushGrace is how long the rising chime waits for the controller's
	// barge-in speaker flush to land before queueing itself.
	flushGrace = 150 * time.Millisecond
)

// New builds a Cue for the profile. It returns nil when the profile does not
// enable a wake cue, which is the case for a device whose LED ring already
// provides the acknowledgement.
func New(p *profile.Profile, spk Player) *Cue {
	if !p.WakeCue.Enabled {
		return nil
	}
	c := &Cue{prof: p, spk: spk}
	if p.WakeCue.ChimeMs > 0 {
		c.chimeUp = renderChime(p, true)
		c.chimeDown = renderChime(p, false)
	}
	return c
}

// Start acknowledges the beginning of a voice turn: a rising chime, and the
// screen held bright for as long as the device is listening.
//
// The screen is held rather than blipped because a flash only answers "did it
// hear me", not "is it still listening": and the second question is the one
// you need answered while deciding whether to keep talking.
//
// Safe to call from the control-plane goroutine: it returns immediately and
// does the work in the background, so a slow sysfs write or a full audio queue
// can never stall LED handling.
func (c *Cue) Start() {
	if c == nil {
		return
	}
	c.mu.Lock()
	if c.listening {
		c.mu.Unlock()
		return // already acknowledged this turn
	}
	c.listening = true
	c.turn++

	// The backlight moves inline, under the lock, rather than from a
	// goroutine. Two unordered goroutines meant a Stop could restore the
	// screen before its matching Start had raised it, leaving it bright with
	// nothing left to put it back, and a back-to-back turn could save the
	// already-raised value as the level to return to.
	c.raiseBacklightLocked()

	if c.safety != nil {
		c.safety.Stop()
	}
	// If the end of listening never arrives (a controller that drops mid-turn,
	// a turn that times out) the screen would stay bright indefinitely.
	c.safety = time.AfterFunc(maxTurn, c.expire)
	c.mu.Unlock()

	go func() {
		// The controller flushes the speaker at wake detection so a reply in
		// progress is cut for barge-in. That flush and this chime are queued at
		// the same moment, and when the flush wins it discards the chime, which
		// is exactly the intermittent missing rising tone this had. Let the
		// flush land first. The falling chime needs no such wait: nothing
		// flushes at end of listening.
		time.Sleep(flushGrace)
		c.playTone(c.chimeUp)
	}()
}

// expire restores the screen when a turn ends without an end-of-listening
// signal.
func (c *Cue) expire() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.listening {
		return
	}
	c.listening = false
	c.safety = nil
	log.Printf("cue: no end-of-turn after %v, restoring screen", maxTurn)
	c.restoreBacklightLocked()
}

// Stop acknowledges the end of listening: a falling chime, and the screen back
// to whatever it was.
func (c *Cue) Stop() {
	if c == nil {
		return
	}
	c.mu.Lock()
	if !c.listening {
		c.mu.Unlock()
		return
	}
	c.listening = false
	if c.safety != nil {
		c.safety.Stop()
		c.safety = nil
	}
	c.restoreBacklightLocked()
	c.mu.Unlock()

	go c.playTone(c.chimeDown)
}

// raiseBacklight stores the current brightness and goes to full. The previous
// value is read at Start rather than cached at construction so this works
// whatever the user or Android left behind, including a screen that had
// dimmed or switched off.
func (c *Cue) raiseBacklightLocked() {
	path := c.prof.WakeCue.BacklightPath
	if path == "" {
		return
	}
	prev, err := os.ReadFile(path)
	if err != nil {
		log.Printf("cue: read backlight: %v", err)
		return
	}
	c.prevBacklight = strings.TrimSpace(string(prev))

	max := c.prof.WakeCue.BacklightMax
	if max <= 0 {
		max = 255
	}
	if err := os.WriteFile(path, []byte(strconv.Itoa(max)), 0644); err != nil {
		log.Printf("cue: write backlight: %v", err)
	}
}

// restoreBacklight puts back the brightness captured at Start.
func (c *Cue) restoreBacklightLocked() {
	path := c.prof.WakeCue.BacklightPath
	if path == "" {
		return
	}
	prev := c.prevBacklight
	if prev == "" {
		return
	}
	if err := os.WriteFile(path, []byte(prev), 0644); err != nil {
		log.Printf("cue: restore backlight: %v", err)
	}
}

// playTone hands the pre-rendered tone to the speaker's system-sound path. It
// still goes through the same mono-to-stereo duplication and AEC tap as a
// reply, so the chime lands in the echo reference rather than being a signal
// the canceller never saw, without touching the reply stream's lifecycle.
func (c *Cue) playTone(tone []byte) {
	if len(tone) == 0 || c.spk == nil {
		return
	}
	c.spk.PlaySystemSound(tone)
}

// renderChime synthesises the acknowledgement tone as mono S16 at the
// speaker's rate.
//
// It is a two-note pair rather than a single beep: two short notes cut through
// room noise better than one longer one, and the interval direction is what
// distinguishes start from end. Both notes get a raised-
// cosine envelope: a bare sine switched on and off clicks, and on this device
// the click is loud enough to be the thing you notice instead of the tone.
func renderChime(p *profile.Profile, rising bool) []byte {
	rate := float64(p.Speaker.SampleRate)
	noteMs := p.WakeCue.ChimeMs / 2
	if noteMs <= 0 {
		noteMs = 60
	}
	amp := p.WakeCue.ChimeAmplitude
	if amp <= 0 || amp > 1 {
		amp = 0.18
	}
	base := p.WakeCue.ChimeHz
	if base <= 0 {
		base = 880
	}

	// Rising for "listening", falling for "done". Direction is what carries
	// the meaning: the ear reads a rising interval as opening and a falling
	// one as closing, so the two are distinguishable without being told.
	notes := []float64{base, base * 1.25}
	if !rising {
		notes = []float64{base * 1.25, base}
	}
	out := make([]byte, 0, int(rate*float64(noteMs*len(notes))/1000)*2)

	for _, hz := range notes {
		n := int(rate * float64(noteMs) / 1000)
		for i := 0; i < n; i++ {
			// Raised-cosine window over the whole note: zero at both ends.
			env := 0.5 * (1 - math.Cos(2*math.Pi*float64(i)/float64(n-1)))
			v := math.Sin(2*math.Pi*hz*float64(i)/rate) * env * amp
			s := int16(v * math.MaxInt16)
			out = append(out, byte(s), byte(s>>8))
		}
	}
	return out
}
