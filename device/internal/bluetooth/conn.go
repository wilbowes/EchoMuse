package bluetooth

// LE connections for the active proxy (#656): a central that holds up to
// three links and speaks ATT over each as a client.
//
// It lives inside a scanner session — the session owns /dev/stpbt and hands
// this its hciHost — so a session that ends (proxy switched off, watchdog
// re-init) ends every link with it, and says so through OnDisconnect.
//
// What a v1 leaves out, each answered on the wire so a peer is never left
// waiting: pairing (SMP Security Request → Pairing Not Supported), a peer's
// connection-parameter update (rejected; the interval we asked for stands),
// and serving attributes of our own (ATT requests → Request Not Supported).
//
// Measured on the Dot's MT6627, 2026-10-03: three links held at once, the
// connection interval honoured (it ignores scan interval and window), and
// 3–11% of WiFi frames resent while a link is held against 155% for the scan.

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"net"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// MaxConnSlots is how many links one Echo holds. Three is what the chip
	// has been shown to hold, and ESPHome's own default on an ESP32.
	MaxConnSlots = 3

	// attClientMTU is the receive MTU we offer. The chip is Bluetooth 4.0, so
	// anything over 23 is carried as several link-layer packets; this is the
	// largest value that still fits one packet where a peer has 4.2's longer
	// ones, which is what most peers ask for.
	attClientMTU = 247

	// Two connection intervals, because the two things a link costs pull
	// opposite ways (measured on a Dot, 2026-10-03). A request takes one to
	// two intervals, so discovery wants a short one: 2s at 30ms, 16s at
	// 200ms. But the scan shares the radio with the link, and at 30ms it
	// caught 24% of the adverts it catches alone, against 54% at 200ms and
	// 65% at 1s — which is Bermuda's presence data. So a link runs fast while
	// it is being used and is moved to the slow interval once it goes quiet.
	// WiFi does not enter into it: resends were the same at either.
	//
	// Speeding up again is not instant — the controller schedules an update
	// six intervals ahead, 3s at 500ms — so the first requests after an idle
	// spell go at the slow rate while it takes effect.
	leConnIntervalMs     = 30
	leConnIdleIntervalMs = 500
	leConnIdleAfter      = 5 * time.Second
	leConnectTimeout     = 20 * time.Second
	attTxnTimeout        = 30 * time.Second // Vol 3 Part F 3.3.3
	aclCreditTimeout     = 5 * time.Second

	attMaxValueLen = 512 // Vol 3 Part F 3.2.9

	hciReasonLocalHost = 0x16
	hciReasonRemote    = 0x13
)

var (
	ErrNotRunning       = errors.New("ble: the proxy is not running")
	ErrNoSlots          = errors.New("ble: no free connection slot")
	ErrAlreadyConnected = errors.New("ble: already connected to that address")
	ErrNotConnected     = errors.New("ble: not connected to that address")
	ErrConnectTimeout   = errors.New("ble: the peer did not answer")
	ErrDisconnected     = errors.New("ble: the link dropped")
	ErrATTTimeout       = errors.New("ble: the peer did not answer an ATT request in 30s")
	ErrTooLong          = errors.New("ble: value longer than one ATT write carries")
)

// ConnInfo describes a link that has come up.
type ConnInfo struct {
	Addr       string
	Handle     uint16
	MTU        int
	IntervalMs float64
}

type leConn struct {
	addr       string
	handle     uint16
	intervalMs float64

	txn  sync.Mutex    // one ATT request outstanding per link
	rsp  chan []byte   // its response
	gone chan struct{} // closed when the link is down

	// under ConnManager.mu
	mtu      int
	unacked  int
	services []Service
	lastUse  time.Time // the last ATT request of ours
	fast     bool      // the interval the link is running at
	wantFast bool      // the one it should be
	updating bool      // an LE Connection Update is in flight
	askedFor bool      // what that update asked for
}

