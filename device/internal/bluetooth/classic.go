package bluetooth

// BR/EDR links: discovery, connecting, pairing and the L2CAP channels the
// audio profiles ride on. It shares the HCI transport with the LE side
// (hciHost) and is fed every packet the LE connection manager does not own.
//
// Security is Secure Simple Pairing, Just Works (the Echo has no display), or
// legacy PIN 0000 for speakers that predate it. Pairing is accepted ONLY for
// the one address a person asked to pair, inside its window; any other
// device that tries to pair, or to connect without a stored key, is refused.

import (
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"
)

const (
	psmSDP   = 0x0001
	psmAVCTP = 0x0017
	psmAVDTP = 0x0019

	classicConnectTimeout = 20 * time.Second
	classicSecureTimeout  = 30 * time.Second
	pairWindow            = 60 * time.Second
	remoteNameTimeout     = 6 * time.Second

	// Page timeout 0x4000 slots = 10.24s; the default 5.12s is short for a
	// speaker that is waking from standby.
	pageTimeoutSlots = 0x4000

	// HCI reasons and statuses used here (Vol 1 Part F).
	hciUnacceptableAddr  = 0x0F
	hciPairingNotAllowed = 0x18
	hciKeyMissing        = 0x06
	hciConnExists        = 0x0B
	hciAuthFailure       = 0x05

	// L2CAP receive MTU we offer on BR/EDR channels: what the reassembler
	// keeps (l2capMaxPDU). Inbound traffic is signalling and SDP, all small.
	classicLocalMTU = l2capMaxPDU
)

var (
	ErrClassicNotRunning = errors.New("bluetooth: the radio is not running")
	ErrPairingRefused    = errors.New("bluetooth: pairing failed")
	ErrKeyMissing        = errors.New("bluetooth: the speaker no longer has our pairing; pair it again")
	ErrNotPaired         = errors.New("bluetooth: not paired with that device")
)

// ClassicLink is one ACL link to a BR/EDR device.
type ClassicLink struct {
	Addr   string
	handle uint16
	sig    *l2Signal
	gone   chan struct{}

	mu        sync.Mutex
	unacked   int
	encrypted bool
}

// Gone is closed when the link drops.
func (l *ClassicLink) Gone() <-chan struct{} { return l.gone }

// Encrypted reports whether the link is authenticated and encrypted.
func (l *ClassicLink) Encrypted() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.encrypted
}

// Connect opens an L2CAP channel on the link.
func (l *ClassicLink) Connect(psm uint16) (*L2Channel, error) { return l.sig.Connect(psm) }

// eventWaiter collects the events its match function accepts.
type eventWaiter struct {
	match func([]byte) bool
	ch    chan []byte
}

// Classic manages BR/EDR links. Make one with NewClassic; a Scanner attaches
// it to the radio when one is running.
type Classic struct {
	keys *keyStore

	// SDP answers a request on the SDP channel (the server's Handle). nil
	// refuses SDP connections.
	SDP func(req []byte, mtu int) []byte
	// OnChannel is given every inbound channel that opens on a PSM the
	// profiles use (AVDTP, AVCTP). It runs off the reader.
	OnChannel func(l *ClassicLink, ch *L2Channel)
	// OnLinkDown is called once per link that ends. It must not block.
	OnLinkDown func(addr string, reason byte)

	localName string

	mu        sync.Mutex
	host      *hciHost
	links     map[uint16]*ClassicLink
	sem       chan struct{}
	aclLen    int
	pairAddr  string
	pairUntil time.Time
	waiters   []*eventWaiter

	reasm aclReassembler // reader only

	busy   atomic.Int32
	onBusy func()
}

// NewClassic loads the paired devices from keyPath. A file that cannot be
// read is reported and treated as empty: the cost is pairing again.
func NewClassic(keyPath, localName string) *Classic {
	ks, err := openKeyStore(keyPath)
	if err != nil {
		log.Printf("[bt] paired devices unreadable, starting empty: %v", err)
	}
	return &Classic{keys: ks, localName: localName}
}

// Paired lists the paired devices, without their keys.
func (c *Classic) Paired() []Paired { return c.keys.list() }

// Busy reports whether discovery, pairing or a stream wants the radio, so the
// LE scan stays off.
func (c *Classic) Busy() bool { return c.busy.Load() > 0 }

