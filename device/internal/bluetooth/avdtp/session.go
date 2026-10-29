package avdtp

// The source's signalling session: one local SEP (a SOURCE on the Echo), one
// stream, either role. As initiator (Connect) it discovers the speaker's sink and configures
// and opens it; as acceptor (session_acp.go) it answers a speaker that does
// the same to us, which many do the moment the link comes up.
//
// Stream states are §9's: Idle → Configured → Open ⇄ Streaming, and Closing
// or Aborting while the media channel is released. The media channel is the
// caller's: it opens it after Open and reports MediaOpened/MediaClosed.

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"sync"
	"time"
)

// State is a stream endpoint state (§9).
type State uint8

const (
	StateIdle State = iota
	StateConfigured
	StateOpen
	StateStreaming
	StateClosing
	StateAborting
)

func (s State) String() string {
	return [...]string{"idle", "configured", "open", "streaming", "closing", "aborting"}[s]
}

// Signal carries outbound signalling packets. Send must be safe to call from
// more than one goroutine; the session serialises its own sends regardless.
type Signal interface {
	Send(pdu []byte) error
}

// DefaultTimeout is how long a command waits for its response: GAVDP 1.3
// §4.1.8's TGAVDP100 (0.5–3 s), at the top, because a speaker powering its
// amplifier up answers slowly and a response we gave up on costs a reconnect.
const DefaultTimeout = 3 * time.Second

// DefaultMTU is L2CAP's default signalling MTU when the caller states none.
const DefaultMTU = 672

// Both ends configuring at once is retried this many times (see configure).
const maxCollisionRetries = 4

var (
	ErrTimeout  = errors.New("avdtp: no response")
	ErrClosed   = errors.New("avdtp: signalling channel closed")
	ErrBadState = errors.New("avdtp: not allowed in this stream state")
	ErrNoSink   = errors.New("avdtp: peer has no free audio sink")

	errCollision = errors.New("avdtp: configuration collision")
)

// Config is a session's fixed parameters.
type Config struct {
	Signal Signal
	MTU    int // the peer's signalling MTU; 0 means DefaultMTU

	LocalSEID uint8 // our SEP; 0 means 1
	// Type is our SEP's end of the stream. The zero value is SEPSource, what
	// the Echo is; SEPSink exists so tests can wire two sessions together.
	Type  SEPType
	Codec Codec // our SEP's codec capability (package sbc's IE)

	// DelayReporting: we accept the sink's delay reports (AVDTP 1.3 §8.19).
	DelayReporting bool
	// PeerVersion is the peer's AVDTP version from SDP (0x0103 = 1.3); 0 is
	// unknown, and GET_ALL_CAPABILITIES is tried first.
	PeerVersion uint16

	// Select picks our configuration from a sink's codec capability
	// (initiator). An error skips that sink.
	Select func(peer Codec) (Codec, error)
	// Accept vets a configuration the peer proposes (acceptor). An error
	// refuses it; a *CodeError chooses the code. Neither callback may call
	// back into the session.
	Accept func(cfg Codec) error

	OnState func(State)
	OnDelay func(tenthsMs uint16) // a DELAYREPORT, in 1/10 ms

	Timeout time.Duration                   // 0 means DefaultTimeout
	Backoff func(attempt int) time.Duration // collision retry delay; nil = 100-600ms jittered
}

type pending struct {
	sig SignalID
	ch  chan Message
}

// Session is one AVDTP signalling channel.
type Session struct {
	cfg Config

	sendMu sync.Mutex

	mu      sync.Mutex
	reasm   Reassembler
	label   uint8
	pending map[uint8]*pending
	done    chan struct{}
	closed  bool
	changed chan struct{} // closed and replaced on every state change
	notes   []func()      // callbacks owed, run after mu is released

	state      State
	remoteSEID uint8
	config     *Codec
	delayOn    bool
	delay      uint16
	haveDelay  bool
	mediaOpen  bool

	settingConfig bool // our SET_CONFIGURATION is in flight
	collided      bool // and the peer's arrived meanwhile
}

// NewSession starts a session in Idle.
func NewSession(cfg Config) *Session {
	if cfg.MTU == 0 {
		cfg.MTU = DefaultMTU
	}
	if cfg.LocalSEID == 0 {
		cfg.LocalSEID = 1
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = DefaultTimeout
	}
	if cfg.Backoff == nil {
		cfg.Backoff = func(int) time.Duration {
			return 100*time.Millisecond + rand.N(500*time.Millisecond)
		}
	}
	return &Session{
		cfg:     cfg,
		pending: map[uint8]*pending{},
		done:    make(chan struct{}),
		changed: make(chan struct{}),
	}
}

// State is the stream's current state.
func (s *Session) State() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state
}

// Configuration is the codec configuration in force, if any.
func (s *Session) Configuration() (Codec, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.config == nil {
		return Codec{}, false
	}
	c := *s.config
	c.Info = clone(c.Info)
	return c, true
}

// Delay is the sink's last reported delay in 1/10 ms, if it has reported.
func (s *Session) Delay() (uint16, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.delay, s.haveDelay
}

