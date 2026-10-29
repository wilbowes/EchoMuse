// Package avrcp is AVRCP 1.6 over AVCTP 1.4 for an A2DP source: the speaker
// presses our buttons (we are the category 1 target) and we set its volume
// (we are the category 2 controller, AVRCP 1.6 §6.13).
//
// It knows nothing of L2CAP: the caller hands it a Channel to send on and
// feeds it every inbound PDU through Session.Receive.
package avrcp

import (
	"errors"
	"fmt"
)

// AVCTP 1.4 §6.1. Every packet's first byte is
//
//	label(4) | packet type(2) | C/R(1) | IPID(1)
//
// a single packet follows it with the 2-byte profile ID, a start packet with
// the packet count and then the profile ID, and continue/end packets with
// nothing: their profile is the start packet's.
const (
	// PID is the A/V Remote Control service class UUID, AVCTP's profile ID.
	PID = 0x110E

	// MinMTU is L2CAP's BR/EDR minimum MTU (Core Vol 3 Part A §5.1); no
	// AVCTP channel can be configured smaller.
	MinMTU = 48

	// MaxFrame is the AV/C frame ceiling (AVRCP 1.6 §6.3.1, 512 bytes). A
	// reassembled message larger than this is not one we will buffer.
	MaxFrame = 512
)

const (
	pktSingle   = 0
	pktStart    = 1
	pktContinue = 2
	pktEnd      = 3
)

// ErrMalformed marks an inbound PDU that breaks the framing.
var ErrMalformed = errors.New("avrcp: malformed")

// message is one whole AVCTP message, before fragmentation or after
// reassembly.
type message struct {
	label    uint8
	response bool
	ipid     bool
	pid      uint16
	payload  []byte
}

func header(label uint8, pt int, response, ipid bool) byte {
	b := label<<4 | byte(pt)<<2
	if response {
		b |= 0x02
	}
	if ipid {
		b |= 0x01
	}
	return b
}

// fragment frames m as AVCTP packets of at most mtu bytes (§6.1.3): one
// single packet when it fits, else a start, continues and an end.
func fragment(m message, mtu int) ([][]byte, error) {
	if mtu < MinMTU {
		mtu = MinMTU
	}
	if m.label > 0x0F {
		return nil, fmt.Errorf("avrcp: label %d", m.label)
	}
	p := m.payload
	if len(p)+3 <= mtu {
		pkt := make([]byte, 3, 3+len(p))
		pkt[0] = header(m.label, pktSingle, m.response, m.ipid)
		pkt[1], pkt[2] = byte(m.pid>>8), byte(m.pid)
		return [][]byte{append(pkt, p...)}, nil
	}
	first, rest := mtu-4, mtu-1
	n := 1 + (len(p)-first+rest-1)/rest
	if n > 255 {
		return nil, fmt.Errorf("avrcp: %d bytes needs %d packets", len(p), n)
	}
	out := make([][]byte, 0, n)
	start := []byte{header(m.label, pktStart, m.response, m.ipid), byte(n), byte(m.pid >> 8), byte(m.pid)}
	out = append(out, append(start, p[:first]...))
	p = p[first:]
	for len(p) > 0 {
		k, pt := rest, pktContinue
		if len(p) <= rest {
			k, pt = len(p), pktEnd
		}
		out = append(out, append([]byte{header(m.label, pt, m.response, m.ipid)}, p[:k]...))
		p = p[k:]
	}
	return out, nil
}

// reassembler puts fragmented messages back together. AVCTP sends a
// message's packets back to back (§6.1.3), so one is in flight at a time; a
// new start or single abandons it.
type reassembler struct {
	active bool
	m      message
	want   int
	got    int
}

// feed takes one packet. It returns a whole message when one completes, and
// ErrMalformed for a packet it cannot place, which the caller drops.
func (r *reassembler) feed(pkt []byte) (message, bool, error) {
	if len(pkt) == 0 {
		return message{}, false, ErrMalformed
	}
	h := pkt[0]
	label, pt := h>>4, int(h>>2)&0x03
	response, ipid := h&0x02 != 0, h&0x01 != 0
	switch pt {
	case pktSingle:
		r.active = false
		if len(pkt) < 3 || len(pkt)-3 > MaxFrame {
			return message{}, false, ErrMalformed
		}
		return message{label, response, ipid, uint16(pkt[1])<<8 | uint16(pkt[2]),
			append([]byte(nil), pkt[3:]...)}, true, nil
	case pktStart:
		r.active = false
		if len(pkt) < 4 || pkt[1] < 2 || len(pkt)-4 > MaxFrame {
			return message{}, false, ErrMalformed
		}
		r.m = message{label, response, ipid, uint16(pkt[2])<<8 | uint16(pkt[3]),
			append([]byte(nil), pkt[4:]...)}
		r.want, r.got, r.active = int(pkt[1]), 1, true
		return message{}, false, nil
	}
	// Continue or end: must belong to the message in flight.
	if !r.active || label != r.m.label || response != r.m.response {
		r.active = false
		return message{}, false, ErrMalformed
	}
	r.got++
	if len(r.m.payload)+len(pkt)-1 > MaxFrame || r.got > r.want ||
		(pt == pktContinue && r.got == r.want) || (pt == pktEnd && r.got != r.want) {
		r.active = false
		return message{}, false, ErrMalformed
	}
	r.m.payload = append(r.m.payload, pkt[1:]...)
	if pt == pktEnd {
		r.active = false
		return r.m, true, nil
	}
	return message{}, false, nil
}