// ConnManager holds the links. The zero value is not usable; a Scanner makes
// its own and exposes it through Conns.
type ConnManager struct {
	// OnNotify receives every notification and indication. OnDisconnect is
	// called once per link that ends, whoever ended it. Both run on the
	// manager's reader and must not block.
	OnNotify     func(addr string, handle uint16, value []byte, indication bool)
	OnDisconnect func(addr string, reason byte)

	connectMu sync.Mutex // one Create Connection at a time

	mu          sync.Mutex
	host        *hciHost
	stop        chan struct{}
	conns       map[uint16]*leConn
	pending     chan connComplete
	pendingAddr string
	sem         chan struct{} // the controller's ACL buffers
	aclLen      int
	ownAddr     []byte // random static address, LE order; nil uses the public one
	ownRandom   bool   // the controller accepted ownAddr

	hold func(bool) // pauses the scan while a connection is being made

	connectTimeout time.Duration
	attTimeout     time.Duration
	idleAfter      time.Duration
}

func newConnManager(hold func(bool)) *ConnManager {
	return &ConnManager{
		hold:           hold,
		connectTimeout: leConnectTimeout,
		attTimeout:     attTxnTimeout,
		idleAfter:      leConnIdleAfter,
	}
}

// StaticRandomAddr derives a random static address (Vol 6 Part B 1.3.2.1)
// from a seed, in the byte order HCI wants. Every Dot checked reports the
// same public address, an NVRAM default, so two Echoes connecting to one
// peripheral would be indistinguishable to it; a per-device address fixes
// that and stays the same across reboots.
func StaticRandomAddr(seed string) []byte {
	sum := sha256.Sum256([]byte("echomuse-ble-addr\x00" + seed))
	addr := append([]byte(nil), sum[:6]...)
	addr[5] |= 0xC0 // the two most significant bits mark it static
	// The other 46 bits may be neither all zero nor all one.
	zero, one := true, true
	for i, b := range addr {
		if i == 5 {
			b &= 0x3F
			one = one && b == 0x3F
		} else {
			one = one && b == 0xFF
		}
		zero = zero && b == 0
	}
	if zero || one {
		addr[0] ^= 0x01
	}
	return addr
}

// SetOwnAddress makes connections use this random static address. It takes
// effect at the next session start.
func (m *ConnManager) SetOwnAddress(addr []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.ownAddr = append([]byte(nil), addr...)
}

// attach starts serving links on a freshly reset controller.
func (m *ConnManager) attach(h *hciHost) {
	aclLen, aclNum := 27, 1
	if cc, err := h.Cmd(opLEReadBufferSize, nil); err == nil && len(cc.params) >= 3 &&
		binary.LittleEndian.Uint16(cc.params[0:2]) != 0 && cc.params[2] != 0 {
		aclLen, aclNum = int(binary.LittleEndian.Uint16(cc.params[0:2])), int(cc.params[2])
	} else if cc, err := h.Cmd(opReadBufferSize, nil); err == nil && len(cc.params) >= 5 {
		// No LE buffers of its own: LE shares the BR/EDR ones (the MT6627).
		if l, n := int(binary.LittleEndian.Uint16(cc.params[0:2])), int(binary.LittleEndian.Uint16(cc.params[3:5])); l > 0 && n > 0 {
			aclLen, aclNum = l, n
		}
	}
	m.mu.Lock()
	own := m.ownAddr
	m.mu.Unlock()
	random := false
	if len(own) == 6 {
		if _, err := h.Cmd(opLESetRandomAddr, own); err != nil {
			log.Printf("[ble] random address refused, using the public one: %v", err)
		} else {
			random = true
		}
	}
	m.mu.Lock()
	m.host, m.stop = h, make(chan struct{})
	m.conns = map[uint16]*leConn{}
	m.sem = make(chan struct{}, aclNum)
	m.aclLen, m.ownRandom = aclLen, random
	stop := m.stop
	m.mu.Unlock()
	go m.run(h, stop)
}

