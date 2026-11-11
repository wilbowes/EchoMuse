// Call is one intercom call on this device: the peer listener, the
// dialler, and the two audio pumps. It is created on intercom_offer and
// torn down on intercom_end, on a button hangup, on a ring timeout, or
// when the peer link drops — whichever comes first.
//
// Audio in: the DataClient's post-AEC mono mic frames, via the
// OnProcessedMic hook. Tapping after AEC is the whole point: the
// canceller is one shared, stateful Speex instance fed by the speaker's
// echo reference, so a second consumer calling Process on the raw mic
// would double-feed its echo state. The hook is the same mono STT
// hears, which is also what makes the Phase 2 hardware test the AEC
// test: the peer hears exactly what the wake-word scorer hears.
//
// Audio out: peer 48 kHz PCM straight into the voice plane
// (PumpPeriod). Phase 2 accepts the voice plane's ~1s prime — the
// low-prime intercom plane is Phase 3 — so this version is
// walkie-talkie grade by design, and what it proves is the lifecycle:
// offer, ring, answer, flow, hangup, and that the AEC holds under
// continuous two-way speech.
//
// The Call owns the machine transitions from Connect onward and the
// firewall rule for its listener port. server.go owns the LED ring and
// the control messages, and drives them from OnConnected/OnEnded.
package intercom

import (
	"log"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/firewall"
)

// DefaultRingTimeout bounds a call attempt: if the callee has not
// answered (or the caller has not been dialled) by then, the attempt
// ends itself. 30s matches the listen session's maxOpen.
const DefaultRingTimeout = 30 * time.Second

// WriteTimeout bounds one send to the peer. A peer that stops reading
// must not wedge the sender goroutine forever.
const WriteTimeout = 5 * time.Second

// Speaker is the sink for peer audio: the voice plane. PcmSpeaker
// satisfies it. PumpPeriod blocks at playback speed, which paces the
// receiver to what the speaker can actually play.
type Speaker interface {
	PumpPeriod([]byte) error
	Flush()
}

// MicTap is the source of this end's audio: the DataClient's
// post-AEC mono frames. The callback must not block — the mic
// goroutine calls it — and must copy the frame if it keeps it.
type MicTap interface {
	OnProcessedMic(func([]byte))
}

// Call is a single intercom call. The zero value is not usable;
// use NewCall. All methods are safe for concurrent use.
type Call struct {
	deviceID string
	sess     Session
	machine  *Machine
	mic      MicTap
	spk      Speaker

	// Port overrides the listener port (DefaultPort). Tests use an
	// ephemeral port by setting this to 0 and reading ListenerAddr.
	Port int
	// RingTimeout overrides DefaultRingTimeout. Tests shorten it.
	RingTimeout time.Duration

	// OnConnected runs when the peer link is up and audio is flowing.
	OnConnected func()
	// OnEnded runs when the call ends on its own — peer gone, ring
	// timeout, dial failure — with the reason. It does NOT run for
	// Stop, which is the caller tearing the call down itself.
	OnEnded func(reason string)

	mu        sync.Mutex
	listener  net.Listener
	conn      net.Conn
	done      chan struct{}
	ended     bool
	dialling  bool
	timer     *time.Timer
	micCh     chan []byte
	resampler Resampler
	drops     uint64
}

// NewCall returns a call for the offered session. mic may be nil — a
// speaker-only device still takes the call, it just sends no audio.
func NewCall(deviceID string, sess Session, machine *Machine, mic MicTap, spk Speaker) *Call {
	return &Call{
		deviceID: deviceID,
		sess:     sess,
		machine:  machine,
		mic:      mic,
		spk:      spk,
	}
}

// Start opens the peer listener and the firewall rule and starts the
// ring timer. Both ends listen from the offer, so either can be
// dialled; the callee dials on answer.
func (c *Call) Start() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	port := c.Port
	if port == 0 {
		port = DefaultPort
	}
	ln, err := net.Listen("tcp", ":"+strconv.Itoa(port))
	if err != nil {
		return err
	}
	c.listener = ln
	c.done = make(chan struct{})
	c.micCh = make(chan []byte, 8)
	// Open before advertising readiness: the peer dials as soon as it
	// answers, and may not retry a timeout. Logged, not fatal: FireOS
	// has no filter to open.
	if ferr := firewall.Open(port); ferr != nil {
		log.Printf("[intercom] could not open port %d: %v", port, ferr)
	}
	timeout := c.RingTimeout
	if timeout == 0 {
		timeout = DefaultRingTimeout
	}
	c.timer = time.AfterFunc(timeout, func() { c.end("no_answer") })
	go c.acceptLoop(ln)
	return nil
}

// ListenerAddr returns the listener's address once Start has run, for
// tests (port 0) and for logging. Empty before Start.
func (c *Call) ListenerAddr() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.listener == nil {
		return ""
	}
	return c.listener.Addr().String()
}

// Answer dials the peer. Only the callee answers — the caller waits to
// be dialled — so a call in any other state is a no-op.
func (c *Call) Answer() {
	c.mu.Lock()
	if c.machine.State() != StateRinging || c.dialling {
		c.mu.Unlock()
		return
	}
	c.dialling = true
	c.mu.Unlock()
	go func() {
		conn, err := dial(c.sess.PeerAddr, handshake{
			Session: c.sess.ID,
			Token:   c.sess.Token,
			Caller:  c.deviceID,
		})
		if err != nil {
			log.Printf("[intercom] dial %s: %v", c.sess.PeerAddr, err)
			c.end("dial_failed")
			return
		}
		c.connected(conn)
	}()
}