// Hold marks the radio busy while held (an audio stream).
func (c *Classic) Hold(on bool) {
	if on {
		c.busy.Add(1)
	} else {
		c.busy.Add(-1)
	}
	if f := c.onBusy; f != nil {
		f()
	}
}

// attach brings up BR/EDR on a transport that has just been reset. pool is
// the LE side's ACL buffers when LE has none of its own, which is when the
// two share the controller's BR/EDR buffers (the MT6627); nil gives Classic
// its own count from Read Buffer Size.
func (c *Classic) attach(h *hciHost, pool chan struct{}, poolLen int) {
	setup := []struct {
		name   string
		op     uint16
		params []byte
	}{
		{"event mask", opSetEventMask, eventMaskParams()},
		{"simple pairing", opWriteSimplePairing, []byte{0x01}},
		{"inquiry mode", opWriteInquiryMode, []byte{0x02}}, // extended: names in the results
		{"class of device", opWriteClassOfDevice, codParams(ownClassOfDevice)},
		{"local name", opWriteLocalName, localNameParams(c.localName)},
		{"page timeout", opWritePageTimeout, []byte{pageTimeoutSlots & 0xFF, pageTimeoutSlots >> 8}},
		{"link policy", opWriteDefLinkPolicy, []byte{0x05, 0x00}}, // role switch, sniff
	}
	for _, s := range setup {
		if _, err := h.Cmd(s.op, s.params); err != nil {
			log.Printf("[bt] %s: %v", s.name, err)
		}
	}
	aclLen, sem := poolLen, pool
	if sem == nil {
		aclLen, sem = 27, make(chan struct{}, 1)
		if cc, err := h.Cmd(opReadBufferSize, nil); err == nil && len(cc.params) >= 5 {
			if l, n := int(binary.LittleEndian.Uint16(cc.params[0:2])), int(binary.LittleEndian.Uint16(cc.params[3:5])); l > 0 && n > 0 {
				aclLen, sem = l, make(chan struct{}, n)
			}
		}
	}
	c.mu.Lock()
	c.host, c.links, c.sem, c.aclLen = h, map[uint16]*ClassicLink{}, sem, aclLen
	c.reasm = aclReassembler{}
	c.mu.Unlock()
	c.updateScanEnable()
}

// detach ends every link: the transport that carried them is gone.
func (c *Classic) detach() {
	c.mu.Lock()
	links := c.links
	c.host, c.links = nil, nil
	c.mu.Unlock()
	for _, l := range links {
		c.linkEnded(l, hciReasonLocalHost)
	}
}

// updateScanEnable makes the Echo connectable (page scan) while it has a
// paired device to come back to it, and never discoverable: pairing is
// always started from our side.
func (c *Classic) updateScanEnable() {
	c.mu.Lock()
	h := c.host
	c.mu.Unlock()
	if h == nil {
		return
	}
	mode := byte(0x00)
	if len(c.keys.list()) > 0 {
		mode = scanPage
	}
	if _, err := h.Cmd(opWriteScanEnable, []byte{mode}); err != nil {
		log.Printf("[bt] scan enable: %v", err)
	}
}

func (c *Classic) running() (*hciHost, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.host == nil {
		return nil, ErrClassicNotRunning
	}
	return c.host, nil
}

// pairingAllowed is true only for the address being paired, inside its window.
func (c *Classic) pairingAllowed(addr string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return addr != "" && addr == c.pairAddr && time.Now().Before(c.pairUntil)
}

func (c *Classic) await(match func([]byte) bool, depth int) *eventWaiter {
	w := &eventWaiter{match: match, ch: make(chan []byte, depth)}
	c.mu.Lock()
	c.waiters = append(c.waiters, w)
	c.mu.Unlock()
	return w
}

func (c *Classic) unwait(w *eventWaiter) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i, o := range c.waiters {
		if o == w {
			c.waiters = append(c.waiters[:i], c.waiters[i+1:]...)
			return
		}
	}
}

func (c *Classic) offer(pkt []byte) {
	c.mu.Lock()
	ws := append([]*eventWaiter(nil), c.waiters...)
	c.mu.Unlock()
	for _, w := range ws {
		if w.match(pkt) {
			select {
			case w.ch <- pkt:
			default:
			}
		}
	}
}

func (c *Classic) byHandle(handle uint16) *ClassicLink {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.links[handle]
}

func (c *Classic) byAddr(addr string) *ClassicLink {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, l := range c.links {
		if l.Addr == addr {
			return l
		}
	}
	return nil
}

