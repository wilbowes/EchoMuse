package bluetooth

// HCI ACL framing and the L2CAP basic header for LE's fixed channels (Core
// Spec Vol 4 Part E 5.4.2, Vol 3 Part A 3.1), and the two HCI structures a
// central needs to bring a link up.

import (
	"encoding/binary"
	"net"
)

const (
	evtDisconnectComplete  = 0x05
	evtNumCompletedPackets = 0x13

	leSubeventConnComplete   = 0x01
	leSubeventConnUpdateDone = 0x03

	opDisconnect         = 0x01<<10 | 0x0006
	opReadBufferSize     = 0x04<<10 | 0x0005
	opLEReadBufferSize   = 0x08<<10 | 0x0002
	opLESetRandomAddr    = 0x08<<10 | 0x0005
	opLECreateConn       = 0x08<<10 | 0x000D
	opLECreateConnCancel = 0x08<<10 | 0x000E
	opLEConnUpdate       = 0x08<<10 | 0x0013

	cidATT   = 0x0004
	cidLESig = 0x0005
	cidSMP   = 0x0006

	// Largest L2CAP PDU accepted from a peer. ATT's own ceiling is 512 bytes
	// of attribute value; anything claiming more is not something to buffer.
	l2capMaxPDU = 1024
)

// fragmentACL frames one L2CAP PDU as HCI ACL packets (H4-framed) of at most
// maxLen data bytes each: the first with the start flag an LE host must use
// (PB=00), the rest as continuations (PB=01).
func fragmentACL(handle, cid uint16, payload []byte, maxLen int) [][]byte {
	frame := make([]byte, 4+len(payload))
	binary.LittleEndian.PutUint16(frame[0:2], uint16(len(payload)))
	binary.LittleEndian.PutUint16(frame[2:4], cid)
	copy(frame[4:], payload)
	if maxLen < 1 {
		maxLen = len(frame)
	}
	var pkts [][]byte
	for first := true; len(frame) > 0; first = false {
		n := len(frame)
		if n > maxLen {
			n = maxLen
		}
		hf := handle & 0x0FFF
		if !first {
			hf |= 0x1 << 12
		}
		pkt := make([]byte, 5+n)
		pkt[0] = h4TypeACL
		binary.LittleEndian.PutUint16(pkt[1:3], hf)
		binary.LittleEndian.PutUint16(pkt[3:5], uint16(n))
		copy(pkt[5:], frame[:n])
		pkts = append(pkts, pkt)
		frame = frame[n:]
	}
	return pkts
}

// buildACL frames one L2CAP PDU as a single ACL packet.
func buildACL(handle, cid uint16, payload []byte) []byte {
	return fragmentACL(handle, cid, payload, 0)[0]
}

// aclReassembler rebuilds L2CAP PDUs from ACL fragments, per connection.
type aclReassembler struct {
	partial map[uint16][]byte
}

// Feed takes one H4-framed ACL packet and returns a complete PDU when this
// fragment finishes one. A continuation with nothing started is dropped, and
// so is a PDU that claims more than l2capMaxPDU.
func (r *aclReassembler) Feed(pkt []byte) (handle, cid uint16, payload []byte, ok bool) {
	if len(pkt) < 5 || pkt[0] != h4TypeACL {
		return 0, 0, nil, false
	}
	hf := binary.LittleEndian.Uint16(pkt[1:3])
	handle = hf & 0x0FFF
	data := pkt[5:]
	if r.partial == nil {
		r.partial = map[uint16][]byte{}
	}
	if pb := (hf >> 12) & 0x3; pb == 0x1 {
		if r.partial[handle] == nil {
			return 0, 0, nil, false
		}
		r.partial[handle] = append(r.partial[handle], data...)
	} else {
		r.partial[handle] = append([]byte(nil), data...)
	}
	buf := r.partial[handle]
	if len(buf) < 4 {
		return 0, 0, nil, false
	}
	n := int(binary.LittleEndian.Uint16(buf[0:2]))
	if n > l2capMaxPDU {
		delete(r.partial, handle)
		return 0, 0, nil, false
	}
	if len(buf) < 4+n {
		return 0, 0, nil, false
	}
	delete(r.partial, handle)
	return handle, binary.LittleEndian.Uint16(buf[2:4]), buf[4 : 4+n], true
}

// Drop forgets a connection's unfinished PDU.
func (r *aclReassembler) Drop(handle uint16) {
	delete(r.partial, handle)
}

// createConnParams builds LE Create Connection for one peer. ownType is 0 for
// the public address, 1 for the random one set with LE Set Random Address.
// The interval is in ms (spec units of 1.25ms); supervision timeout is 5s.
func createConnParams(peer net.HardwareAddr, peerType, ownType, intervalMs int) []byte {
	iv := uint16(intervalMs * 100 / 125)
	if iv < 6 {
		iv = 6
	}
	if iv > 3200 {
		iv = 3200
	}
	p := make([]byte, 25)
	binary.LittleEndian.PutUint16(p[0:2], 0x0060) // scan interval 60ms
	binary.LittleEndian.PutUint16(p[2:4], 0x0030) // scan window 30ms
	p[4] = 0x00                                   // no white list
	p[5] = byte(peerType)
	for i := 0; i < 6; i++ {
		p[6+i] = peer[5-i]
	}
	p[12] = byte(ownType)
	binary.LittleEndian.PutUint16(p[13:15], iv)
	binary.LittleEndian.PutUint16(p[15:17], iv)
	binary.LittleEndian.PutUint16(p[17:19], 0)   // latency
	binary.LittleEndian.PutUint16(p[19:21], 500) // supervision timeout, 10ms units
	return p
}

// connUpdateParams builds LE Connection Update: a new interval for a link
// that is up, with the same latency and supervision timeout it was made with.
func connUpdateParams(handle uint16, intervalMs int) []byte {
	iv := uint16(intervalMs * 100 / 125)
	p := make([]byte, 14)
	binary.LittleEndian.PutUint16(p[0:2], handle)
	binary.LittleEndian.PutUint16(p[2:4], iv)
	binary.LittleEndian.PutUint16(p[4:6], iv)
	binary.LittleEndian.PutUint16(p[6:8], 0)    // latency
	binary.LittleEndian.PutUint16(p[8:10], 500) // supervision timeout, 10ms units
	return p
}

// connComplete is an LE Connection Complete event.
type connComplete struct {
	status     byte
	handle     uint16
	intervalMs float64
	latency    int
	timeoutMs  int
}

func parseConnComplete(pkt []byte) (connComplete, bool) {
	if len(pkt) < 22 || pkt[0] != h4TypeEvent || pkt[1] != evtLEMeta || pkt[3] != leSubeventConnComplete {
		return connComplete{}, false
	}
	p := pkt[3:]
	return connComplete{
		status:     p[1],
		handle:     binary.LittleEndian.Uint16(p[2:4]) & 0x0FFF,
		intervalMs: float64(binary.LittleEndian.Uint16(p[12:14])) * 1.25,
		latency:    int(binary.LittleEndian.Uint16(p[14:16])),
		timeoutMs:  int(binary.LittleEndian.Uint16(p[16:18])) * 10,
	}, true
}
