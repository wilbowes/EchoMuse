// Package listen decides when the microphone may leave the device.
//
// Under private listening (owwOnDevice=on against a controller announcing
// listen_session) the wake stream is scored on the device and nothing is sent
// until the device's own wake word fires. A wake opens a numbered SESSION; the
// audio after the wake word is sent tagged with that number until the
// controller closes it, or until one of the device's own limits does. The
// spec is docs/listening.md.
//
// The limits live HERE, on the device, on purpose. The controller normally
// closes a session at end of speech, but a lost message, a crashed controller
// or a partition must never leave a device streaming: an unacknowledged wake
// closes after AckTimeout, and no session outlives MaxOpen whatever the
// controller says.
//
// Gate is pure state behind a mutex, driven with explicit timestamps, so every
// rule is testable without a microphone, a socket or a clock. It is touched
// from three goroutines — the mic loop (Push), the scorer (Open) and the
// control plane (Ack/Close) — and never blocks any of them.
package listen

import (
	"encoding/binary"
	"math"
	"sync"
	"time"
)

// FrameBytes is one 80ms frame of 16kHz mono S16_LE, the unit the wake scorer
// and the wire both use.
const FrameBytes = 2560

// frameDur is FrameBytes as time.
const frameDur = 80 * time.Millisecond

// Defaults, per docs/listening.md.
const (
	// DefaultRing is how much recent audio is held while closed. It only has
	// to cover the gap between the frame that crossed and the moment the
	// session opens — the scorer's queue, at most 640ms — so 2s is generous.
	DefaultRing = 2 * time.Second
	// DefaultAckTimeout closes a wake the controller never answered.
	DefaultAckTimeout = 3 * time.Second
	// DefaultMaxOpen bounds every session, acknowledged or not. Above the
	// controller's own 20s streaming cap so it never ends a healthy turn.
	DefaultMaxOpen = 30 * time.Second
)

// Reason says why a session ended. Sent to the controller on listen_end.
type Reason string

const (
	ReasonAckTimeout Reason = "ack_timeout"
	ReasonMaxOpen    Reason = "max_open"
	ReasonMuted      Reason = "muted"
	ReasonLink       Reason = "link"
	ReasonController Reason = "controller"
	ReasonStopped    Reason = "stopped"
)

// End reports a session the gate closed on its own.
type End struct {
	Session uint32
	Reason  Reason
}

type frame struct {
	pcm []byte
	at  time.Time
}

// Gate holds the ring, the open session and its deadlines.
type Gate struct {
	ring       time.Duration
	ackTimeout time.Duration
	maxOpen    time.Duration

	mu       sync.Mutex
	frames   []frame // ring, oldest first; only filled while closed
	open     bool
	session  uint32
	openedAt time.Time
	acked    bool
	backlog  [][]byte // ringed frames owed to the wire, sent on the next Push
	nextID   uint32
	floor    float64
}

// New builds a gate. Zero durations take the defaults.
func New(ring, ackTimeout, maxOpen time.Duration) *Gate {
	if ring <= 0 {
		ring = DefaultRing
	}
	if ackTimeout <= 0 {
		ackTimeout = DefaultAckTimeout
	}
	if maxOpen <= 0 {
		maxOpen = DefaultMaxOpen
	}
	return &Gate{ring: ring, ackTimeout: ackTimeout, maxOpen: maxOpen}
}

