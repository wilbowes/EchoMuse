//go:build bench

package bluetooth

// Bench-only GATT feasibility probe for tools/ble_probe (#656, step 0). It
// answers what has to be known before an active proxy is built: whether the
// MT6627 holds an LE connection driven from raw HCI at all, how many at once,
// whether it honours the connection interval (it ignores the scan interval
// and window), and — read AP-side while this holds a link — what a connection
// costs WiFi.
//
// One goroutine owns the session. Per connection it exchanges MTU, reads
// Device Name (0x2A00) by type, then holds: idle, or re-reading the name
// every ReadEvery to put traffic on the link and time the round trip. A read
// takes one to two connection intervals, so its timing shows the interval the
// chip is really using.
//
// It is a client only. Anything the peer asks of us is refused by the spec's
// own means (ATT Error Response, L2CAP parameter update rejected, SMP Pairing
// Failed), so no peer is left waiting out a 30s transaction timeout.

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"strings"
	"time"
)

const (
	opReadLocalVersion = 0x04<<10 | 0x0001
	opLEReadFeatures   = 0x08<<10 | 0x0003
	opLEReadWhiteList  = 0x08<<10 | 0x000F
	opLEReadStates     = 0x08<<10 | 0x001C

	attMTUMin = 23

	attError         = 0x01
	attMTUReq        = 0x02
	attMTURsp        = 0x03
	attReadByTypeReq = 0x08
	attReadByTypeRsp = 0x09
	attReadReq       = 0x0A
	attReadRsp       = 0x0B
	attIndication    = 0x1D
	attConfirmation  = 0x1E

	connectTimeout = 20 * time.Second
	attTimeout     = 30 * time.Second // ATT transaction timeout, Vol 3 Part F 3.3.3
)

// GattTarget is one peer to connect to.
type GattTarget struct {
	Addr     string
	AddrType int // 0 public, 1 random
}

// GattOptions describe one probe run.
type GattOptions struct {
	Targets        []GattTarget
	ConnIntervalMs int
	Hold           time.Duration
	ReadEvery      time.Duration // 0 holds the links idle
	Scan           bool          // passive scan while holding, as the proxy would
	Discover       bool          // walk the peer's table with DiscoverServices and read what is readable
	Notify         bool          // with Discover: subscribe to every notifying characteristic
	Write          string        // with Discover: "uuid-substring:hex", written with response
	Logf           func(format string, args ...any)
}

// GattConn is what happened on one connection.
type GattConn struct {
	Addr          string    `json:"addr"`
	ConnectStatus string    `json:"connectStatus"` // "ok", "timeout", or the HCI status
	ConnectMs     int64     `json:"connectMs"`
	IntervalMs    float64   `json:"intervalMs"` // as the controller reported it
	UpdatedMs     []float64 `json:"updatedIntervalMs,omitempty"`
	MTU           int       `json:"mtu"`
	Name          string    `json:"name"`
	Reads         int       `json:"reads"`
	ReadFails     int       `json:"readFails"`
	ReadRttMs     []float64 `json:"readRttMs,omitempty"`
	Dropped       string    `json:"dropped,omitempty"` // HCI reason, if the link fell during the run
	PeerAsked     []string  `json:"peerAsked,omitempty"`
	Table         []string  `json:"table,omitempty"` // discovery, one line per attribute
	Notified      int       `json:"notified"`

	handle     uint16
	live       bool
	nameHandle uint16
	resp       []byte
}

// GattResult is the whole run.
type GattResult struct {
	HCIVersion  int        `json:"hciVersion"` // 6 = 4.0, 7 = 4.1, 8 = 4.2
	LMPSubver   int        `json:"lmpSubversion"`
	Manufactur  int        `json:"manufacturer"`
	LEBufLen    int        `json:"leAclLen"`
	LEBufNum    int        `json:"leAclPackets"`
	LEFeatures  string     `json:"leFeatures"`
	LEStates    string     `json:"leStates"`
	WhiteList   int        `json:"whiteListSize"`
	Conns       []GattConn `json:"conns"`
	HoldAdverts int        `json:"holdAdverts"`
}

