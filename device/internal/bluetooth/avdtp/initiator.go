package avdtp

// The initiator's procedures (GAVDP 1.3 §4.1): Connect does discovery,
// capabilities, configuration and open; Start, Suspend, Close and Abort act
// on the stream whichever side set it up.

import (
	"errors"
	"time"
)

// Connect brings the stream to Open, configuring the peer's first free audio
// sink whose codec Select accepts. If the peer configures us first, Connect
// waits for the peer to open the stream instead. The caller then opens the
// media channel and calls MediaOpened.
func (s *Session) Connect() error {
	for attempt := 0; ; attempt++ {
		err := s.connectOnce()
		if !errors.Is(err, errCollision) || attempt >= maxCollisionRetries {
			return err
		}
		select {
		case <-time.After(s.cfg.Backoff(attempt)):
		case <-s.done:
			return ErrClosed
		}
	}
}

func (s *Session) connectOnce() error {
	switch s.State() {
	case StateOpen, StateStreaming:
		return nil
	case StateConfigured:
		return s.waitOpen()
	case StateClosing, StateAborting:
		return ErrBadState
	}
	m, err := s.request(SigDiscover, nil)
	if err != nil {
		return err
	}
	seps, err := parseSEPs(m.Params)
	if err != nil {
		return err
	}
	want := SEPSink
	if s.cfg.Type == SEPSink {
		want = SEPSource
	}
	lastErr := ErrNoSink
	for _, sep := range seps {
		if sep.Type != want || sep.Media != MediaAudio || sep.InUse {
			continue
		}
		caps, err := s.capabilities(sep.SEID)
		if err != nil {
			lastErr = err
			continue
		}
		if caps.Codec == nil || caps.Codec.Media != MediaAudio || s.cfg.Select == nil {
			continue
		}
		cfg, err := s.cfg.Select(*caps.Codec)
		if err != nil {
			lastErr = err
			continue
		}
		return s.configure(sep.SEID, caps, cfg)
	}
	return lastErr
}

// capabilities asks with GET_ALL_CAPABILITIES unless the peer predates 1.3,
// falling back to GET_CAPABILITIES when the peer does not know it (§8.8).
func (s *Session) capabilities(seid uint8) (Capabilities, error) {
	var re *RejectError
	if v := s.cfg.PeerVersion; v == 0 || v >= 0x0103 {
		m, err := s.request(SigGetAllCapabilities, []byte{seidByte(seid)})
		if err == nil {
			return ParseCapabilities(m.Params)
		}
		if !errors.As(err, &re) || !(re.General || re.Code == CodeNotSupportedCommand) {
			return Capabilities{}, err
		}
	}
	m, err := s.request(SigGetCapabilities, []byte{seidByte(seid)})
	if err != nil {
		return Capabilities{}, err
	}
	return ParseCapabilities(m.Params)
}

// configure sends SET_CONFIGURATION then OPEN.
//
// Collision: a speaker that configures us on connect may do so while ours is
// in flight. We accept its command (session_acp.go marks collided). If the
// speaker refused ours, the stream is the speaker's and opening it is its move
// as INT. If it accepted ours too, each side holds the other's configuration
// and neither knows which one the other's SEP now carries, so both are
// aborted and the attempt retried after a jittered backoff — the speaker, if
// it retries at all, is unlikely to land on the same instant twice.
func (s *Session) configure(seid uint8, peer Capabilities, codec Codec) error {
	delay := peer.DelayReporting && s.cfg.DelayReporting
	caps := Capabilities{MediaTransport: true, Codec: &codec, DelayReporting: delay}
	params := append([]byte{seidByte(seid), seidByte(s.cfg.LocalSEID)}, caps.Encode()...)

	s.mu.Lock()
	if s.state != StateIdle {
		s.mu.Unlock()
		return s.waitOpen()
	}
	s.settingConfig, s.collided = true, false
	s.mu.Unlock()

	_, err := s.request(SigSetConfiguration, params)

	s.mu.Lock()
	s.settingConfig = false
	if s.collided {
		if err == nil {
			s.mu.Unlock()
			_, _ = s.request(SigAbort, []byte{seidByte(seid)})
			s.mu.Lock()
			s.toIdleLocked()
			s.unlockAndNotify()
			return errCollision
		}
		s.mu.Unlock()
		return s.waitOpen()
	}
	if err != nil {
		s.mu.Unlock()
		return err
	}
	s.remoteSEID, s.config, s.delayOn = seid, &codec, delay
	s.setStateLocked(StateConfigured)
	s.unlockAndNotify()

	if _, err := s.request(SigOpen, []byte{seidByte(seid)}); err != nil {
		return err
	}
	s.mu.Lock()
	if s.state == StateConfigured {
		s.setStateLocked(StateOpen)
	}
	s.unlockAndNotify()
	return nil
}