// Push takes one frame captured at `at` and returns what may be sent: nothing
// while closed, otherwise any backlog followed by this frame, all under
// `session`. If a deadline ended the session, end is set and nothing is sent.
//
// The caller must not reuse pcm: a closed gate keeps it in the ring.
func (g *Gate) Push(pcm []byte, at time.Time) (out [][]byte, session uint32, end *End) {
	g.mu.Lock()
	defer g.mu.Unlock()

	if g.open {
		switch {
		case at.Sub(g.openedAt) >= g.maxOpen:
			end = g.closeLocked(ReasonMaxOpen)
		case !g.acked && at.Sub(g.openedAt) >= g.ackTimeout:
			end = g.closeLocked(ReasonAckTimeout)
		}
	}
	if !g.open {
		g.noteFloorLocked(pcm)
		g.frames = append(g.frames, frame{pcm: pcm, at: at})
		if max := int(g.ring / frameDur); len(g.frames) > max {
			// Shift rather than reslice so the backing array does not grow
			// without bound over a device's uptime.
			n := copy(g.frames, g.frames[len(g.frames)-max:])
			for i := n; i < len(g.frames); i++ {
				g.frames[i] = frame{}
			}
			g.frames = g.frames[:n]
		}
		return nil, 0, end
	}
	out = append(g.backlog, pcm)
	g.backlog = nil
	return out, g.session, nil
}

// Open starts a session for a wake whose last frame was captured at crossAt.
// The session begins with every ringed frame captured AFTER it, so the command
// follows the wake word with no gap and no repeat.
//
// ok is false when a session is already open: a wake word spoken inside an
// open session is just words, and must not start a second one.
func (g *Gate) Open(crossAt, now time.Time) (session uint32, ok bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.open {
		return g.session, false
	}
	g.nextID++
	if g.nextID == 0 { // wrapped; 0 means "no session" on the wire
		g.nextID = 1
	}
	g.startLocked(g.nextID, now)
	for _, f := range g.frames {
		if f.at.After(crossAt) {
			g.backlog = append(g.backlog, f.pcm)
		}
	}
	g.frames = g.frames[:0]
	return g.session, true
}

// Ack records that the controller took the wake, stopping the ack clock.
func (g *Gate) Ack(session uint32) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.open || g.session != session {
		return false
	}
	g.acked = true
	return true
}

// Close ends `session` if it is the open one. A close for any other id is a
// late message about a session already gone, and is ignored.
func (g *Gate) Close(session uint32) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.open || g.session != session {
		return false
	}
	g.closeLocked(ReasonController)
	return true
}

// CloseAny ends whatever session is open, for mute, link loss and stream stop.
func (g *Gate) CloseAny(r Reason) *End {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.open {
		return nil
	}
	return g.closeLocked(r)
}

// IsOpen reports the open session, if any.
func (g *Gate) IsOpen() (uint32, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.session, g.open
}

// Floor is the room's noise floor as RMS in 0..1, tracked while closed. The
// controller used to measure it from the continuous stream; a private device
// sends none, so it reports its own with each wake.
func (g *Gate) Floor() float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.floor
}

func (g *Gate) startLocked(session uint32, now time.Time) {
	g.open = true
	g.session = session
	g.openedAt = now
	g.acked = false
	g.backlog = nil
}

func (g *Gate) closeLocked(r Reason) *End {
	e := &End{Session: g.session, Reason: r}
	g.open = false
	g.acked = false
	g.backlog = nil
	return e
}

// noteFloorLocked is the controller's asymmetric EWMA, moved here unchanged:
// quick to follow a drop (0.3), slow to rise (0.008, ~10s at 12.5 frames/s) so
// speech does not drag it up.
func (g *Gate) noteFloorLocked(pcm []byte) {
	rms := frameRMS(pcm)
	switch {
	case g.floor == 0:
		g.floor = rms
	case rms < g.floor:
		g.floor += 0.3 * (rms - g.floor)
	default:
		g.floor += 0.008 * (rms - g.floor)
	}
}

func frameRMS(pcm []byte) float64 {
	n := len(pcm) / 2
	if n == 0 {
		return 0
	}
	var sum float64
	for i := 0; i < n; i++ {
		f := float64(int16(binary.LittleEndian.Uint16(pcm[2*i:]))) / 32768.0
		sum += f * f
	}
	return math.Sqrt(sum / float64(n))
}
