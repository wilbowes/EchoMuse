package bluetooth

// L2CAP on BR/EDR: the signalling channel (CID 0x0001) and connection-oriented
// channels in Basic mode, which is all SDP, AVDTP and AVCTP use. Core Spec
// v5.x Vol 3 Part A §4 (commands), §5 (configuration options), §6 (state).
//
// One l2Signal per ACL link. It is pure: frames go out through the send
// function it is given and come in through onFrame, so two of them wired
// back to back are a complete test bench.

import (
	"encoding/binary"
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	cidSignalBREDR = 0x0001
	cidDynFirst    = 0x0040

	sigCommandReject = 0x01
	sigConnReq       = 0x02
	sigConnRsp       = 0x03
	sigConfigReq     = 0x04
	sigConfigRsp     = 0x05
	sigDisconnReq    = 0x06
	sigDisconnRsp    = 0x07
	sigEchoReq       = 0x08
	sigEchoRsp       = 0x09
	sigInfoReq       = 0x0A
	sigInfoRsp       = 0x0B

	connSuccess     = 0x0000
	connPending     = 0x0001
	connBadPSM      = 0x0002
	connSecurity    = 0x0003
	connNoResources = 0x0004

	cfgSuccess      = 0x0000
	cfgUnacceptable = 0x0001
	cfgRejected     = 0x0002
	cfgUnknown      = 0x0003

	optMTU          = 0x01
	optFlushTimeout = 0x02
	optQoS          = 0x03
	optRFC          = 0x04
	optFCS          = 0x07

	rejectNotUnderstood = 0x0000
	rejectInvalidCID    = 0x0002

	// The minimum and default MTU on a BR/EDR dynamic channel (§5.1).
	l2MinMTU     = 48
	l2DefaultMTU = 672

	// RTX: how long a request may go unanswered (§6.2.1 allows 1-60s);
	// ERTX: after a Pending response, how long the final one may take.
	l2RTX  = 10 * time.Second
	l2ERTX = 60 * time.Second
)

var (
	ErrL2Refused = errors.New("l2cap: connection refused")
	ErrL2Closed  = errors.New("l2cap: channel closed")
	ErrL2Timeout = errors.New("l2cap: no response")
)

// L2Channel is one open connection-oriented channel.
type L2Channel struct {
	PSM    uint16
	Local  uint16 // our CID: the peer sends to this
	Remote uint16 // theirs: we send to this

	sig *l2Signal

	mu        sync.Mutex
	outMTU    int // the largest SDU the peer accepts
	localDone bool
	peerDone  bool
	open      chan struct{} // closed when both configurations are done
	gone      chan struct{} // closed when the channel ends
	closed    bool

	onData func([]byte)
	early  [][]byte // SDUs that arrived before a handler was set
}

// maxEarlySDUs bounds what is held for a channel nobody is reading yet: the
// peer may send the moment configuration completes, before our side has
// handed the channel to whoever reads it.
const maxEarlySDUs = 8

// SetOnData sets the handler for inbound SDUs and delivers any that arrived
// first. It runs on the link's reader and must not block.
func (c *L2Channel) SetOnData(f func([]byte)) {
	c.mu.Lock()
	c.onData = f
	early := c.early
	c.early = nil
	c.mu.Unlock()
	for _, b := range early {
		f(b)
	}
}

func (c *L2Channel) deliver(b []byte) {
	c.mu.Lock()
	f := c.onData
	if f == nil {
		if len(c.early) < maxEarlySDUs {
			c.early = append(c.early, append([]byte(nil), b...))
		}
		c.mu.Unlock()
		return
	}
	c.mu.Unlock()
	f(b)
}

// MTU is the largest SDU the peer said it accepts.
func (c *L2Channel) MTU() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.outMTU
}

// Send writes one SDU.
func (c *L2Channel) Send(sdu []byte) error {
	c.mu.Lock()
	closed, mtu := c.closed, c.outMTU
	c.mu.Unlock()
	if closed {
		return ErrL2Closed
	}
	if len(sdu) > mtu {
		return fmt.Errorf("l2cap: %d bytes over the peer's MTU %d", len(sdu), mtu)
	}
	return c.sig.send(c.Remote, sdu)
}