// RemoteSEID is the peer's SEP in the stream, 0 when there is none.
func (s *Session) RemoteSEID() uint8 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.remoteSEID
}

// MediaOpened records that the caller's media channel is up.
func (s *Session) MediaOpened() {
	s.mu.Lock()
	s.mediaOpen = true
	s.mu.Unlock()
}

// MediaClosed records the media channel's release; a Closing or Aborting
// stream is then Idle.
func (s *Session) MediaClosed() {
	s.mu.Lock()
	s.mediaOpen = false
	if s.state == StateClosing || s.state == StateAborting {
		s.toIdleLocked()
	}
	s.unlockAndNotify()
}

// Disconnected ends the session: the signalling channel is gone. Commands in
// flight fail with ErrClosed and the stream is Idle.
func (s *Session) Disconnected() {
	s.mu.Lock()
	if !s.closed {
		s.closed = true
		close(s.done)
	}
	s.mediaOpen = false
	s.toIdleLocked()
	s.unlockAndNotify()
}

// Feed takes one inbound signalling packet.
func (s *Session) Feed(pdu []byte) {
	s.mu.Lock()
	m, ok, err := s.reasm.Feed(pdu)
	if err != nil || !ok {
		s.mu.Unlock()
		return
	}
	if m.Type != MsgCommand {
		if p := s.pending[m.Label]; p != nil && p.sig == m.Signal {
			delete(s.pending, m.Label)
			p.ch <- m // buffered
		}
		s.mu.Unlock()
		return
	}
	resp, reply := s.answer(m)
	notes := s.notes
	s.notes = nil
	s.mu.Unlock()
	// The answer goes out before the callbacks run, so a caller reacting to
	// Streaming starts sending media after the peer has its accept.
	if reply {
		_ = s.send(resp)
	}
	for _, f := range notes {
		f()
	}
}

func (s *Session) send(m Message) error {
	pkts, err := Fragment(m, s.cfg.MTU)
	if err != nil {
		return err
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	for _, p := range pkts {
		if err := s.cfg.Signal.Send(p); err != nil {
			return err
		}
	}
	return nil
}

// request sends a command and waits for its response. A reject comes back as
// a *RejectError.
func (s *Session) request(sig SignalID, params []byte) (Message, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return Message{}, ErrClosed
	}
	label := s.label
	for i := 0; i < 16 && s.pending[label] != nil; i++ {
		label = (label + 1) & 0x0F
	}
	s.label = (label + 1) & 0x0F
	p := &pending{sig, make(chan Message, 1)}
	s.pending[label] = p
	s.mu.Unlock()
	forget := func() {
		s.mu.Lock()
		if s.pending[label] == p {
			delete(s.pending, label)
		}
		s.mu.Unlock()
	}
	if err := s.send(Message{label, MsgCommand, sig, params}); err != nil {
		forget()
		return Message{}, err
	}
	t := time.NewTimer(s.cfg.Timeout)
	defer t.Stop()
	var m Message
	select {
	case m = <-p.ch:
	case <-t.C:
		forget()
		return Message{}, fmt.Errorf("%w to %v", ErrTimeout, sig)
	case <-s.done:
		return Message{}, ErrClosed
	}
	switch m.Type {
	case MsgAccept:
		return m, nil
	case MsgGeneralReject:
		return m, &RejectError{Signal: sig, General: true}
	}
	return m, parseReject(sig, m.Params)
}

// parseReject reads a reject's body, whose shape depends on the signal
// (§8.6-8.19).
func parseReject(sig SignalID, p []byte) error {
	e := &RejectError{Signal: sig}
	switch sig {
	case SigSetConfiguration, SigReconfigure:
		if len(p) >= 2 {
			e.Category, e.Code = Category(p[0]), ErrorCode(p[1])
		}
	case SigStart, SigSuspend:
		if len(p) >= 2 {
			e.SEID, e.Code = p[0]>>2, ErrorCode(p[1])
		}
	default:
		if len(p) >= 1 {
			e.Code = ErrorCode(p[0])
		}
	}
	return e
}

// setStateLocked moves the stream and owes the caller an OnState.
func (s *Session) setStateLocked(st State) {
	if s.state == st {
		return
	}
	s.state = st
	close(s.changed)
	s.changed = make(chan struct{})
	if f := s.cfg.OnState; f != nil {
		s.notes = append(s.notes, func() { f(st) })
	}
}

func (s *Session) toIdleLocked() {
	s.remoteSEID, s.config, s.delayOn, s.haveDelay = 0, nil, false, false
	s.setStateLocked(StateIdle)
}

// toReleasingLocked is CLOSE's and ABORT's end: Closing or Aborting while a
// media channel remains to be released, straight to Idle when there is none.
func (s *Session) toReleasingLocked(st State) {
	if s.mediaOpen {
		s.setStateLocked(st)
		return
	}
	s.toIdleLocked()
}

func (s *Session) unlockAndNotify() {
	notes := s.notes
	s.notes = nil
	s.mu.Unlock()
	for _, f := range notes {
		f()
	}
}

func seidByte(seid uint8) byte { return seid << 2 }