type gattSession struct {
	f       *os.File
	events  chan []byte
	backlog [][]byte
	reasm   aclReassembler
	conns   []*GattConn
	pending *connComplete
	adverts int
	logf    func(format string, args ...any)
}

// cmd sends a command and waits for its Command Complete or Command Status.
// Anything else that arrives meanwhile is kept for next().
func (s *gattSession) cmd(opcode uint16, params []byte) (commandComplete, error) {
	if _, err := s.f.Write(buildCommand(opcode, params)); err != nil {
		return commandComplete{}, fmt.Errorf("write cmd %04x: %w", opcode, err)
	}
	deadline := time.After(cmdTimeout)
	for {
		select {
		case pkt, ok := <-s.events:
			if !ok {
				return commandComplete{}, fmt.Errorf("read closed during cmd %04x", opcode)
			}
			if cc, ok := parseCommandComplete(pkt); ok && cc.opcode == opcode {
				s.logf("cmd %04x status 0x%02x ret % x", opcode, cc.status, cc.params)
				if cc.status != 0 {
					return cc, fmt.Errorf("cmd %04x status 0x%02x", opcode, cc.status)
				}
				return cc, nil
			}
			s.backlog = append(s.backlog, pkt)
		case <-deadline:
			return commandComplete{}, fmt.Errorf("cmd %04x timeout", opcode)
		}
	}
}

// pump handles one inbound packet, or returns false when none came in time.
func (s *gattSession) pump(wait time.Duration) (bool, error) {
	var pkt []byte
	if len(s.backlog) > 0 {
		pkt, s.backlog = s.backlog[0], s.backlog[1:]
	} else {
		select {
		case p, ok := <-s.events:
			if !ok {
				return false, fmt.Errorf("read closed")
			}
			pkt = p
		case <-time.After(wait):
			return false, nil
		}
	}
	s.handle(pkt)
	return true, nil
}

func (s *gattSession) byHandle(h uint16) *GattConn {
	for _, c := range s.conns {
		if c.live && c.handle == h {
			return c
		}
	}
	return nil
}

func (s *gattSession) handle(pkt []byte) {
	if len(pkt) == 0 {
		return
	}
	if pkt[0] == h4TypeACL {
		if h, cid, payload, ok := s.reasm.Feed(pkt); ok {
			s.onL2CAP(h, cid, payload)
		}
		return
	}
	if len(pkt) < 3 || pkt[0] != h4TypeEvent {
		return
	}
	switch pkt[1] {
	case evtDisconnectComplete:
		// status(1), handle(2), reason(1)
		if len(pkt) >= 7 {
			h := binary.LittleEndian.Uint16(pkt[4:6]) & 0x0FFF
			if c := s.byHandle(h); c != nil {
				c.live = false
				c.Dropped = fmt.Sprintf("0x%02x", pkt[6])
				s.logf("disconnected %s reason 0x%02x", c.Addr, pkt[6])
			}
		}
	case evtLEMeta:
		if cc, ok := parseConnComplete(pkt); ok {
			s.pending = &cc
			return
		}
		if len(pkt) >= 13 && pkt[3] == leSubeventConnUpdateDone {
			// status(1), handle(2), interval(2), latency(2), timeout(2)
			h := binary.LittleEndian.Uint16(pkt[5:7]) & 0x0FFF
			iv := float64(binary.LittleEndian.Uint16(pkt[7:9])) * 1.25
			if c := s.byHandle(h); c != nil {
				c.UpdatedMs = append(c.UpdatedMs, iv)
			}
			s.logf("connection update handle %d status 0x%02x interval %.2fms", h, pkt[4], iv)
			return
		}
		s.adverts += len(parseAdvReports(pkt))
	case 0x13: // Number Of Completed Packets
	default:
		s.logf("event % x", pkt)
	}
}