// Links lists the addresses of the BR/EDR links that are up.
func (c *Classic) Links() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, 0, len(c.links))
	for _, l := range c.links {
		out = append(out, l.Addr)
	}
	return out
}

// handle takes one packet the LE manager did not own. It runs on the
// manager's reader, so anything that waits on the controller goes to a
// goroutine.
func (c *Classic) handle(pkt []byte) {
	if len(pkt) == 0 {
		return
	}
	if pkt[0] == h4TypeACL {
		if handle, cid, payload, ok := c.reasm.Feed(pkt); ok {
			if l := c.byHandle(handle); l != nil {
				l.sig.onFrame(cid, payload)
			}
		}
		return
	}
	if len(pkt) < 3 || pkt[0] != h4TypeEvent {
		return
	}
	c.onEvent(pkt)
	c.offer(pkt)
}

func (c *Classic) onEvent(pkt []byte) {
	h, err := c.running()
	if err != nil {
		return
	}
	switch pkt[1] {
	case evtConnComplete:
		if cc, ok := parseClassicConnComplete(pkt); ok && cc.status == 0 && cc.linkType == linkTypeACL {
			c.linkUp(cc)
		}
	case evtConnRequest:
		// addr 6, class 3, link type 1
		addr, ok := eventAddr(pkt, 0)
		if !ok || len(pkt) < 3+10 {
			return
		}
		ba := pkt[3:9]
		if pkt[12] == linkTypeACL && (c.keys.known(addr) || c.pairingAllowed(addr)) {
			go h.Cmd(opAcceptConnReq, acceptConnParams(ba))
		} else {
			log.Printf("[bt] refused a connection from unpaired %s", addr)
			go h.Cmd(opRejectConnReq, rejectConnParams(ba, hciUnacceptableAddr))
		}
	case evtDisconnectComplete:
		if len(pkt) < 7 || pkt[3] != 0 {
			return
		}
		handle := binary.LittleEndian.Uint16(pkt[4:6]) & 0x0FFF
		c.mu.Lock()
		l := c.links[handle]
		delete(c.links, handle)
		c.mu.Unlock()
		if l != nil {
			c.reasm.Drop(handle)
			c.linkEnded(l, pkt[6])
		}
	case evtNumCompletedPackets:
		c.completed(pkt)
	case evtLinkKeyRequest:
		addr, ok := eventAddr(pkt, 0)
		if !ok {
			return
		}
		ba := append([]byte(nil), pkt[3:9]...)
		if key, have := c.keys.key(addr); have {
			go h.Cmd(opLinkKeyReqReply, linkKeyReplyParams(ba, key))
		} else {
			go h.Cmd(opLinkKeyReqNegReply, ba)
		}
	case evtLinkKeyNotification:
		addr, key, typ, ok := parseLinkKeyNotification(pkt)
		if !ok {
			return
		}
		// A key for a device we already hold is a re-pair or key refresh by
		// that device and replaces the old one; any other address gets a key
		// only inside its pairing window.
		if c.pairingAllowed(addr) || c.keys.known(addr) {
			if err := c.keys.put(addr, key, typ, ""); err != nil {
				log.Printf("[bt] could not save the pairing with %s: %v", addr, err)
			}
		}
	case evtIOCapRequest:
		addr, ok := eventAddr(pkt, 0)
		if !ok {
			return
		}
		ba := append([]byte(nil), pkt[3:9]...)
		if c.pairingAllowed(addr) {
			go h.Cmd(opIOCapReqReply, ioCapReplyParams(ba))
		} else {
			go h.Cmd(opIOCapReqNegReply, append(ba, hciPairingNotAllowed))
		}
	case evtUserConfirmRequest:
		addr, ok := eventAddr(pkt, 0)
		if !ok {
			return
		}
		ba := append([]byte(nil), pkt[3:9]...)
		if c.pairingAllowed(addr) {
			go h.Cmd(opUserConfirmReqReply, ba)
		} else {
			go h.Cmd(opUserConfirmNegReply, ba)
		}
	case evtPinCodeRequest:
		addr, ok := eventAddr(pkt, 0)
		if !ok {
			return
		}
		ba := append([]byte(nil), pkt[3:9]...)
		if c.pairingAllowed(addr) {
			go h.Cmd(opPinCodeReqReply, pinReplyParams(ba, "0000"))
		} else {
			go h.Cmd(opPinCodeReqNegReply, ba)
		}
	case evtEncryptionChange:
		if st, handle, ok := eventHandleStatus(pkt); ok && len(pkt) >= 7 {
			if l := c.byHandle(handle); l != nil {
				l.mu.Lock()
				l.encrypted = st == 0 && pkt[6] != 0
				l.mu.Unlock()
			}
		}
	}
}