// Gone is closed when the channel ends.
func (c *L2Channel) Gone() <-chan struct{} { return c.gone }

// Close disconnects the channel.
func (c *L2Channel) Close() error { return c.sig.disconnect(c) }

// Request implements a one-outstanding-request transport (SDP's) over the
// channel: send, then wait for the next SDU.
type l2Requester struct {
	ch  *L2Channel
	rsp chan []byte
	mu  sync.Mutex
}

func newL2Requester(ch *L2Channel) *l2Requester {
	r := &l2Requester{ch: ch, rsp: make(chan []byte, 1)}
	ch.SetOnData(func(b []byte) {
		select {
		case r.rsp <- append([]byte(nil), b...):
		default:
		}
	})
	return r
}

func (r *l2Requester) Request(pdu []byte) ([]byte, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	select {
	case <-r.rsp:
	default:
	}
	if err := r.ch.Send(pdu); err != nil {
		return nil, err
	}
	t := time.NewTimer(l2RTX)
	defer t.Stop()
	select {
	case b := <-r.rsp:
		return b, nil
	case <-r.ch.gone:
		return nil, ErrL2Closed
	case <-t.C:
		return nil, ErrL2Timeout
	}
}

// l2Accept decides an inbound connection: return connSuccess to take it, or
// a refusal result. It runs on the reader.
type l2Accept func(psm uint16) uint16

type l2Signal struct {
	send     func(cid uint16, pdu []byte) error
	accept   l2Accept
	onOpen   func(*L2Channel) // an inbound channel finished configuring
	localMTU int              // what we accept, advertised in every ConfigReq

	mu      sync.Mutex
	nextID  byte
	nextCID uint16
	chans   map[uint16]*L2Channel // by local CID
	waits   map[byte]chan []byte  // responses by identifier
	cfgPart map[uint16][]byte     // a ConfigReq continued across commands
	down    bool
}

func newL2Signal(send func(cid uint16, pdu []byte) error, accept l2Accept, onOpen func(*L2Channel), localMTU int) *l2Signal {
	if localMTU < l2MinMTU {
		localMTU = l2DefaultMTU
	}
	return &l2Signal{
		send: send, accept: accept, onOpen: onOpen, localMTU: localMTU,
		nextID: 1, nextCID: cidDynFirst,
		chans: map[uint16]*L2Channel{}, waits: map[byte]chan []byte{}, cfgPart: map[uint16][]byte{},
	}
}

// onFrame takes one L2CAP basic frame for this link.
func (s *l2Signal) onFrame(cid uint16, payload []byte) {
	if cid == cidSignalBREDR {
		s.onSignal(payload)
		return
	}
	s.mu.Lock()
	ch := s.chans[cid]
	s.mu.Unlock()
	if ch != nil {
		ch.deliver(payload)
	}
}

// linkDown ends every channel: the ACL link is gone.
func (s *l2Signal) linkDown() {
	s.mu.Lock()
	s.down = true
	chans := s.chans
	s.chans = map[uint16]*L2Channel{}
	s.mu.Unlock()
	for _, c := range chans {
		c.end()
	}
}

func (c *L2Channel) end() {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	c.closed = true
	c.mu.Unlock()
	close(c.gone)
}

// command builds one signalling command.
func command(code, id byte, data []byte) []byte {
	b := make([]byte, 4+len(data))
	b[0], b[1] = code, id
	binary.LittleEndian.PutUint16(b[2:4], uint16(len(data)))
	copy(b[4:], data)
	return b
}

func (s *l2Signal) ident() byte {
	id := s.nextID
	s.nextID++
	if s.nextID == 0 { // 0x00 is never a valid identifier
		s.nextID = 1
	}
	return id
}

