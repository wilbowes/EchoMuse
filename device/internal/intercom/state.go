// Package intercom is the device side of EchoMuse intercom: a real-time
// voice call between two EchoMuse devices, peer to peer, with the
// controller as switchboard only (docs/intercom.md).
//
// This package owns the call's state machine, the peer link, the
// resampler and the audio pumps. The LED ring is driven by cmd/server.go,
// which also owns the button: the package reports state changes through
// callbacks and server.go turns them into light and control messages.
// The low-prime speaker plane arrives in Phase 3; until then the peer
// audio rides the voice plane with its ~1s prime.
package intercom

import (
	"errors"
	"sync"
)

// States. Suspended is the Phase 3 state: this end is running a nested
// Home Assistant turn while the call is paused. It is part of the machine
// from the start so the state table never has to be relearned.
const (
	StateIdle      = "idle"
	StateRinging   = "ringing"
	StateCalling   = "calling"
	StateInCall    = "in_call"
	StateSuspended = "suspended"
)

// Roles. The caller waits for the callee to answer; the callee rings
// and dials the caller when its button is pressed.
const (
	RoleCaller = "caller"
	RoleCallee = "callee"
)

// Session identifies one call. The token is issued by the controller and
// is what the callee checks a dialler against; PeerAddr is the other
// end's intercom listener (ip:port), put in the offer by the controller
// so the callee can dial it on answer without another round trip.
type Session struct {
	ID        string
	PeerID    string
	PeerLabel string
	Token     string
	Role      string
	PeerAddr  string
}

var (
	// ErrNotIdle is an offer for a new call while one is already running.
	// The caller must end the current call first.
	ErrNotIdle = errors.New("intercom: already in a call")
	// ErrNoSession is an end/suspend/resume with no call to act on.
	ErrNoSession = errors.New("intercom: no active call")
	// ErrNotInCall is a suspend for a call that is not flowing audio.
	ErrNotInCall = errors.New("intercom: call is not in progress")
	// ErrNotSuspended is a resume for a call that is not suspended.
	ErrNotSuspended = errors.New("intercom: call is not suspended")
)

// Machine is the call state machine. The zero value is not usable;
// use NewMachine. All methods are safe for concurrent use.
type Machine struct {
	mu      sync.Mutex
	state   string
	session Session
}

// NewMachine returns a machine in the idle state.
func NewMachine() *Machine {
	return &Machine{state: StateIdle}
}

// State returns the current state: idle, in_call or suspended.
func (m *Machine) State() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.state
}

// Current returns the active session and true, or the zero session and
// false when idle.
func (m *Machine) Current() (Session, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state == StateIdle {
		return Session{}, false
	}
	return m.session, true
}

// Offer enters a call attempt. From idle it starts the session: the
// callee rings and waits for its button, the caller waits for the
// callee to answer. Re-offering the same session is a no-op, because
// the controller fans every offer out to both ends and a retried offer
// must not look like a second call. Offering a different session while
// one runs is ErrNotIdle.
func (m *Machine) Offer(sess Session) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state == StateIdle {
		m.session = sess
		if sess.Role == RoleCaller {
			m.state = StateCalling
		} else {
			m.state = StateRinging
		}
		return nil
	}
	if m.session.ID == sess.ID {
		return nil
	}
	return ErrNotIdle
}

// Connect moves a ringing or calling attempt into a flowing call, once
// the peer link is up. Connecting a call that is already flowing is a
// no-op; connecting anything else is an error.
func (m *Machine) Connect() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	switch m.state {
	case StateRinging, StateCalling:
		m.state = StateInCall
		return nil
	case StateInCall:
		return nil
	case StateIdle:
		return ErrNoSession
	default:
		return ErrNotInCall
	}
}

// End leaves the call and returns the session that was active. Ending
// when idle is a no-op returning the zero session, so a duplicated or
// late intercom_end can never wedge the machine — teardown is
// idempotent by construction, and the controller sends the end to both
// ends for exactly that reason.
func (m *Machine) End() (Session, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state == StateIdle {
		return Session{}, nil
	}
	sess := m.session
	m.session = Session{}
	m.state = StateIdle
	return sess, nil
}

// Suspend pauses a flowing call for a nested Home Assistant turn (Phase
// 3). The peer link stays up; only this end stops sending and playing.
func (m *Machine) Suspend() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state == StateIdle {
		return ErrNoSession
	}
	if m.state != StateInCall {
		return ErrNotInCall
	}
	m.state = StateSuspended
	return nil
}

// Resume returns a suspended call to flowing audio.
func (m *Machine) Resume() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state == StateIdle {
		return ErrNoSession
	}
	if m.state != StateSuspended {
		return ErrNotSuspended
	}
	m.state = StateInCall
	return nil
}
