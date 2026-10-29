package avdtp

// The acceptor: answers to the peer's commands against our one SEP.
// Called by Feed with s.mu held; returns the response and whether to send one
// (ABORT for an SEP that is not ours gets none).

import (
	"encoding/binary"
	"errors"
)

func (s *Session) answer(m Message) (Message, bool) {
	accept := func(p []byte) (Message, bool) { return Message{m.Label, MsgAccept, m.Signal, p}, true }
	reject := func(p ...byte) (Message, bool) { return Message{m.Label, MsgReject, m.Signal, p}, true }
	local := s.cfg.LocalSEID
	p := m.Params
	// one reads a single ACP SEID; ok is false for a wrong length.
	one := func() (uint8, bool) {
		if len(p) != 1 {
			return 0, false
		}
		return p[0] >> 2, true
	}

	switch m.Signal {
	case SigDiscover:
		if len(p) != 0 {
			return reject(byte(CodeBadLength))
		}
		return accept(encodeSEPs([]SEPInfo{{local, s.state != StateIdle, MediaAudio, s.cfg.Type}}))

	case SigGetCapabilities, SigGetAllCapabilities:
		seid, ok := one()
		if !ok {
			return reject(byte(CodeBadLength))
		}
		if seid != local {
			return reject(byte(CodeBadACPSEID))
		}
		codec := s.cfg.Codec
		caps := Capabilities{MediaTransport: true, Codec: &codec}
		// Delay reporting is a 1.3 category: GET_CAPABILITIES returns only
		// the basic ones, so a 1.2 peer never sees what it cannot parse (§8.8).
		caps.DelayReporting = m.Signal == SigGetAllCapabilities && s.cfg.DelayReporting
		return accept(caps.Encode())

	case SigSetConfiguration:
		return s.answerSetConfiguration(m)

	case SigGetConfiguration:
		seid, ok := one()
		if !ok {
			return reject(byte(CodeBadLength))
		}
		if seid != local {
			return reject(byte(CodeBadACPSEID))
		}
		if s.config == nil {
			return reject(byte(CodeSEPNotInUse))
		}
		caps := Capabilities{MediaTransport: true, Codec: s.config, DelayReporting: s.delayOn}
		return accept(caps.Encode())

	case SigReconfigure:
		return s.answerReconfigure(m)

	case SigOpen:
		seid, ok := one()
		if !ok {
			return reject(byte(CodeBadLength))
		}
		if seid != local {
			return reject(byte(CodeBadACPSEID))
		}
		if s.state != StateConfigured {
			return reject(byte(CodeBadState))
		}
		s.setStateLocked(StateOpen)
		return accept(nil)

	case SigStart, SigSuspend:
		// A list of SEIDs; the reject names the first that fails (§8.13).
		if len(p) == 0 {
			return reject(0, byte(CodeBadLength))
		}
		from, to := StateOpen, StateStreaming
		if m.Signal == SigSuspend {
			from, to = StateStreaming, StateOpen
		}
		for _, b := range p {
			if b>>2 != local {
				return reject(b&0xFC, byte(CodeBadACPSEID))
			}
			if s.state != from {
				return reject(b&0xFC, byte(CodeBadState))
			}
		}
		s.setStateLocked(to)
		return accept(nil)

	case SigClose:
		seid, ok := one()
		if !ok {
			return reject(byte(CodeBadLength))
		}
		if seid != local {
			return reject(byte(CodeBadACPSEID))
		}
		if s.state != StateOpen && s.state != StateStreaming {
			return reject(byte(CodeBadState))
		}
		s.toReleasingLocked(StateClosing)
		return accept(nil)

	case SigAbort:
		// ABORT has no reject (§8.16): one that is not ours goes unanswered.
		if seid, ok := one(); !ok || seid != local {
			return Message{}, false
		}
		if s.state != StateIdle {
			s.toReleasingLocked(StateAborting)
		}
		return accept(nil)

	case SigSecurityControl:
		if len(p) < 1 {
			return reject(byte(CodeBadLength))
		}
		if p[0]>>2 != local {
			return reject(byte(CodeBadACPSEID))
		}
		// We never configure content protection, so there is nothing to
		// exchange in any state.
		return reject(byte(CodeBadState))

	case SigDelayReport:
		if len(p) != 3 {
			return reject(byte(CodeBadLength))
		}
		if p[0]>>2 != local {
			return reject(byte(CodeBadACPSEID))
		}
		if s.state != StateConfigured && s.state != StateOpen && s.state != StateStreaming {
			return reject(byte(CodeBadState))
		}
		d := binary.BigEndian.Uint16(p[1:3])
		s.delay, s.haveDelay = d, true
		if f := s.cfg.OnDelay; f != nil {
			s.notes = append(s.notes, func() { f(d) })
		}
		return accept(nil)
	}
	// A signal we do not know: General Reject (AVDTP 1.3 §8.18).
	return Message{m.Label, MsgGeneralReject, m.Signal, nil}, true
}