// detach ends every link: the session that carried them is over.
func (m *ConnManager) detach() {
	m.mu.Lock()
	if m.host == nil {
		m.mu.Unlock()
		return
	}
	close(m.stop)
	m.host = nil
	conns := m.conns
	m.conns = nil
	m.mu.Unlock()
	for _, c := range conns {
		close(c.gone)
		if m.OnDisconnect != nil {
			m.OnDisconnect(c.addr, hciReasonLocalHost)
		}
	}
}

// Connected lists the addresses of the links that are up, sorted.
func (m *ConnManager) Connected() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]string, 0, len(m.conns))
	for _, c := range m.conns {
		out = append(out, c.addr)
	}
	sort.Strings(out)
	return out
}

// normaliseAddr puts an address in the one spelling the manager keys on.
func normaliseAddr(addr string) (string, bool) {
	mac, err := net.ParseMAC(addr)
	if err != nil || len(mac) != 6 {
		return "", false
	}
	return mac.String(), true
}

// Slots reports free and total connection slots.
func (m *ConnManager) Slots() (free, limit int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.host == nil {
		return 0, MaxConnSlots
	}
	return MaxConnSlots - len(m.conns), MaxConnSlots
}

func (m *ConnManager) run(h *hciHost, stop chan struct{}) {
	var reasm aclReassembler
	period := m.idleAfter / 2
	if period > time.Second {
		period = time.Second
	}
	idle := time.NewTicker(period)
	defer idle.Stop()
	for {
		select {
		case pkt := <-h.link:
			m.handle(h, &reasm, pkt)
		case now := <-idle.C:
			m.mu.Lock()
			for _, c := range m.conns {
				if c.wantFast && now.Sub(c.lastUse) >= m.idleAfter {
					c.wantFast = false
					go m.applyInterval(h, c)
				}
			}
			m.mu.Unlock()
		case <-h.dead:
			return
		case <-stop:
			return
		}
	}
}

func (m *ConnManager) byHandle(handle uint16) *leConn {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.conns[handle]
}

func (m *ConnManager) byAddr(addr string) *leConn {
	addr = strings.ToLower(addr)
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, c := range m.conns {
		if c.addr == addr {
			return c
		}
	}
	return nil
}

func (m *ConnManager) handle(h *hciHost, reasm *aclReassembler, pkt []byte) {
	if len(pkt) == 0 {
		return
	}
	if pkt[0] == h4TypeACL {
		if handle, cid, payload, ok := reasm.Feed(pkt); ok {
			if c := m.byHandle(handle); c != nil {
				m.onL2CAP(h, c, cid, payload)
			}
		}
		return
	}
	if len(pkt) < 3 || pkt[0] != h4TypeEvent {
		return
	}
	switch pkt[1] {
	case evtDisconnectComplete:
		// status(1), handle(2), reason(1)
		if len(pkt) < 7 || pkt[3] != 0 {
			return
		}
		handle := binary.LittleEndian.Uint16(pkt[4:6]) & 0x0FFF
		reasm.Drop(handle)
		m.mu.Lock()
		c := m.conns[handle]
		if c != nil {
			delete(m.conns, handle)
			// Packets the controller still held for this link are flushed
			// without a Number Of Completed Packets event.
			for ; c.unacked > 0; c.unacked-- {
				select {
				case <-m.sem:
				default:
				}
			}
		}
		m.mu.Unlock()
		if c != nil {
			close(c.gone)
			log.Printf("[ble] %s disconnected (reason 0x%02x)", c.addr, pkt[6])
			if m.OnDisconnect != nil {
				m.OnDisconnect(c.addr, pkt[6])
			}
		}
	case evtNumCompletedPackets:
		// count(1), then handle(2) + completed(2) each
		if len(pkt) < 4 {
			return
		}
		body := pkt[4:]
		m.mu.Lock()
		for i := 0; i < int(pkt[3]) && len(body) >= 4; i, body = i+1, body[4:] {
			c := m.conns[binary.LittleEndian.Uint16(body[0:2])&0x0FFF]
			n := int(binary.LittleEndian.Uint16(body[2:4]))
			for ; n > 0 && c != nil && c.unacked > 0; n, c.unacked = n-1, c.unacked-1 {
				select {
				case <-m.sem:
				default:
				}
			}
		}
		m.mu.Unlock()
	case evtLEMeta:
		if cc, ok := parseConnComplete(pkt); ok {
			m.onConnComplete(h, cc)
			return
		}
		if len(pkt) >= 13 && pkt[3] == leSubeventConnUpdateDone {
			// status(1), handle(2), interval(2), latency(2), timeout(2)
			c := m.byHandle(binary.LittleEndian.Uint16(pkt[5:7]) & 0x0FFF)
			if c == nil {
				return
			}
			m.mu.Lock()
			if pkt[4] == 0 {
				c.intervalMs = float64(binary.LittleEndian.Uint16(pkt[7:9])) * 1.25
				c.fast = c.askedFor
			}
			c.updating = false
			again := c.wantFast != c.fast && pkt[4] == 0
			interval := c.intervalMs
			m.mu.Unlock()
			log.Printf("[ble] %s connection update status 0x%02x, interval %.0fms", c.addr, pkt[4], interval)
			if again {
				// What was wanted changed while this one was in flight.
				go m.applyInterval(h, c)
			}
		}
	}
}

