package avrcp

import "encoding/binary"

// The category 1 target: the speaker is the controller and we are the player
// it presses buttons on. Absolute volume runs the other way (AVRCP 1.6
// §6.13): the sink renders the audio, so it is the category 2 target that
// owns EVENT_VOLUME_CHANGED, and we, the source, register with it. We
// therefore advertise only EVENT_PLAYBACK_STATUS_CHANGED.
var supportedEvents = []Event{EventPlaybackStatusChanged}

// target answers one inbound command. after, when set, runs once the
// response is on its way, so a slow callback cannot push us past TRCP.
func (s *Session) target(f frame, label uint8) (resp frame, after func()) {
	resp = frame{RespNotImplemented, f.subunit, f.opcode, f.operands}
	switch f.opcode {
	case opUnitInfo:
		// The unit is a panel, vendor the SIG (AVRCP 1.6 §6.1, AV/C UNIT INFO).
		if f.code == CtypeStatus && f.subunit == subunitUnit {
			resp.code = RespStable
			resp.operands = []byte{0x07, subunitPanel, CompanyBTSIG >> 16, CompanyBTSIG >> 8 & 0xFF, CompanyBTSIG & 0xFF}
		}
	case opSubunitInfo:
		// One subunit, the panel, on page 0; later pages are empty (0xFF).
		if f.code == CtypeStatus && f.subunit == subunitUnit && len(f.operands) >= 1 {
			entry := byte(subunitPanel)
			if f.operands[0]>>4&0x07 != 0 {
				entry = 0xFF
			}
			resp.code = RespStable
			resp.operands = []byte{f.operands[0], entry, 0xFF, 0xFF, 0xFF}
		}
	case opPassThrough:
		if f.code != CtypeControl || f.subunit != subunitPanel {
			break
		}
		op, released, err := parsePassThrough(f.operands)
		if err != nil || !supportedOps[op] {
			break
		}
		resp.code = RespAccepted
		if cb := s.cfg.OnPassThrough; cb != nil {
			return resp, func() { cb(op, !released) }
		}
	case opVendor:
		if f.subunit == subunitPanel {
			return s.vendorCommand(f, label), nil
		}
	}
	return resp, nil
}

// vendorCommand answers an AVRCP PDU. A company other than the SIG is NOT
// IMPLEMENTED (the frame is not AVRCP's); an AVRCP PDU we cannot serve is
// REJECTED with a status (AVRCP 1.6 §6.15.2).
func (s *Session) vendorCommand(f frame, label uint8) frame {
	company, v, err := parseVendor(f.operands)
	if company != CompanyBTSIG {
		return frame{RespNotImplemented, f.subunit, f.opcode, f.operands}
	}
	reject := func(status uint8) frame { return vendorFrame(RespRejected, v.id, []byte{status}) }
	// Commands always fit one frame; only responses fragment (§6.3.1).
	if err != nil || v.pktType != pktSingle {
		return reject(StatusInvalidCommand)
	}
	switch v.id {
	case pduGetCapabilities:
		if f.code != CtypeStatus {
			return reject(StatusInvalidCommand)
		}
		if len(v.params) != 1 {
			return reject(StatusParameterContent)
		}
		switch v.params[0] {
		case capCompanyID:
			return vendorFrame(RespStable, v.id, []byte{capCompanyID, 1,
				CompanyBTSIG >> 16, CompanyBTSIG >> 8 & 0xFF, CompanyBTSIG & 0xFF})
		case capEventsSupported:
			p := []byte{capEventsSupported, byte(len(supportedEvents))}
			for _, e := range supportedEvents {
				p = append(p, byte(e))
			}
			return vendorFrame(RespStable, v.id, p)
		}
		return reject(StatusInvalidParameter)
	case pduGetPlayStatus:
		if f.code != CtypeStatus {
			return reject(StatusInvalidCommand)
		}
		if len(v.params) != 0 {
			return reject(StatusParameterContent)
		}
		// Length and position unknown: 0xFFFFFFFF (§6.7.1).
		p := make([]byte, 9)
		binary.BigEndian.PutUint32(p[0:4], 0xFFFFFFFF)
		binary.BigEndian.PutUint32(p[4:8], 0xFFFFFFFF)
		s.mu.Lock()
		p[8] = byte(s.playStatus)
		s.mu.Unlock()
		return vendorFrame(RespStable, v.id, p)
	case pduRegisterNotification:
		if f.code != CtypeNotify {
			return reject(StatusInvalidCommand)
		}
		if len(v.params) != 5 {
			return reject(StatusParameterContent)
		}
		if Event(v.params[0]) != EventPlaybackStatusChanged {
			return reject(StatusInvalidParameter)
		}
		// A repeat registration replaces the last; CHANGED goes to the newest.
		s.mu.Lock()
		s.playLabel = int(label)
		st := s.playStatus
		s.mu.Unlock()
		return vendorFrame(RespInterim, v.id, []byte{byte(EventPlaybackStatusChanged), byte(st)})
	case pduRequestContinuingResponse, pduAbortContinuingResponse:
		// We never fragment a response, so there is nothing to continue.
		return reject(StatusInvalidParameter)
	}
	return reject(StatusInvalidCommand)
}

// SetPlayStatus records what we are doing. If the peer has registered for
// play status and it changed, the peer gets CHANGED on its registration's
// label, which ends that registration (§6.7.2): the peer re-registers.
func (s *Session) SetPlayStatus(st PlayStatus) error {
	s.mu.Lock()
	changed := st != s.playStatus
	s.playStatus = st
	label := s.playLabel
	if changed {
		s.playLabel = -1
	}
	s.mu.Unlock()
	if !changed || label < 0 {
		return nil
	}
	f := vendorFrame(RespChanged, pduRegisterNotification, []byte{byte(EventPlaybackStatusChanged), byte(st)})
	return s.send(message{label: uint8(label), response: true, pid: PID, payload: f.encode()})
}