func (s *gattSession) onL2CAP(h, cid uint16, p []byte) {
	c := s.byHandle(h)
	if c == nil || len(p) == 0 {
		return
	}
	asked := func(what string) {
		c.PeerAsked = append(c.PeerAsked, what)
		s.logf("%s asked: %s (% x)", c.Addr, what, p)
	}
	switch cid {
	case cidATT:
		op := p[0]
		switch {
		case op == attMTUReq:
			asked("att mtu")
			s.f.Write(buildACL(h, cidATT, []byte{attMTURsp, attMTUMin, 0}))
		case op == attOpNotification || op == attIndication:
			if nh, v, _, ok := parseHandleValue(p); ok {
				c.Notified++
				s.logf("%s notified handle 0x%04x: % x %q", c.Addr, nh, v, v)
			}
			if op == attIndication {
				s.f.Write(buildACL(h, cidATT, []byte{attConfirmation}))
			}
		case op == attError || op&0x01 == 1:
			// Error or a response.
			c.resp = append([]byte(nil), p...)
		case op&0x40 == 0:
			// A request we do not serve: Request Not Supported.
			asked(fmt.Sprintf("att request 0x%02x", op))
			s.f.Write(buildACL(h, cidATT, []byte{attError, op, 0, 0, 0x06}))
		}
	case cidLESig:
		// Connection Parameter Update Request: code 0x12, id, len(2), params.
		if p[0] == 0x12 && len(p) >= 12 {
			asked(fmt.Sprintf("interval %.2f-%.2fms latency %d",
				float64(binary.LittleEndian.Uint16(p[4:6]))*1.25,
				float64(binary.LittleEndian.Uint16(p[6:8]))*1.25,
				binary.LittleEndian.Uint16(p[8:10])))
			s.f.Write(buildACL(h, cidLESig, []byte{0x13, p[1], 2, 0, 1, 0})) // rejected
		}
	case cidSMP:
		if p[0] == 0x0B { // Security Request
			asked("smp security request")
			s.f.Write(buildACL(h, cidSMP, []byte{0x05, 0x05})) // Pairing Not Supported
		}
	}
}

// benchRequester puts DiscoverServices on a live connection.
type benchRequester struct {
	s *gattSession
	c *GattConn
}

func (r benchRequester) Request(req []byte) ([]byte, error) { return r.s.attRaw(r.c, req) }

// att is attRaw with an Error Response turned into an error.
func (s *gattSession) att(c *GattConn, req []byte) ([]byte, error) {
	rsp, err := s.attRaw(c, req)
	if err != nil {
		return nil, err
	}
	if rsp[0] == attError {
		return nil, fmt.Errorf("att error % x", rsp)
	}
	return rsp, nil
}

// attRaw sends one request and waits for the peer's answer on that connection.
func (s *gattSession) attRaw(c *GattConn, req []byte) ([]byte, error) {
	c.resp = nil
	if _, err := s.f.Write(buildACL(c.handle, cidATT, req)); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(attTimeout)
	for c.resp == nil {
		if !c.live {
			return nil, fmt.Errorf("link dropped (%s)", c.Dropped)
		}
		left := time.Until(deadline)
		if left <= 0 {
			return nil, fmt.Errorf("att 0x%02x timeout", req[0])
		}
		if _, err := s.pump(left); err != nil {
			return nil, err
		}
	}
	return c.resp, nil
}

// explore runs the production discovery against the peer, then reads,
// subscribes and writes as asked, recording one line per attribute.
func (s *gattSession) explore(c *GattConn, o GattOptions) {
	services, err := DiscoverServices(benchRequester{s, c})
	if err != nil {
		c.Table = append(c.Table, "discovery failed: "+err.Error())
		return
	}
	wantUUID, wantHex, _ := strings.Cut(o.Write, ":")
	for _, sv := range services {
		c.Table = append(c.Table, fmt.Sprintf("service %s 0x%04x-0x%04x", sv.UUID, sv.Start, sv.End))
		for _, ch := range sv.Characteristics {
			line := fmt.Sprintf("  char %s props 0x%02x value 0x%04x", ch.UUID, ch.Properties, ch.ValueHandle)
			if ch.Properties&0x02 != 0 && c.live {
				rsp, err := s.attRaw(c, encodeReadReq(ch.ValueHandle))
				if err != nil {
					line += " read: " + err.Error()
				} else if v, err := parseReadRsp(attOpReadReq, rsp); err != nil {
					line += " read: " + err.Error()
				} else {
					line += fmt.Sprintf(" = % x %q", v, v)
				}
			}
			c.Table = append(c.Table, line)
			for _, d := range ch.Descriptors {
				dl := fmt.Sprintf("    desc %s 0x%04x", d.UUID, d.Handle)
				if o.Notify && d.UUID.Is16(0x2902) && ch.Properties&0x30 != 0 && c.live {
					v := []byte{0x01, 0x00}
					if ch.Properties&0x10 == 0 {
						v = []byte{0x02, 0x00} // indicate only
					}
					rsp, err := s.attRaw(c, encodeWriteReq(d.Handle, v))
					if err == nil {
						err = parseWriteRsp(rsp)
					}
					dl += fmt.Sprintf(" subscribe: %v", err)
				}
				c.Table = append(c.Table, dl)
			}
			if wantUUID != "" && strings.Contains(ch.UUID.String(), strings.ToLower(wantUUID)) && c.live {
				v, err := hex.DecodeString(wantHex)
				if err == nil {
					var rsp []byte
					if rsp, err = s.attRaw(c, encodeWriteReq(ch.ValueHandle, v)); err == nil {
						err = parseWriteRsp(rsp)
					}
				}
				c.Table = append(c.Table, fmt.Sprintf("  wrote % x to 0x%04x: %v", v, ch.ValueHandle, err))
			}
		}
	}
}