func (c *Classic) linkUp(cc classicConnComplete) {
	c.mu.Lock()
	if c.links == nil || c.links[cc.handle] != nil {
		c.mu.Unlock()
		return
	}
	l := &ClassicLink{Addr: cc.addr, handle: cc.handle, gone: make(chan struct{}), encrypted: cc.encrypt}
	l.sig = newL2Signal(func(cid uint16, pdu []byte) error { return c.send(l, cid, pdu) },
		func(psm uint16) uint16 { return c.acceptPSM(l, psm) },
		func(ch *L2Channel) { c.channelOpened(l, ch) },
		classicLocalMTU)
	c.links[cc.handle] = l
	c.mu.Unlock()
	log.Printf("[bt] link up %s (handle %d)", cc.addr, cc.handle)
}

func (c *Classic) linkEnded(l *ClassicLink, reason byte) {
	l.sig.linkDown()
	c.mu.Lock()
	sem := c.sem
	c.mu.Unlock()
	l.mu.Lock()
	// Packets the controller still held for this link are flushed without a
	// Number Of Completed Packets event.
	for ; l.unacked > 0; l.unacked-- {
		select {
		case <-sem:
		default:
		}
	}
	l.mu.Unlock()
	select {
	case <-l.gone:
		return
	default:
		close(l.gone)
	}
	log.Printf("[bt] link down %s (reason 0x%02x)", l.Addr, reason)
	if c.OnLinkDown != nil {
		c.OnLinkDown(l.Addr, reason)
	}
}

func (c *Classic) completed(pkt []byte) {
	if len(pkt) < 4 {
		return
	}
	c.mu.Lock()
	sem := c.sem
	c.mu.Unlock()
	body := pkt[4:]
	for i := 0; i < int(pkt[3]) && len(body) >= 4; i, body = i+1, body[4:] {
		l := c.byHandle(binary.LittleEndian.Uint16(body[0:2]) & 0x0FFF)
		if l == nil {
			continue
		}
		n := int(binary.LittleEndian.Uint16(body[2:4]))
		l.mu.Lock()
		for ; n > 0 && l.unacked > 0; n, l.unacked = n-1, l.unacked-1 {
			select {
			case <-sem:
			default:
			}
		}
		l.mu.Unlock()
	}
}

// acceptPSM decides an inbound channel. SDP needs no security (security mode
// 4, level 0); the audio profiles need an encrypted link, which a paired
// speaker has before it opens them.
func (c *Classic) acceptPSM(l *ClassicLink, psm uint16) uint16 {
	switch psm {
	case psmSDP:
		if c.SDP == nil {
			return connBadPSM
		}
		return connSuccess
	case psmAVDTP, psmAVCTP:
		if c.OnChannel == nil {
			return connBadPSM
		}
		if !l.Encrypted() {
			return connSecurity
		}
		return connSuccess
	}
	return connBadPSM
}

func (c *Classic) channelOpened(l *ClassicLink, ch *L2Channel) {
	if ch.PSM == psmSDP {
		serve := c.SDP
		ch.SetOnData(func(req []byte) {
			if serve == nil {
				return
			}
			// The response may be at most the peer's MTU; the server splits
			// a longer one with a continuation state.
			if rsp := serve(req, ch.MTU()); rsp != nil {
				_ = ch.Send(rsp)
			}
		})
		return
	}
	if c.OnChannel != nil {
		c.OnChannel(l, ch)
	}
}