// Stop tears the call down without invoking OnEnded: the caller is
// ending it itself (intercom_end, button cancel, controller link drop)
// and reports the state itself.
func (c *Call) Stop() {
	c.teardown("", false)
}

// end tears the call down and reports it via OnEnded.
func (c *Call) end(reason string) {
	c.teardown(reason, true)
}

func (c *Call) teardown(reason string, notify bool) {
	c.mu.Lock()
	if c.ended {
		c.mu.Unlock()
		return
	}
	c.ended = true
	if c.timer != nil {
		c.timer.Stop()
	}
	if c.mic != nil {
		c.mic.OnProcessedMic(nil)
	}
	if c.conn != nil {
		c.conn.Close()
	}
	if c.listener != nil {
		c.listener.Close()
	}
	port := c.Port
	if port == 0 {
		port = DefaultPort
	}
	done := c.done
	if done != nil {
		close(done)
	}
	spk := c.spk
	cb := c.OnEnded
	// The machine leaves the call too, whatever state it was in —
	// teardown is idempotent by construction.
	c.machine.End()
	c.mu.Unlock()

	firewall.Close(port)
	if spk != nil {
		spk.Flush()
	}
	if notify && cb != nil {
		cb(reason)
	}
}

// acceptLoop serves the listener until Stop or the call ends. A failed
// handshake closes that connection and keeps listening: a stray scan
// must not end a call, and the ring timer still bounds the attempt.
func (c *Call) acceptLoop(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return // listener closed by teardown
		}
		st := c.machine.State()
		waiting := st == StateRinging || st == StateCalling
		if _, err := acceptHandshake(conn, c.sess, waiting); err != nil {
			log.Printf("[intercom] rejected peer connection: %v", err)
			conn.Close()
			continue
		}
		c.connected(conn)
		return // one call, one peer — stop accepting
	}
}

// connected brings a peer link up: machine into in_call, mic hook on,
// pumps running, OnConnected fired. The connection is closed and the
// link ignored if the call already ended or is already connected.
func (c *Call) connected(conn net.Conn) {
	c.mu.Lock()
	if c.ended || c.conn != nil {
		c.mu.Unlock()
		conn.Close()
		return
	}
	if err := c.machine.Connect(); err != nil {
		c.mu.Unlock()
		log.Printf("[intercom] peer link up but machine would not connect: %v", err)
		conn.Close()
		c.end("connect_failed")
		return
	}
	c.conn = conn
	if c.timer != nil {
		c.timer.Stop()
	}
	if c.mic != nil {
		c.mic.OnProcessedMic(c.onMicFrame)
	}
	done := c.done
	cb := c.OnConnected
	c.mu.Unlock()

	go c.sendLoop(conn, done)
	go c.recvLoop(conn, done)
	if cb != nil {
		cb()
	}
}

// onMicFrame is the DataClient's post-AEC mono callback. It copies the
// frame (the mic loop owns the slice) and queues it for the sender,
// dropping when the peer is too slow to keep up rather than blocking
// the mic goroutine.
func (c *Call) onMicFrame(mono []byte) {
	cp := append([]byte(nil), mono...)
	c.mu.Lock()
	ch := c.micCh
	done := c.done
	c.mu.Unlock()
	if ch == nil || done == nil {
		return
	}
	select {
	case ch <- cp:
	case <-done:
	default:
		c.mu.Lock()
		c.drops++
		if c.drops == 1 || c.drops%64 == 0 {
			log.Printf("[intercom] mic queue full — frame dropped (drops=%d)", c.drops)
		}
		c.mu.Unlock()
	}
}

// sendLoop resamples queued mic frames to 48 kHz and writes them to the
// peer until the call ends or the peer stops reading.
func (c *Call) sendLoop(conn net.Conn, done chan struct{}) {
	for {
		select {
		case <-done:
			return
		case frame := <-c.micCh:
			up := c.resampler.Write(frame)
			if len(up) == 0 {
				continue
			}
			if err := conn.SetWriteDeadline(time.Now().Add(WriteTimeout)); err != nil {
				c.end("peer_disconnected")
				return
			}
			if _, err := conn.Write(up); err != nil {
				c.end("peer_disconnected")
				return
			}
		}
	}
}

// recvLoop reads peer PCM and pumps it to the voice plane until the
// peer closes or the call ends. PumpPeriod paces the loop to playback
// speed, so a fast peer is throttled by TCP backpressure, not by a
// growing buffer.
func (c *Call) recvLoop(conn net.Conn, done chan struct{}) {
	buf := make([]byte, 4096)
	for {
		select {
		case <-done:
			return
		default:
		}
		n, err := conn.Read(buf)
		if n > 0 {
			if perr := c.spk.PumpPeriod(buf[:n]); perr != nil {
				log.Printf("[intercom] speaker error: %v", perr)
				c.end("speaker_error")
				return
			}
		}
		if err != nil {
			c.end("peer_disconnected")
			return
		}
	}
}