func (s *gattSession) connect(t GattTarget, intervalMs int) error {
	c := &GattConn{Addr: t.Addr}
	s.conns = append(s.conns, c)
	mac, err := net.ParseMAC(t.Addr)
	if err != nil || len(mac) != 6 {
		return fmt.Errorf("bad address %q", t.Addr)
	}
	t0 := time.Now()
	s.pending = nil
	if cc, err := s.cmd(opLECreateConn, createConnParams(mac, t.AddrType, 0, intervalMs)); err != nil {
		c.ConnectStatus = fmt.Sprintf("0x%02x", cc.status)
		return nil
	}
	cancelled := false
	for s.pending == nil {
		if !cancelled && time.Since(t0) > connectTimeout {
			// The cancel is answered by a Connection Complete with status 0x02.
			if _, err := s.cmd(opLECreateConnCancel, nil); err != nil {
				c.ConnectStatus = "timeout"
				return nil
			}
			cancelled = true
		}
		if cancelled && time.Since(t0) > connectTimeout+cmdTimeout {
			break
		}
		if _, err := s.pump(time.Second); err != nil {
			return err
		}
	}
	c.ConnectMs = time.Since(t0).Milliseconds()
	if s.pending == nil || cancelled {
		c.ConnectStatus = "timeout"
		return nil
	}
	if s.pending.status != 0 {
		c.ConnectStatus = fmt.Sprintf("0x%02x", s.pending.status)
		return nil
	}
	c.ConnectStatus, c.live = "ok", true
	c.handle, c.IntervalMs = s.pending.handle, s.pending.intervalMs
	s.logf("connected %s handle %d interval %.2fms in %dms", c.Addr, c.handle, c.IntervalMs, c.ConnectMs)

	c.MTU = attMTUMin
	if rsp, err := s.att(c, []byte{attMTUReq, 185, 0}); err != nil {
		s.logf("%s mtu: %v", c.Addr, err)
	} else if len(rsp) >= 3 && rsp[0] == attMTURsp {
		if m := int(binary.LittleEndian.Uint16(rsp[1:3])); m < 185 {
			c.MTU = m
		} else {
			c.MTU = 185
		}
	}
	if !c.live {
		return nil
	}
	// Read By Type, whole handle range, Device Name (0x2A00).
	rsp, err := s.att(c, []byte{attReadByTypeReq, 0x01, 0x00, 0xFF, 0xFF, 0x00, 0x2A})
	if err != nil {
		s.logf("%s device name: %v", c.Addr, err)
		return nil
	}
	if len(rsp) >= 4 && rsp[0] == attReadByTypeRsp && int(rsp[1]) >= 2 && len(rsp) >= 2+int(rsp[1]) {
		c.nameHandle = binary.LittleEndian.Uint16(rsp[2:4])
		c.Name = string(rsp[4 : 2+int(rsp[1])])
	}
	return nil
}