// send writes one L2CAP PDU on a BR/EDR link against the shared controller
// buffers. BR/EDR starts a PDU with PB=10 (automatically flushable), not
// LE's 00 (Vol 4 Part E §5.4.2).
func (c *Classic) send(l *ClassicLink, cid uint16, pdu []byte) error {
	c.mu.Lock()
	h, sem, aclLen := c.host, c.sem, c.aclLen
	c.mu.Unlock()
	if h == nil {
		return ErrClassicNotRunning
	}
	for _, pkt := range fragmentACLPB(l.handle, cid, pdu, aclLen, 0x2) {
		t := time.NewTimer(aclCreditTimeout)
		select {
		case sem <- struct{}{}:
			t.Stop()
		case <-l.gone:
			t.Stop()
			return ErrDisconnected
		case <-t.C:
			return fmt.Errorf("bluetooth: controller has had no free buffer for %s", aclCreditTimeout)
		}
		l.mu.Lock()
		l.unacked++
		l.mu.Unlock()
		if err := h.write(pkt); err != nil {
			return err
		}
	}
	return nil
}

// cmdAndWait sends a command and waits for the first event match accepts.
// The waiter is registered first so a fast answer cannot be missed.
func (c *Classic) cmdAndWait(h *hciHost, op uint16, params []byte, match func([]byte) bool, timeout time.Duration) ([]byte, error) {
	w := c.await(match, 4)
	defer c.unwait(w)
	if _, err := h.Cmd(op, params); err != nil {
		return nil, err
	}
	t := time.NewTimer(timeout)
	defer t.Stop()
	select {
	case pkt := <-w.ch:
		return pkt, nil
	case <-h.dead:
		return nil, ErrClassicNotRunning
	case <-t.C:
		return nil, fmt.Errorf("bluetooth: no answer to command %04x in %s", op, timeout)
	}
}

// Connect pages addr and returns the link, secured with the stored key.
func (c *Classic) Connect(addr string) (*ClassicLink, error) {
	addr, ok := normaliseAddr(addr)
	if !ok {
		return nil, fmt.Errorf("bluetooth: bad address")
	}
	if !c.keys.known(addr) && !c.pairingAllowed(addr) {
		return nil, ErrNotPaired
	}
	l, err := c.page(addr)
	if err != nil {
		return nil, err
	}
	if err := c.secure(l); err != nil {
		return nil, err
	}
	return l, nil
}

func (c *Classic) page(addr string) (*ClassicLink, error) {
	if l := c.byAddr(addr); l != nil {
		return l, nil
	}
	h, err := c.running()
	if err != nil {
		return nil, err
	}
	ba, err := bdaddrLE(addr)
	if err != nil {
		return nil, err
	}
	isOurs := func(p []byte) bool {
		cc, ok := parseClassicConnComplete(p)
		return ok && cc.addr == addr
	}
	pkt, err := c.cmdAndWait(h, opCreateConn, createConnParamsBREDR(ba, 0), isOurs, classicConnectTimeout)
	if err != nil {
		// The speaker may have connected to us meanwhile (Connection Already
		// Exists comes back as the command's status).
		if l := c.byAddr(addr); l != nil {
			return l, nil
		}
		return nil, err
	}
	cc, _ := parseClassicConnComplete(pkt)
	if cc.status == hciConnExists {
		if l := c.byAddr(addr); l != nil {
			return l, nil
		}
	}
	if cc.status != 0 {
		return nil, fmt.Errorf("bluetooth: could not reach %s (hci 0x%02x)", addr, cc.status)
	}
	if l := c.byHandle(cc.handle); l != nil {
		return l, nil
	}
	return nil, ErrDisconnected
}

// secure authenticates the link (the stored key, or pairing when the window
// is open) and then encrypts it.
func (c *Classic) secure(l *ClassicLink) error {
	if l.Encrypted() {
		return nil
	}
	h, err := c.running()
	if err != nil {
		return err
	}
	forHandle := func(code byte) func([]byte) bool {
		return func(p []byte) bool {
			_, hd, ok := eventHandleStatus(p)
			return ok && p[1] == code && hd == l.handle
		}
	}
	pkt, err := c.cmdAndWait(h, opAuthRequested, handleParams(l.handle), forHandle(evtAuthComplete), classicSecureTimeout)
	if err != nil {
		return err
	}
	switch st := pkt[3]; {
	case st == hciKeyMissing && !c.pairingAllowed(l.Addr):
		return ErrKeyMissing
	case st != 0:
		return fmt.Errorf("%w (hci 0x%02x)", ErrPairingRefused, st)
	}
	pkt, err = c.cmdAndWait(h, opSetConnEncryption, append(handleParams(l.handle), 0x01), forHandle(evtEncryptionChange), classicSecureTimeout)
	if err != nil {
		return err
	}
	if pkt[3] != 0 || len(pkt) < 7 || pkt[6] == 0 {
		return fmt.Errorf("bluetooth: encryption refused (hci 0x%02x)", pkt[3])
	}
	l.mu.Lock()
	l.encrypted = true
	l.mu.Unlock()
	return nil
}