func (m *ConnManager) onConnComplete(h *hciHost, cc connComplete) {
	m.mu.Lock()
	pending, addr := m.pending, m.pendingAddr
	m.pending = nil
	if pending != nil && cc.status == 0 && m.conns != nil {
		// Registered here, on the reader, so the peer's first packets find it.
		m.conns[cc.handle] = &leConn{
			addr: addr, handle: cc.handle, intervalMs: cc.intervalMs, mtu: attDefaultMTU,
			rsp: make(chan []byte, 1), gone: make(chan struct{}),
			lastUse: time.Now(), fast: true, wantFast: true,
		}
	}
	m.mu.Unlock()
	if pending != nil {
		pending <- cc
		return
	}
	if cc.status == 0 {
		// A link nobody is waiting for: a connect that timed out and then
		// completed anyway. Holding it would leak a slot at the peer.
		go h.Cmd(opDisconnect, []byte{byte(cc.handle), byte(cc.handle >> 8), hciReasonRemote})
	}
}

func (m *ConnManager) onL2CAP(h *hciHost, c *leConn, cid uint16, p []byte) {
	if len(p) == 0 {
		return
	}
	// Off the reader: send waits for a controller buffer, and the event that
	// frees one arrives on this goroutine.
	reply := func(cid uint16, pdu []byte) {
		go func() {
			if err := m.send(h, c, cid, pdu); err != nil && !errors.Is(err, ErrDisconnected) {
				log.Printf("[ble] %s: reply on cid %d: %v", c.addr, cid, err)
			}
		}()
	}
	switch cid {
	case cidATT:
		op := p[0]
		switch {
		case op == attOpNotification || op == attOpIndication:
			handle, value, indication, ok := parseHandleValue(p)
			if !ok {
				return
			}
			if indication {
				reply(cidATT, []byte{attOpConfirmation})
			}
			if m.OnNotify != nil {
				m.OnNotify(c.addr, handle, append([]byte(nil), value...), indication)
			}
		case op == attOpMTUReq:
			// The peer's half of the exchange: answer with ours, and take
			// the smaller of the two.
			if len(p) == 3 {
				m.setMTU(c, binary.LittleEndian.Uint16(p[1:3]))
			}
			reply(cidATT, []byte{attOpMTURsp, byte(attClientMTU), byte(attClientMTU >> 8)})
		case op == attOpError || op&0x01 == 1:
			select {
			case c.rsp <- append([]byte(nil), p...):
			default: // nobody asked
			}
		case op&0x40 == 0:
			// A request for attributes we do not have.
			var handle uint16
			if len(p) >= 3 {
				handle = binary.LittleEndian.Uint16(p[1:3])
			}
			reply(cidATT, attErrorRsp(op, handle, attErrReqNotSupport))
		}
	case cidLESig:
		if len(p) < 4 {
			return
		}
		switch p[0] {
		case 0x12: // Connection Parameter Update Request → rejected
			reply(cidLESig, []byte{0x13, p[1], 2, 0, 1, 0})
		case 0x01, 0x13: // a reject or a response; nothing owed
		default: // Command Reject: not understood
			reply(cidLESig, []byte{0x01, p[1], 2, 0, 0, 0})
		}
	case cidSMP:
		if p[0] == 0x0B { // Security Request → Pairing Failed: Not Supported
			reply(cidSMP, []byte{0x05, 0x05})
		}
	}
}