// GattProbe runs one probe. The caller must ensure nothing else owns
// /dev/stpbt. The controller is reset on the way out, which drops any link
// still up.
func GattProbe(o GattOptions) (GattResult, error) {
	var res GattResult
	f, err := os.OpenFile(devPath(), os.O_RDWR, 0)
	if err != nil {
		return res, fmt.Errorf("open %s: %w", devPath(), err)
	}
	defer f.Close()

	s := &gattSession{f: f, events: make(chan []byte, 1024), logf: o.Logf}
	go func() {
		var parser h4Parser
		buf := make([]byte, 2048)
		for {
			n, err := f.Read(buf)
			if err != nil {
				close(s.events)
				return
			}
			for _, pkt := range parser.Feed(buf[:n]) {
				select {
				case s.events <- pkt:
				default:
				}
			}
		}
	}()
	collect := func() {
		for _, c := range s.conns {
			res.Conns = append(res.Conns, *c)
		}
		res.HoldAdverts = s.adverts
	}

	if _, err := s.cmd(opReset, nil); err != nil {
		return res, err
	}
	defer s.cmd(opReset, nil)

	if cc, err := s.cmd(opReadLocalVersion, nil); err == nil && len(cc.params) >= 8 {
		res.HCIVersion = int(cc.params[0])
		res.Manufactur = int(binary.LittleEndian.Uint16(cc.params[4:6]))
		res.LMPSubver = int(binary.LittleEndian.Uint16(cc.params[6:8]))
	}
	if cc, err := s.cmd(opLEReadBufferSize, nil); err == nil && len(cc.params) >= 3 {
		res.LEBufLen = int(binary.LittleEndian.Uint16(cc.params[0:2]))
		res.LEBufNum = int(cc.params[2])
	}
	if cc, err := s.cmd(opLEReadFeatures, nil); err == nil {
		res.LEFeatures = fmt.Sprintf("%x", cc.params)
	}
	if cc, err := s.cmd(opLEReadStates, nil); err == nil {
		res.LEStates = fmt.Sprintf("%x", cc.params)
	}
	if cc, err := s.cmd(opLEReadWhiteList, nil); err == nil && len(cc.params) >= 1 {
		res.WhiteList = int(cc.params[0])
	}

	for _, t := range o.Targets {
		if err := s.connect(t, o.ConnIntervalMs); err != nil {
			collect()
			return res, err
		}
	}

	if o.Discover {
		for _, c := range s.conns {
			if c.live {
				s.explore(c, o)
			}
		}
	}

	s.adverts = 0
	if o.Scan {
		if _, err := s.cmd(opLESetScanParams, scanParams(320, 30)); err == nil {
			s.cmd(opLESetScanEnable, []byte{0x01, 0x00})
		}
	}
	end := time.Now().Add(o.Hold)
	nextRead := time.Now().Add(o.ReadEvery)
	for time.Now().Before(end) {
		if o.ReadEvery > 0 && !time.Now().Before(nextRead) {
			nextRead = nextRead.Add(o.ReadEvery)
			for _, c := range s.conns {
				if !c.live || c.nameHandle == 0 {
					continue
				}
				t0 := time.Now()
				req := []byte{attReadReq, byte(c.nameHandle), byte(c.nameHandle >> 8)}
				if rsp, err := s.att(c, req); err != nil || rsp[0] != attReadRsp {
					c.ReadFails++
				} else {
					c.Reads++
					c.ReadRttMs = append(c.ReadRttMs, float64(time.Since(t0).Microseconds())/1000)
				}
			}
			continue
		}
		wait := time.Until(end)
		if o.ReadEvery > 0 && time.Until(nextRead) < wait {
			wait = time.Until(nextRead)
		}
		if wait < time.Millisecond {
			wait = time.Millisecond
		}
		if _, err := s.pump(wait); err != nil {
			collect()
			return res, err
		}
	}
	if o.Scan {
		s.cmd(opLESetScanEnable, []byte{0x00, 0x00})
	}

	for _, c := range s.conns {
		if !c.live {
			continue
		}
		c.Dropped = ""
		p := []byte{byte(c.handle), byte(c.handle >> 8), 0x13} // remote user terminated
		if _, err := s.cmd(opDisconnect, p); err != nil {
			continue
		}
		for t0 := time.Now(); c.live && time.Since(t0) < cmdTimeout; {
			if _, err := s.pump(time.Second); err != nil {
				break
			}
		}
		c.Dropped = "" // ours, not a fault
	}
	collect()
	return res, nil
}

