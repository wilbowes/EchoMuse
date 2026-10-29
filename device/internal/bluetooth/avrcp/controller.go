package avrcp

// The category 2 controller: we set the speaker's volume and follow changes
// made on the speaker itself (AVRCP 1.6 §6.13), and can press its buttons.

func registerFrame(e Event) frame {
	// Event ID, then a 4-byte playback interval that only
	// EVENT_PLAYBACK_POS_CHANGED reads (§6.7.2).
	return vendorFrame(CtypeNotify, pduRegisterNotification, []byte{byte(e), 0, 0, 0, 0})
}

// vendorResult unwraps a vendor-dependent response to pdu: its parameters,
// or the reason there are none.
func (s *Session) vendorResult(f frame, pdu uint8) ([]byte, error) {
	if f.code == RespNotImplemented {
		return nil, ErrNotImplemented
	}
	company, v, err := parseVendor(f.operands)
	if err != nil || company != CompanyBTSIG || v.id != pdu {
		return nil, ErrUnexpected
	}
	if v.pktType != pktSingle {
		// None of our requests has a long answer; stop the peer sending the
		// rest rather than buffering it (§6.8.2).
		go s.request(vendorFrame(CtypeControl, pduAbortContinuingResponse, []byte{pdu}), false)
		return nil, ErrFragmented
	}
	if f.code == RespRejected {
		st := uint8(StatusInternalError)
		if len(v.params) > 0 {
			st = v.params[0]
		}
		return nil, &StatusError{PDU: pdu, Status: st}
	}
	return v.params, nil
}

func (s *Session) getCapabilities(id uint8, size int) ([]byte, error) {
	f, err := s.request(vendorFrame(CtypeStatus, pduGetCapabilities, []byte{id}), false)
	if err != nil {
		return nil, err
	}
	p, err := s.vendorResult(f, pduGetCapabilities)
	if err != nil {
		return nil, err
	}
	if f.code != RespStable {
		return nil, ErrUnexpected
	}
	if len(p) < 2 || p[0] != id || len(p) != 2+int(p[1])*size {
		return nil, ErrMalformed
	}
	return p[2:], nil
}

// PeerEvents asks the peer which notification events it supports. A sink
// without EventVolumeChanged has no absolute volume.
func (s *Session) PeerEvents() ([]Event, error) {
	b, err := s.getCapabilities(capEventsSupported, 1)
	if err != nil {
		return nil, err
	}
	out := make([]Event, len(b))
	for i, e := range b {
		out[i] = Event(e)
	}
	return out, nil
}

// PeerCompanies asks the peer for the company IDs it supports.
func (s *Session) PeerCompanies() ([]uint32, error) {
	b, err := s.getCapabilities(capCompanyID, 3)
	if err != nil {
		return nil, err
	}
	out := make([]uint32, 0, len(b)/3)
	for i := 0; i+3 <= len(b); i += 3 {
		out = append(out, uint32(b[i])<<16|uint32(b[i+1])<<8|uint32(b[i+2]))
	}
	return out, nil
}

// SetAbsoluteVolume sets the peer's volume (0..0x7F) and returns the volume
// it actually applied, which a sink with coarser steps may round.
func (s *Session) SetAbsoluteVolume(abs uint8) (uint8, error) {
	if abs > 0x7F {
		return 0, ErrRange
	}
	f, err := s.request(vendorFrame(CtypeControl, pduSetAbsoluteVolume, []byte{abs}), false)
	if err != nil {
		return 0, err
	}
	p, err := s.vendorResult(f, pduSetAbsoluteVolume)
	if err != nil {
		return 0, err
	}
	if f.code != RespAccepted {
		return 0, ErrUnexpected
	}
	if len(p) != 1 {
		return 0, ErrMalformed
	}
	got := p[0] & 0x7F
	s.mu.Lock()
	s.lastVol = int(got)
	s.mu.Unlock()
	return got, nil
}

// WatchVolume registers for the peer's volume changes and returns its
// current volume. Each CHANGED goes to Config.OnVolume and is followed by a
// fresh registration, since a CHANGED ends the one before (§6.7.2), until
// the session closes or the peer stops answering.
func (s *Session) WatchVolume() (uint8, error) {
	s.mu.Lock()
	if s.volWatch && s.lastVol >= 0 {
		v := uint8(s.lastVol)
		s.mu.Unlock()
		return v, nil
	}
	s.volWatch = true
	s.mu.Unlock()
	v, err := s.registerVolume()
	if err != nil {
		s.mu.Lock()
		s.volWatch = false
		s.mu.Unlock()
	}
	return v, err
}

func (s *Session) registerVolume() (uint8, error) {
	f, err := s.request(registerFrame(EventVolumeChanged), true)
	if err != nil {
		return 0, err
	}
	p, err := s.vendorResult(f, pduRegisterNotification)
	if err != nil {
		return 0, err
	}
	if f.code != RespInterim && f.code != RespChanged {
		return 0, ErrUnexpected
	}
	v, ok := volumeParam(p)
	if !ok {
		return 0, ErrMalformed
	}
	s.mu.Lock()
	s.lastVol = int(v)
	s.mu.Unlock()
	return v, nil
}

// volumeParam reads an EVENT_VOLUME_CHANGED payload: event ID, volume.
func volumeParam(p []byte) (uint8, bool) {
	if len(p) != 2 || Event(p[0]) != EventVolumeChanged {
		return 0, false
	}
	return p[1] & 0x7F, true
}

// notification handles a response to one of our registrations: INTERIM
// answers the registration, CHANGED ends it, anything else refuses it.
func (s *Session) notification(label uint8, p *pending, f frame) {
	var vol uint8
	volOK := false
	if _, v, err := parseVendor(f.operands); err == nil && v.id == pduRegisterNotification {
		vol, volOK = volumeParam(v.params)
	}
	s.mu.Lock()
	if s.pending[label] != p {
		s.mu.Unlock()
		return
	}
	resp := p.resp
	p.resp = nil
	report, rewatch := false, false
	switch f.code {
	case RespInterim:
		p.gen++ // the registration now waits on the peer, with no deadline
		p.timer.Stop()
		// A background re-registration's INTERIM carries the volume now; it
		// is news only if it moved while we were re-registering.
		report = resp == nil && volOK && int(vol) != s.lastVol
	case RespChanged:
		delete(s.pending, label)
		p.timer.Stop()
		report = volOK
		rewatch = volOK && s.volWatch
	default:
		delete(s.pending, label)
		p.timer.Stop()
		if p.event == EventVolumeChanged {
			s.volWatch = false
		}
	}
	if report {
		s.lastVol = int(vol)
	}
	cb := s.cfg.OnVolume
	s.mu.Unlock()

	if resp != nil {
		resp <- result{f: f}
	}
	if report && cb != nil {
		cb(vol)
	}
	if rewatch {
		if _, err := s.start(registerFrame(EventVolumeChanged), true, false); err != nil {
			s.mu.Lock()
			s.volWatch = false
			s.mu.Unlock()
		}
	}
}

// PassThrough presses and releases one of the peer's buttons.
func (s *Session) PassThrough(op Op) error {
	for _, released := range []bool{false, true} {
		f, err := s.request(frame{CtypeControl, subunitPanel, opPassThrough, passThroughOperands(op, released)}, false)
		if err != nil {
			return err
		}
		switch f.code {
		case RespAccepted:
		case RespNotImplemented:
			return ErrNotImplemented
		case RespRejected:
			return ErrRejected
		default:
			return ErrUnexpected
		}
	}
	return nil
}