// applyInterval moves a link to the interval it should be running at. One
// update at a time per link; a change of mind while one is in flight is
// picked up when it completes.
func (m *ConnManager) applyInterval(h *hciHost, c *leConn) {
	m.mu.Lock()
	if c.updating || c.wantFast == c.fast || m.conns[c.handle] != c {
		m.mu.Unlock()
		return
	}
	c.updating, c.askedFor = true, c.wantFast
	ms := leConnIdleIntervalMs
	if c.wantFast {
		ms = leConnIntervalMs
	}
	m.mu.Unlock()
	if _, err := h.Cmd(opLEConnUpdate, connUpdateParams(c.handle, ms)); err != nil {
		m.mu.Lock()
		c.updating = false
		m.mu.Unlock()
		select {
		case <-c.gone:
		default:
			log.Printf("[ble] %s: connection update to %dms: %v", c.addr, ms, err)
		}
	}
}

// setMTU records the peer's receive MTU; the link runs at the smaller of the
// two sides', and never below the default.
func (m *ConnManager) setMTU(c *leConn, peer uint16) {
	mtu := int(peer)
	if mtu > attClientMTU {
		mtu = attClientMTU
	}
	if mtu < attDefaultMTU {
		mtu = attDefaultMTU
	}
	m.mu.Lock()
	c.mtu = mtu
	m.mu.Unlock()
}

// send writes one L2CAP PDU, fragmented to the controller's buffer size and
// held back while the controller has no free buffer.
func (m *ConnManager) send(h *hciHost, c *leConn, cid uint16, pdu []byte) error {
	m.mu.Lock()
	sem, aclLen := m.sem, m.aclLen
	m.mu.Unlock()
	for _, pkt := range fragmentACL(c.handle, cid, pdu, aclLen) {
		t := time.NewTimer(aclCreditTimeout)
		select {
		case sem <- struct{}{}:
			t.Stop()
		case <-c.gone:
			t.Stop()
			return ErrDisconnected
		case <-t.C:
			return fmt.Errorf("ble: controller has had no free buffer for %s", aclCreditTimeout)
		}
		m.mu.Lock()
		c.unacked++
		m.mu.Unlock()
		if err := h.write(pkt); err != nil {
			return err
		}
	}
	return nil
}

