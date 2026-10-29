package avrcp

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// Response timers (AVRCP 1.6 §6.2 and §4.6.1): a target answers UNIT INFO,
// SUBUNIT INFO and PASS THROUGH within TRCP, a CONTROL within TMTC (or sends
// INTERIM and then answers within TMTP), and a STATUS or NOTIFY's first
// response within TMTP.
const (
	TRCP = 100 * time.Millisecond
	TMTC = 200 * time.Millisecond
	TMTP = 1000 * time.Millisecond

	// DefaultSlack is added to each timer while we wait: the spec times are
	// the target's, and the answer still has to cross a radio shared with WiFi.
	DefaultSlack = time.Second
)

var (
	ErrNotImplemented = errors.New("avrcp: not implemented by the peer")
	ErrNoProfile      = errors.New("avrcp: peer has no AVRCP (IPID)")
	ErrTimeout        = errors.New("avrcp: no response")
	ErrClosed         = errors.New("avrcp: session closed")
	ErrBusy           = errors.New("avrcp: all 16 transaction labels in use")
	ErrRange          = errors.New("avrcp: absolute volume above 0x7F")
	ErrFragmented     = errors.New("avrcp: fragmented response")
	ErrUnexpected     = errors.New("avrcp: unexpected response")
	ErrRejected       = errors.New("avrcp: rejected by the peer")
)

// StatusError is a REJECTED vendor-dependent response and its AVRCP status.
type StatusError struct {
	PDU    uint8
	Status uint8
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("avrcp: pdu 0x%02x rejected, status 0x%02x", e.PDU, e.Status)
}

// Channel carries AVCTP packets to the peer: the AVRCP control L2CAP channel
// (PSM 0x17). One call is one L2CAP SDU.
type Channel interface {
	Send(pdu []byte) error
}

// Config is a session's MTU, wait slack and callbacks. Callbacks run on the
// goroutine calling Receive and must not call back into a blocking request.
type Config struct {
	// MTU is the channel's outgoing MTU; below MinMTU means MinMTU.
	MTU int
	// Slack is added to the spec timers when waiting; 0 means DefaultSlack.
	Slack time.Duration
	// OnPassThrough reports a button on the peer (we are its category 1
	// target), pressed and then released.
	OnPassThrough func(op Op, pressed bool)
	// OnVolume reports the peer's absolute volume after it changed, once
	// WatchVolume has registered for it.
	OnVolume func(abs uint8)
}

// Session is one AVRCP connection, both roles at once.
type Session struct {
	ch     Channel
	cfg    Config
	sendMu sync.Mutex // keeps one message's fragments together

	mu        sync.Mutex
	reasm     reassembler
	nextLabel uint8
	pending   map[uint8]*pending
	closed    bool

	// Target state: the play status we report and the label of the peer's
	// PLAYBACK_STATUS_CHANGED registration (-1 when none is outstanding).
	playStatus PlayStatus
	playLabel  int

	// Controller state: whether to keep the volume registration alive, and
	// the last volume the peer reported (-1 unknown).
	volWatch bool
	lastVol  int

	dropped atomic.Uint64
}

type result struct {
	f   frame
	err error
}

// pending is one of our commands awaiting its response. A notification
// stays pending past its INTERIM until the CHANGED that ends it.
type pending struct {
	opcode uint8
	pdu    uint8 // vendor PDU ID, for vendor-dependent commands
	notify bool
	event  Event       // the event a RegisterNotification asked for
	resp   chan result // nil once delivered, or for a background registration
	timer  *time.Timer
	gen    int // bumped on every timer reset, so a stale expiry is ignored
}

// NewSession starts a session that sends on ch.
func NewSession(ch Channel, cfg Config) *Session {
	if cfg.MTU < MinMTU {
		cfg.MTU = MinMTU
	}
	if cfg.Slack <= 0 {
		cfg.Slack = DefaultSlack
	}
	return &Session{ch: ch, cfg: cfg, pending: map[uint8]*pending{}, playLabel: -1, lastVol: -1}
}

// Dropped counts inbound packets discarded as malformed, unplaceable or
// answering nothing we asked.
func (s *Session) Dropped() uint64 { return s.dropped.Load() }

// Close fails every request in flight; later sends return ErrClosed.
func (s *Session) Close() {
	s.mu.Lock()
	s.closed = true
	ps := s.pending
	s.pending = map[uint8]*pending{}
	s.volWatch = false
	s.playLabel = -1
	s.mu.Unlock()
	for _, p := range ps {
		p.timer.Stop()
		if p.resp != nil {
			p.resp <- result{err: ErrClosed}
		}
	}
}