// onSignal walks every command in a C-frame (BR/EDR may pack several).
func (s *l2Signal) onSignal(p []byte) {
	for len(p) >= 4 {
		code, id := p[0], p[1]
		n := int(binary.LittleEndian.Uint16(p[2:4]))
		if 4+n > len(p) {
			return
		}
		data := p[4 : 4+n]
		p = p[4+n:]
		switch code {
		case sigConnRsp, sigConfigRsp, sigDisconnRsp, sigEchoRsp, sigInfoRsp, sigCommandReject:
			if code == sigConnRsp && len(data) >= 6 && binary.LittleEndian.Uint16(data[4:6]) == connSuccess {
				// The peer's CID is recorded here, on the reader: its
				// ConfigReq can be the very next command, and the reply to
				// it is addressed with this.
				s.mu.Lock()
				if c := s.chans[binary.LittleEndian.Uint16(data[2:4])]; c != nil {
					c.Remote = binary.LittleEndian.Uint16(data[0:2])
				}
				s.mu.Unlock()
			}
			s.mu.Lock()
			w := s.waits[id]
			s.mu.Unlock()
			if w != nil {
				select {
				case w <- append([]byte{code}, data...):
				default:
				}
			}
		case sigConnReq:
			s.onConnReq(id, data)
		case sigConfigReq:
			s.onConfigReq(id, data)
		case sigDisconnReq:
			s.onDisconnReq(id, data)
		case sigEchoReq:
			_ = s.send(cidSignalBREDR, command(sigEchoRsp, id, data))
		case sigInfoReq:
			s.onInfoReq(id, data)
		default:
			_ = s.send(cidSignalBREDR, command(sigCommandReject, id, []byte{rejectNotUnderstood, 0}))
		}
	}
}

func (s *l2Signal) onInfoReq(id byte, data []byte) {
	if len(data) < 2 {
		return
	}
	typ := binary.LittleEndian.Uint16(data)
	rsp := make([]byte, 4, 12)
	binary.LittleEndian.PutUint16(rsp[0:2], typ)
	switch typ {
	case 0x0002: // extended features: fixed channels supported, nothing else
		rsp = binary.LittleEndian.AppendUint32(rsp, 0x00000080)
	case 0x0003: // fixed channels: the signalling channel only
		rsp = binary.LittleEndian.AppendUint64(rsp, 0x0000000000000002)
	default:
		binary.LittleEndian.PutUint16(rsp[2:4], 0x0001) // not supported
	}
	_ = s.send(cidSignalBREDR, command(sigInfoRsp, id, rsp))
}

func (s *l2Signal) newChannel(psm, remote uint16) (*L2Channel, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.down {
		return nil, false
	}
	for i := 0; i < 0xFFBF; i++ {
		cid := s.nextCID
		s.nextCID++
		if s.nextCID < cidDynFirst {
			s.nextCID = cidDynFirst
		}
		if s.chans[cid] == nil {
			c := &L2Channel{PSM: psm, Local: cid, Remote: remote, sig: s, outMTU: l2DefaultMTU,
				open: make(chan struct{}), gone: make(chan struct{})}
			s.chans[cid] = c
			return c, true
		}
	}
	return nil, false
}

func (s *l2Signal) onConnReq(id byte, data []byte) {
	if len(data) < 4 {
		return
	}
	psm, scid := binary.LittleEndian.Uint16(data[0:2]), binary.LittleEndian.Uint16(data[2:4])
	result := connBadPSM
	if s.accept != nil {
		result = int(s.accept(psm))
	}
	var c *L2Channel
	if result == connSuccess {
		var ok bool
		if c, ok = s.newChannel(psm, scid); !ok {
			result = connNoResources
		}
	}
	rsp := make([]byte, 8)
	if c != nil {
		binary.LittleEndian.PutUint16(rsp[0:2], c.Local)
	}
	binary.LittleEndian.PutUint16(rsp[2:4], scid)
	binary.LittleEndian.PutUint16(rsp[4:6], uint16(result))
	_ = s.send(cidSignalBREDR, command(sigConnRsp, id, rsp))
	if c != nil {
		// Our side of the configuration runs off the reader: it waits for a
		// response that the reader itself has to deliver.
		go func() {
			if err := s.configure(c); err != nil {
				_ = s.disconnect(c)
				return
			}
			if s.waitOpen(c) == nil && s.onOpen != nil {
				s.onOpen(c)
			}
		}()
	}
}