// request runs one ATT transaction and returns the peer's PDU, which may be
// an Error Response. A peer that does not answer in 30s has broken the
// bearer, and the spec's answer is to drop the link.
func (m *ConnManager) request(c *leConn, req []byte) ([]byte, error) {
	m.mu.Lock()
	h := m.host
	m.mu.Unlock()
	if h == nil {
		return nil, ErrNotRunning
	}
	c.txn.Lock()
	defer c.txn.Unlock()
	m.mu.Lock()
	c.lastUse = time.Now()
	speedUp := !c.wantFast
	c.wantFast = true
	m.mu.Unlock()
	if speedUp {
		go m.applyInterval(h, c)
	}
	select {
	case <-c.rsp: // a late answer to an abandoned request
	default:
	}
	if err := m.send(h, c, cidATT, req); err != nil {
		return nil, err
	}
	t := time.NewTimer(m.attTimeout)
	defer t.Stop()
	select {
	case rsp := <-c.rsp:
		return rsp, nil
	case <-c.gone:
		return nil, ErrDisconnected
	case <-t.C:
		go h.Cmd(opDisconnect, []byte{byte(c.handle), byte(c.handle >> 8), hciReasonRemote})
		return nil, ErrATTTimeout
	}
}

// connRequester puts DiscoverServices on a link.
type connRequester struct {
	m *ConnManager
	c *leConn
}

func (r connRequester) Request(req []byte) ([]byte, error) { return r.m.request(r.c, req) }

// Connect brings up a link to addr ("aa:bb:cc:dd:ee:ff"; addrType 0 public,
// 1 random) and exchanges MTU.
func (m *ConnManager) Connect(addr string, addrType int) (ConnInfo, error) {
	mac, err := net.ParseMAC(addr)
	if err != nil || len(mac) != 6 || addrType < 0 || addrType > 1 {
		return ConnInfo{}, fmt.Errorf("ble: bad address %q type %d", addr, addrType)
	}
	addr = mac.String()

	m.connectMu.Lock()
	defer m.connectMu.Unlock()

	pending := make(chan connComplete, 1)
	m.mu.Lock()
	h := m.host
	if h == nil {
		m.mu.Unlock()
		return ConnInfo{}, ErrNotRunning
	}
	// Before the slot count: with every slot taken, "already connected" is
	// the more useful answer about an address that holds one of them.
	for _, c := range m.conns {
		if c.addr == addr {
			m.mu.Unlock()
			return ConnInfo{}, ErrAlreadyConnected
		}
	}
	if len(m.conns) >= MaxConnSlots {
		m.mu.Unlock()
		return ConnInfo{}, ErrNoSlots
	}
	m.pending, m.pendingAddr = pending, addr
	ownType := 0
	if m.ownRandom {
		ownType = 1
	}
	m.mu.Unlock()
	forget := func() {
		m.mu.Lock()
		if m.pending == pending {
			m.pending = nil
		}
		m.mu.Unlock()
	}

	if m.hold != nil {
		m.hold(true)
		defer m.hold(false)
	}
	if _, err := h.Cmd(opLECreateConn, createConnParams(mac, addrType, ownType, leConnIntervalMs)); err != nil {
		forget()
		return ConnInfo{}, err
	}
	var cc connComplete
	t := time.NewTimer(m.connectTimeout)
	defer t.Stop()
	select {
	case cc = <-pending:
	case <-h.dead:
		forget()
		return ConnInfo{}, ErrNotRunning
	case <-t.C:
		// The cancel is answered by a Connection Complete: status 0x02 if it
		// won, or a real connection if the peer answered at the same moment.
		if _, err := h.Cmd(opLECreateConnCancel, nil); err != nil {
			forget()
			return ConnInfo{}, ErrConnectTimeout
		}
		select {
		case cc = <-pending:
		case <-time.After(cmdTimeout):
			forget()
			return ConnInfo{}, ErrConnectTimeout
		}
	}
	if cc.status == 0x02 {
		return ConnInfo{}, ErrConnectTimeout
	}
	if cc.status != 0 {
		return ConnInfo{}, fmt.Errorf("ble: connect to %s failed, hci status 0x%02x", addr, cc.status)
	}
	c := m.byHandle(cc.handle)
	if c == nil {
		return ConnInfo{}, ErrDisconnected
	}

	// Exchange MTU. A peer that does not support it stays at the default.
	rsp, err := m.request(c, encodeMTUReq(attClientMTU))
	if err != nil {
		m.Disconnect(addr)
		return ConnInfo{}, err
	}
	if mtu, err := parseMTURsp(rsp); err == nil {
		m.setMTU(c, mtu)
	}
	m.mu.Lock()
	info := ConnInfo{Addr: addr, Handle: c.handle, MTU: c.mtu, IntervalMs: c.intervalMs}
	m.mu.Unlock()
	log.Printf("[ble] connected %s (handle %d, mtu %d, interval %.1fms)", addr, info.Handle, info.MTU, info.IntervalMs)
	return info, nil
}