// Pair pairs with addr: page it, run Secure Simple Pairing (or PIN 0000),
// and keep the key. The speaker must be in pairing mode. The link is left up
// for the profiles.
func (c *Classic) Pair(addr, name string) (*ClassicLink, error) {
	addr, ok := normaliseAddr(addr)
	if !ok {
		return nil, fmt.Errorf("bluetooth: bad address")
	}
	c.Hold(true)
	defer c.Hold(false)
	c.mu.Lock()
	c.pairAddr, c.pairUntil = addr, time.Now().Add(pairWindow)
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		c.pairAddr = ""
		c.mu.Unlock()
	}()
	// An old key would be offered first and fail; a pairing replaces it.
	_ = c.keys.forget(addr)
	l, err := c.page(addr)
	if err != nil {
		return nil, err
	}
	if err := c.secure(l); err != nil {
		return nil, err
	}
	if !c.keys.known(addr) {
		return nil, fmt.Errorf("%w: no link key was issued", ErrPairingRefused)
	}
	if name != "" {
		_ = c.keys.setName(addr, name)
	}
	c.updateScanEnable()
	log.Printf("[bt] paired with %s", addr)
	return l, nil
}

// Forget drops a pairing and any link to it.
func (c *Classic) Forget(addr string) error {
	addr, ok := normaliseAddr(addr)
	if !ok {
		return fmt.Errorf("bluetooth: bad address")
	}
	if l := c.byAddr(addr); l != nil {
		if h, err := c.running(); err == nil {
			_, _ = h.Cmd(opDisconnect, append(handleParams(l.handle), hciReasonRemote))
		}
	}
	err := c.keys.forget(addr)
	c.updateScanEnable()
	return err
}

// Discover runs a general inquiry for about d and returns the audio sinks
// found, named where the device told us or answered a name request.
func (c *Classic) Discover(d time.Duration) ([]InquiryResult, error) {
	h, err := c.running()
	if err != nil {
		return nil, err
	}
	c.Hold(true)
	defer c.Hold(false)
	units := byte(d / (1280 * time.Millisecond))
	if units < 1 {
		units = 1
	}
	if units > 0x30 {
		units = 0x30
	}
	w := c.await(func(p []byte) bool {
		switch p[1] {
		case evtInquiryResult, evtInquiryResultRSSI, evtExtInquiryResult, evtInquiryComplete:
			return true
		}
		return false
	}, 128)
	defer c.unwait(w)
	if _, err := h.Cmd(opInquiry, inquiryParams(units)); err != nil {
		return nil, err
	}
	found := map[string]InquiryResult{}
	deadline := time.NewTimer(time.Duration(units)*1280*time.Millisecond + 3*time.Second)
	defer deadline.Stop()
collect:
	for {
		select {
		case p := <-w.ch:
			if p[1] == evtInquiryComplete {
				break collect
			}
			for _, r := range parseInquiryResults(p) {
				if old, ok := found[r.Addr]; ok && r.Name == "" {
					r.Name = old.Name
				}
				found[r.Addr] = r
			}
		case <-h.dead:
			return nil, ErrClassicNotRunning
		case <-deadline.C:
			_, _ = h.Cmd(opInquiryCancel, nil)
			break collect
		}
	}
	var out []InquiryResult
	for _, r := range found {
		if !r.IsAudioSink() {
			continue
		}
		if r.Name == "" {
			r.Name = c.remoteName(h, r)
		}
		out = append(out, r)
	}
	return out, nil
}

func (c *Classic) remoteName(h *hciHost, r InquiryResult) string {
	ba, err := bdaddrLE(r.Addr)
	if err != nil {
		return ""
	}
	off := r.clockOffset
	if off != 0 {
		off |= 0x8000 // valid
	}
	params := append(ba, 0x01, 0x00, byte(off), byte(off>>8))
	pkt, err := c.cmdAndWait(h, opRemoteNameReq, params, func(p []byte) bool {
		addr, ok := eventAddr(p, 1)
		return ok && p[1] == evtRemoteNameComplete && addr == r.Addr
	}, remoteNameTimeout)
	if err != nil {
		return ""
	}
	_, name, _ := parseRemoteName(pkt)
	return name
}