// configOptions parses the option list of a ConfigReq. It returns the MTU the
// peer will accept (0 when not stated), and the options to refuse: unknown
// non-hint ones (§5, result 0x0003), or values we cannot take.
func configOptions(opts []byte) (mtu int, unknown []byte, unacceptable []byte) {
	for len(opts) >= 2 {
		typ, n := opts[0], int(opts[1])
		if 2+n > len(opts) {
			break
		}
		val := opts[2 : 2+n]
		raw := opts[:2+n]
		opts = opts[2+n:]
		switch typ & 0x7F {
		case optMTU:
			if n == 2 {
				mtu = int(binary.LittleEndian.Uint16(val))
				if mtu < l2MinMTU {
					// Refuse it, offering the minimum (§5.1).
					unacceptable = append(unacceptable, optMTU, 2, l2MinMTU, 0)
				}
			}
		case optFlushTimeout, optQoS, optFCS:
			// Flush timeout and QoS are the peer's statement about its own
			// traffic; FCS only matters outside Basic mode.
		case optRFC:
			if n >= 1 && val[0] != 0x00 {
				// ERTM or streaming: we do Basic mode only.
				unacceptable = append(unacceptable, optRFC, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0)
			}
		default:
			if typ&0x80 == 0 { // a hint may be ignored; anything else may not
				unknown = append(unknown, raw...)
			}
		}
	}
	return mtu, unknown, unacceptable
}

func (s *l2Signal) onConfigReq(id byte, data []byte) {
	if len(data) < 4 {
		return
	}
	dcid, flags := binary.LittleEndian.Uint16(data[0:2]), binary.LittleEndian.Uint16(data[2:4])
	s.mu.Lock()
	c := s.chans[dcid]
	if c == nil {
		s.mu.Unlock()
		rej := []byte{rejectInvalidCID, 0, byte(dcid), byte(dcid >> 8), 0, 0}
		_ = s.send(cidSignalBREDR, command(sigCommandReject, id, rej))
		return
	}
	opts := append(s.cfgPart[dcid], data[4:]...)
	if flags&0x0001 != 0 { // continued: acknowledge, keep collecting
		s.cfgPart[dcid] = opts
		s.mu.Unlock()
		_ = s.send(cidSignalBREDR, command(sigConfigRsp, id, configRsp(c.Remote, 0x0001, cfgSuccess, nil)))
		return
	}
	delete(s.cfgPart, dcid)
	s.mu.Unlock()

	mtu, unknown, bad := configOptions(opts)
	switch {
	case len(unknown) > 0:
		_ = s.send(cidSignalBREDR, command(sigConfigRsp, id, configRsp(c.Remote, 0, cfgUnknown, unknown)))
		return
	case len(bad) > 0:
		_ = s.send(cidSignalBREDR, command(sigConfigRsp, id, configRsp(c.Remote, 0, cfgUnacceptable, bad)))
		return
	}
	c.mu.Lock()
	if mtu > 0 {
		c.outMTU = mtu
	}
	c.peerDone = true
	both := c.localDone
	c.mu.Unlock()
	_ = s.send(cidSignalBREDR, command(sigConfigRsp, id, configRsp(c.Remote, 0, cfgSuccess, nil)))
	if both {
		c.markOpen()
	}
}

func (c *L2Channel) markOpen() {
	select {
	case <-c.open:
	default:
		close(c.open)
	}
}

func configRsp(scid, flags, result uint16, opts []byte) []byte {
	b := make([]byte, 6+len(opts))
	binary.LittleEndian.PutUint16(b[0:2], scid)
	binary.LittleEndian.PutUint16(b[2:4], flags)
	binary.LittleEndian.PutUint16(b[4:6], result)
	copy(b[6:], opts)
	return b
}

// request sends one command and waits for the answer with the same
// identifier. Pending connection responses extend the wait to ERTX.
func (s *l2Signal) request(code byte, data []byte) ([]byte, error) {
	s.mu.Lock()
	if s.down {
		s.mu.Unlock()
		return nil, ErrL2Closed
	}
	id := s.ident()
	w := make(chan []byte, 2)
	s.waits[id] = w
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.waits, id)
		s.mu.Unlock()
	}()
	if err := s.send(cidSignalBREDR, command(code, id, data)); err != nil {
		return nil, err
	}
	t := time.NewTimer(l2RTX)
	defer t.Stop()
	for {
		select {
		case r := <-w:
			if r[0] == sigConnRsp && len(r) >= 7 && binary.LittleEndian.Uint16(r[5:7]) == connPending {
				t.Reset(l2ERTX)
				continue
			}
			return r, nil
		case <-t.C:
			return nil, ErrL2Timeout
		}
	}
}