// Seen is one advertiser found by ScanList.
type Seen struct {
	Addr        string `json:"addr"`
	AddrType    int    `json:"addrType"`
	Connectable bool   `json:"connectable"`
	Rssi        int    `json:"rssi"`
	Name        string `json:"name,omitempty"`
	Company     string `json:"company,omitempty"` // manufacturer data company id
	Services    string `json:"services,omitempty"`
	Adverts     int    `json:"adverts"`
}

// ScanList scans actively for d and returns what it heard, so a connectable
// target and its address type can be picked. Active, to collect the names
// that only a scan response carries.
func ScanList(d time.Duration, logf func(format string, args ...any)) ([]Seen, error) {
	f, err := os.OpenFile(devPath(), os.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", devPath(), err)
	}
	defer f.Close()
	s := &gattSession{f: f, events: make(chan []byte, 1024), logf: logf}
	go func() {
		var parser h4Parser
		buf := make([]byte, 2048)
		for {
			n, err := f.Read(buf)
			if err != nil {
				close(s.events)
				return
			}
			for _, pkt := range parser.Feed(buf[:n]) {
				select {
				case s.events <- pkt:
				default:
				}
			}
		}
	}()
	if _, err := s.cmd(opReset, nil); err != nil {
		return nil, err
	}
	defer s.cmd(opReset, nil)
	params := scanParams(100, 100)
	params[0] = 0x01 // active
	if _, err := s.cmd(opLESetScanParams, params); err != nil {
		return nil, err
	}
	if _, err := s.cmd(opLESetScanEnable, []byte{0x01, 0x00}); err != nil {
		return nil, err
	}

	seen := map[string]*Seen{}
	var order []string
	end := time.After(d)
loop:
	for {
		select {
		case pkt, ok := <-s.events:
			if !ok {
				return nil, fmt.Errorf("read closed")
			}
			if len(pkt) < 5 || pkt[0] != h4TypeEvent || pkt[1] != evtLEMeta || pkt[3] != leSubeventAdvReport {
				continue
			}
			// Same walk as parseAdvReports, keeping the event type it drops.
			body := pkt[5:]
			for _, a := range parseAdvReports(pkt) {
				evt := body[0]
				body = body[9+len(a.Data)+1:]
				e := seen[a.Addr]
				if e == nil {
					e = &Seen{Addr: a.Addr, AddrType: a.AddrType}
					seen[a.Addr] = e
					order = append(order, a.Addr)
				}
				e.Adverts++
				e.Rssi = a.Rssi
				if evt == 0x00 || evt == 0x01 { // ADV_IND, ADV_DIRECT_IND
					e.Connectable = true
				}
				for ad := a.Data; len(ad) >= 2 && int(ad[0]) >= 1 && len(ad) >= 1+int(ad[0]); ad = ad[1+int(ad[0]):] {
					val := ad[2 : 1+int(ad[0])]
					switch ad[1] {
					case 0x08, 0x09:
						e.Name = string(val)
					case 0x02, 0x03:
						for ; len(val) >= 2; val = val[2:] {
							if u := fmt.Sprintf("%04x ", binary.LittleEndian.Uint16(val)); !strings.Contains(e.Services, u) {
								e.Services += u
							}
						}
					case 0x06, 0x07:
						for ; len(val) >= 16; val = val[16:] {
							if u := UUID(val[:16]).String() + " "; !strings.Contains(e.Services, u) {
								e.Services += u
							}
						}
					case 0xFF:
						if len(val) >= 2 {
							e.Company = fmt.Sprintf("0x%04x", binary.LittleEndian.Uint16(val[0:2]))
						}
					}
				}
			}
		case <-end:
			break loop
		}
	}
	s.cmd(opLESetScanEnable, []byte{0x00, 0x00})
	out := make([]Seen, 0, len(order))
	for _, a := range order {
		out = append(out, *seen[a])
	}
	return out, nil
}