// waitOpen waits for a stream the peer configured to be opened by the peer.
func (s *Session) waitOpen() error {
	t := time.NewTimer(2 * s.cfg.Timeout)
	defer t.Stop()
	for {
		s.mu.Lock()
		st, ch := s.state, s.changed
		s.mu.Unlock()
		switch st {
		case StateOpen, StateStreaming:
			return nil
		case StateIdle, StateClosing, StateAborting:
			return ErrBadState
		}
		select {
		case <-ch:
		case <-t.C:
			return ErrTimeout
		case <-s.done:
			return ErrClosed
		}
	}
}

// Start moves an Open stream to Streaming.
func (s *Session) Start() error {
	return s.transition(SigStart, StateOpen, StateStreaming)
}

// Suspend moves a Streaming stream back to Open.
func (s *Session) Suspend() error {
	return s.transition(SigSuspend, StateStreaming, StateOpen)
}

func (s *Session) transition(sig SignalID, from, to State) error {
	s.mu.Lock()
	st, r := s.state, s.remoteSEID
	s.mu.Unlock()
	if st == to {
		return nil
	}
	if st != from {
		return ErrBadState
	}
	if _, err := s.request(sig, []byte{seidByte(r)}); err != nil {
		return err
	}
	s.mu.Lock()
	if s.state == from {
		s.setStateLocked(to)
	}
	s.unlockAndNotify()
	return nil
}

// Close releases an Open or Streaming stream. Closing lasts until the caller
// reports MediaClosed.
func (s *Session) Close() error {
	s.mu.Lock()
	st, r := s.state, s.remoteSEID
	s.mu.Unlock()
	if st != StateOpen && st != StateStreaming {
		return ErrBadState
	}
	if _, err := s.request(SigClose, []byte{seidByte(r)}); err != nil {
		return err
	}
	s.mu.Lock()
	if s.state == StateOpen || s.state == StateStreaming {
		s.toReleasingLocked(StateClosing)
	}
	s.unlockAndNotify()
	return nil
}

// Abort tears the stream down from any state. The peer may only accept
// (§8.16), and a silent peer does not keep the stream alive: we abort locally
// whatever comes back.
func (s *Session) Abort() error {
	s.mu.Lock()
	st, r := s.state, s.remoteSEID
	s.mu.Unlock()
	if st == StateIdle {
		return nil
	}
	_, err := s.request(SigAbort, []byte{seidByte(r)})
	s.mu.Lock()
	if s.state != StateIdle {
		s.toReleasingLocked(StateAborting)
	}
	s.unlockAndNotify()
	return err
}

// ReportDelay sends a sink's DELAYREPORT (§8.19), in 1/10 ms. A source never
// sends one; it is here for a sink, and for tests.
func (s *Session) ReportDelay(tenthsMs uint16) error {
	s.mu.Lock()
	r, on := s.remoteSEID, s.delayOn
	s.mu.Unlock()
	if r == 0 || !on {
		return ErrBadState
	}
	_, err := s.request(SigDelayReport, []byte{seidByte(r), byte(tenthsMs >> 8), byte(tenthsMs)})
	return err
}