// configure sends our ConfigReq (our receive MTU) until the peer accepts it,
// taking its counter-proposal once for an MTU it refused.
func (s *l2Signal) configure(c *L2Channel) error {
	mtu := s.localMTU
	for attempt := 0; attempt < 2; attempt++ {
		req := make([]byte, 8)
		binary.LittleEndian.PutUint16(req[0:2], c.Remote)
		req[4], req[5] = optMTU, 2
		binary.LittleEndian.PutUint16(req[6:8], uint16(mtu))
		r, err := s.request(sigConfigReq, req)
		if err != nil {
			return err
		}
		if r[0] != sigConfigRsp || len(r) < 7 {
			return fmt.Errorf("l2cap: configuration rejected")
		}
		switch binary.LittleEndian.Uint16(r[5:7]) {
		case cfgSuccess:
			c.mu.Lock()
			c.localDone = true
			both := c.peerDone
			c.mu.Unlock()
			if both {
				c.markOpen()
			}
			return nil
		case cfgUnacceptable:
			if m, _, _ := configOptions(r[7:]); m >= l2MinMTU {
				mtu = m
				continue
			}
		}
		return fmt.Errorf("l2cap: configuration refused (0x%04x)", binary.LittleEndian.Uint16(r[5:7]))
	}
	return fmt.Errorf("l2cap: configuration did not converge")
}

func (s *l2Signal) waitOpen(c *L2Channel) error {
	t := time.NewTimer(l2RTX)
	defer t.Stop()
	select {
	case <-c.open:
		return nil
	case <-c.gone:
		return ErrL2Closed
	case <-t.C:
		return ErrL2Timeout
	}
}

// Connect opens an outbound channel to psm and returns it configured.
func (s *l2Signal) Connect(psm uint16) (*L2Channel, error) {
	c, ok := s.newChannel(psm, 0)
	if !ok {
		return nil, ErrL2Closed
	}
	forget := func() {
		s.mu.Lock()
		delete(s.chans, c.Local)
		s.mu.Unlock()
		c.end()
	}
	req := make([]byte, 4)
	binary.LittleEndian.PutUint16(req[0:2], psm)
	binary.LittleEndian.PutUint16(req[2:4], c.Local)
	r, err := s.request(sigConnReq, req)
	if err != nil {
		forget()
		return nil, err
	}
	if r[0] != sigConnRsp || len(r) < 9 {
		forget()
		return nil, ErrL2Refused
	}
	if res := binary.LittleEndian.Uint16(r[5:7]); res != connSuccess {
		forget()
		return nil, fmt.Errorf("%w (psm 0x%04x, result 0x%04x)", ErrL2Refused, psm, res)
	}
	if err := s.configure(c); err != nil {
		_ = s.disconnect(c)
		return nil, err
	}
	if err := s.waitOpen(c); err != nil {
		_ = s.disconnect(c)
		return nil, err
	}
	return c, nil
}

func (s *l2Signal) onDisconnReq(id byte, data []byte) {
	if len(data) < 4 {
		return
	}
	dcid, scid := binary.LittleEndian.Uint16(data[0:2]), binary.LittleEndian.Uint16(data[2:4])
	s.mu.Lock()
	c := s.chans[dcid]
	if c != nil && c.Remote == scid {
		delete(s.chans, dcid)
	} else {
		c = nil
	}
	s.mu.Unlock()
	if c == nil {
		rej := []byte{rejectInvalidCID, 0, byte(dcid), byte(dcid >> 8), byte(scid), byte(scid >> 8)}
		_ = s.send(cidSignalBREDR, command(sigCommandReject, id, rej))
		return
	}
	_ = s.send(cidSignalBREDR, command(sigDisconnRsp, id, data[0:4]))
	c.end()
}

func (s *l2Signal) disconnect(c *L2Channel) error {
	s.mu.Lock()
	_, live := s.chans[c.Local]
	delete(s.chans, c.Local)
	s.mu.Unlock()
	if !live {
		c.end()
		return nil
	}
	req := make([]byte, 4)
	binary.LittleEndian.PutUint16(req[0:2], c.Remote)
	binary.LittleEndian.PutUint16(req[2:4], c.Local)
	c.end()
	_, err := s.request(sigDisconnReq, req)
	return err
}