// Disconnect ends the link to addr and waits for the controller to say so.
func (m *ConnManager) Disconnect(addr string) error {
	c := m.byAddr(addr)
	if c == nil {
		return ErrNotConnected
	}
	m.mu.Lock()
	h := m.host
	m.mu.Unlock()
	if h == nil {
		return ErrNotRunning
	}
	if _, err := h.Cmd(opDisconnect, []byte{byte(c.handle), byte(c.handle >> 8), hciReasonRemote}); err != nil {
		return err
	}
	select {
	case <-c.gone:
		return nil
	case <-time.After(cmdTimeout):
		return fmt.Errorf("ble: no disconnection event for %s", addr)
	}
}

// Services returns the peer's attribute table, discovered once per link.
func (m *ConnManager) Services(addr string) ([]Service, error) {
	c := m.byAddr(addr)
	if c == nil {
		return nil, ErrNotConnected
	}
	m.mu.Lock()
	cached := c.services
	m.mu.Unlock()
	if cached != nil {
		return cached, nil
	}
	services, err := DiscoverServices(connRequester{m, c})
	if err != nil {
		return nil, err
	}
	if services == nil {
		services = []Service{}
	}
	m.mu.Lock()
	c.services = services
	m.mu.Unlock()
	return services, nil
}

// Read returns an attribute's whole value, following a value longer than one
// response with Read Blob.
func (m *ConnManager) Read(addr string, handle uint16) ([]byte, error) {
	c := m.byAddr(addr)
	if c == nil {
		return nil, ErrNotConnected
	}
	rsp, err := m.request(c, encodeReadReq(handle))
	if err != nil {
		return nil, err
	}
	part, err := parseReadRsp(attOpReadReq, rsp)
	if err != nil {
		return nil, err
	}
	value := append([]byte(nil), part...)
	m.mu.Lock()
	full := c.mtu - 1
	m.mu.Unlock()
	// A response that fills the MTU may have been cut short.
	for len(part) == full && len(value) < attMaxValueLen {
		if rsp, err = m.request(c, encodeReadBlobReq(handle, uint16(len(value)))); err != nil {
			return nil, err
		}
		if part, err = parseReadRsp(attOpReadBlobReq, rsp); err != nil {
			// Attribute Not Long (0x0B) or Invalid Offset (0x07): the first
			// response was the whole value after all.
			var e *ATTError
			if errors.As(err, &e) && (e.Code == 0x0B || e.Code == 0x07) {
				break
			}
			return nil, err
		}
		value = append(value, part...)
	}
	return value, nil
}

// Write sets an attribute's value: with a response the peer confirms or
// refuses it, without one nothing comes back.
func (m *ConnManager) Write(addr string, handle uint16, value []byte, withResponse bool) error {
	c := m.byAddr(addr)
	if c == nil {
		return ErrNotConnected
	}
	m.mu.Lock()
	mtu, h := c.mtu, m.host
	m.mu.Unlock()
	if len(value) > mtu-3 {
		return ErrTooLong
	}
	if !withResponse {
		if h == nil {
			return ErrNotRunning
		}
		return m.send(h, c, cidATT, encodeWriteCmd(handle, value))
	}
	rsp, err := m.request(c, encodeWriteReq(handle, value))
	if err != nil {
		return err
	}
	return parseWriteRsp(rsp)
}