func (s *Session) answerSetConfiguration(m Message) (Message, bool) {
	reject := func(cat Category, code ErrorCode) (Message, bool) {
		return Message{m.Label, MsgReject, m.Signal, []byte{byte(cat), byte(code)}}, true
	}
	p := m.Params
	if len(p) < 2 {
		return reject(0, CodeBadLength)
	}
	acp, intS := p[0]>>2, p[1]>>2
	if acp != s.cfg.LocalSEID {
		return reject(0, CodeBadACPSEID)
	}
	if !validSEID(intS) {
		return reject(0, CodeBadPayloadFormat)
	}
	if s.state != StateIdle {
		return reject(0, CodeSEPInUse)
	}
	caps, err := ParseCapabilities(p[2:])
	if err != nil {
		var ce *CapError
		errors.As(err, &ce)
		return reject(ce.Category, ce.Code)
	}
	if cat, code := s.vet(caps, false); code != 0 {
		return reject(cat, code)
	}
	if !caps.MediaTransport {
		return reject(CatMediaTransport, CodeInvalidCapabilities)
	}
	if code := s.accepts(*caps.Codec); code != 0 {
		return reject(CatMediaCodec, code)
	}
	if s.settingConfig {
		s.collided = true
	}
	s.remoteSEID, s.config, s.delayOn = intS, caps.Codec, caps.DelayReporting
	s.setStateLocked(StateConfigured)
	return Message{m.Label, MsgAccept, m.Signal, nil}, true
}

// RECONFIGURE changes application capabilities only (codec, content
// protection) and only while Open (§8.11).
func (s *Session) answerReconfigure(m Message) (Message, bool) {
	reject := func(cat Category, code ErrorCode) (Message, bool) {
		return Message{m.Label, MsgReject, m.Signal, []byte{byte(cat), byte(code)}}, true
	}
	p := m.Params
	if len(p) < 1 {
		return reject(0, CodeBadLength)
	}
	if p[0]>>2 != s.cfg.LocalSEID {
		return reject(0, CodeBadACPSEID)
	}
	if s.state == StateIdle {
		return reject(0, CodeSEPNotInUse)
	}
	if s.state != StateOpen {
		return reject(0, CodeBadState)
	}
	caps, err := ParseCapabilities(p[1:])
	if err != nil {
		var ce *CapError
		errors.As(err, &ce)
		return reject(ce.Category, ce.Code)
	}
	if cat, code := s.vet(caps, true); code != 0 {
		return reject(cat, code)
	}
	if code := s.accepts(*caps.Codec); code != 0 {
		return reject(CatMediaCodec, code)
	}
	s.config = caps.Codec
	return Message{m.Label, MsgAccept, m.Signal, nil}, true
}

// vet refuses what our SEP never offered. Undefined categories are
// BAD_SERV_CATEGORY; defined ones we do not support are INVALID_CAPABILITIES
// (§8.9.3). A reconfiguration may name only application categories.
func (s *Session) vet(c Capabilities, reconfigure bool) (Category, ErrorCode) {
	if len(c.Unknown) > 0 {
		return c.Unknown[0].Category, CodeBadServCategory
	}
	transport := []struct {
		cat     Category
		present bool
	}{
		{CatMediaTransport, c.MediaTransport && reconfigure},
		{CatReporting, c.Reporting},
		{CatRecovery, c.Recovery != nil},
		{CatContentProtection, len(c.ContentProtection) > 0},
		{CatHeaderCompression, c.HeaderCompression != nil},
		{CatMultiplexing, c.Multiplexing != nil},
		{CatDelayReporting, c.DelayReporting && (reconfigure || !s.cfg.DelayReporting)},
	}
	for _, t := range transport {
		if t.present {
			return t.cat, CodeInvalidCapabilities
		}
	}
	if c.Codec == nil {
		return CatMediaCodec, CodeInvalidCapabilities
	}
	return 0, 0
}

// accepts checks a proposed codec configuration against our SEP and Accept.
func (s *Session) accepts(c Codec) ErrorCode {
	if c.Media != s.cfg.Codec.Media || c.Type != s.cfg.Codec.Type {
		return CodeUnsupportedConfiguration
	}
	if s.cfg.Accept == nil {
		return 0
	}
	if err := s.cfg.Accept(c); err != nil {
		var ce *CodeError
		if errors.As(err, &ce) {
			return ce.Code
		}
		return CodeUnsupportedConfiguration
	}
	return 0
}