func (s *Session) send(m message) error {
	pkts, err := fragment(m, s.cfg.MTU)
	if err != nil {
		return err
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.mu.Lock()
	closed := s.closed
	s.mu.Unlock()
	if closed {
		return ErrClosed
	}
	for _, p := range pkts {
		if err := s.ch.Send(p); err != nil {
			return err
		}
	}
	return nil
}

// Receive takes one inbound AVCTP packet (one L2CAP SDU). Call it from one
// goroutine. A malformed packet is dropped and returned as an error; the
// session carries on.
func (s *Session) Receive(pkt []byte) error {
	s.mu.Lock()
	m, ok, err := s.reasm.feed(pkt)
	s.mu.Unlock()
	if err != nil {
		s.dropped.Add(1)
		return err
	}
	if !ok {
		return nil
	}
	if m.response {
		s.response(m)
		return nil
	}
	if m.pid != PID {
		// AVCTP 1.4 §6.1.1: a command for a profile we do not have is answered
		// with the IPID bit and no payload.
		return s.send(message{label: m.label, response: true, ipid: true, pid: m.pid})
	}
	f, err := parseFrame(m.payload)
	if err != nil {
		s.dropped.Add(1)
		if len(m.payload) >= 3 {
			// Unreadable but addressable: NOT IMPLEMENTED echoes it (AV/C §8).
			r := append([]byte{RespNotImplemented}, m.payload[1:]...)
			return s.send(message{label: m.label, response: true, pid: PID, payload: r})
		}
		return err
	}
	resp, after := s.target(f, m.label)
	if err := s.send(message{label: m.label, response: true, pid: PID, payload: resp.encode()}); err != nil {
		return err
	}
	if after != nil {
		after()
	}
	return nil
}

func (s *Session) timeoutFor(f frame) time.Duration {
	if f.opcode != opVendor {
		return TRCP + s.cfg.Slack
	}
	if f.code == CtypeControl {
		return TMTC + s.cfg.Slack
	}
	return TMTP + s.cfg.Slack
}

// start sends a command and registers it as pending. wait is false for a
// background registration nobody blocks on.
func (s *Session) start(f frame, notify, wait bool) (chan result, error) {
	p := &pending{opcode: f.opcode, notify: notify}
	if f.opcode == opVendor {
		if _, v, err := parseVendor(f.operands); err == nil {
			p.pdu = v.id
			if v.id == pduRegisterNotification && len(v.params) > 0 {
				p.event = Event(v.params[0])
			}
		}
	}
	var resp chan result
	if wait {
		// Kept here: response clears p.resp once it delivers.
		resp = make(chan result, 1)
		p.resp = resp
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, ErrClosed
	}
	label, ok := s.allocLabel()
	if !ok {
		s.mu.Unlock()
		return nil, ErrBusy
	}
	s.pending[label] = p
	s.armLocked(label, p, s.timeoutFor(f))
	s.mu.Unlock()
	if err := s.send(message{label: label, pid: PID, payload: f.encode()}); err != nil {
		s.mu.Lock()
		if s.pending[label] == p {
			delete(s.pending, label)
			p.timer.Stop()
		}
		s.mu.Unlock()
		return nil, err
	}
	return resp, nil
}

// request sends a command and waits for its final response (or, for a
// notification, its INTERIM).
func (s *Session) request(f frame, notify bool) (frame, error) {
	resp, err := s.start(f, notify, true)
	if err != nil {
		return frame{}, err
	}
	r := <-resp
	return r.f, r.err
}

// allocLabel takes the next transaction label not in use; s.mu held.
func (s *Session) allocLabel() (uint8, bool) {
	for i := uint8(0); i < 16; i++ {
		l := (s.nextLabel + i) & 0x0F
		if _, busy := s.pending[l]; !busy {
			s.nextLabel = (l + 1) & 0x0F
			return l, true
		}
	}
	return 0, false
}

func (s *Session) armLocked(label uint8, p *pending, d time.Duration) {
	p.gen++
	gen := p.gen
	if p.timer != nil {
		p.timer.Stop()
	}
	p.timer = time.AfterFunc(d, func() { s.expire(label, p, gen) })
}

func (s *Session) expire(label uint8, p *pending, gen int) {
	s.mu.Lock()
	if s.pending[label] != p || p.gen != gen {
		s.mu.Unlock()
		return
	}
	delete(s.pending, label)
	resp := p.resp
	p.resp = nil
	if resp == nil && p.notify && p.event == EventVolumeChanged {
		// A background re-registration nobody answered: the watch has lapsed.
		s.volWatch = false
	}
	s.mu.Unlock()
	if resp != nil {
		resp <- result{err: ErrTimeout}
	}
}

// response routes an inbound response to the command that asked for it.
func (s *Session) response(m message) {
	s.mu.Lock()
	p := s.pending[m.label]
	if p == nil {
		s.mu.Unlock()
		s.dropped.Add(1)
		return
	}
	var r result
	if m.ipid {
		r.err = ErrNoProfile
	} else if f, err := parseFrame(m.payload); err != nil {
		r.err = err
	} else if f.opcode != p.opcode {
		r.err = ErrUnexpected
	} else {
		r.f = f
	}
	if p.notify && r.err == nil {
		s.mu.Unlock()
		s.notification(m.label, p, r.f)
		return
	}
	if r.err == nil && r.f.code == RespInterim {
		// A CONTROL the target could not finish within TMTC: it now has TMTP.
		s.armLocked(m.label, p, TMTP+s.cfg.Slack)
		s.mu.Unlock()
		return
	}
	delete(s.pending, m.label)
	p.timer.Stop()
	resp := p.resp
	p.resp = nil
	s.mu.Unlock()
	if resp != nil {
		resp <- r
	}
}
