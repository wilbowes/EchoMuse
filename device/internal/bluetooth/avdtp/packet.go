package avdtp

// Signalling packets: header, fragmentation and reassembly (AVDTP 1.3 §8.4).
//
//	single:   [label:4 type:2 msg:2] [rfa:2 signal:6] params...
//	start:    [label:4 type:2 msg:2] [NOSP] [rfa:2 signal:6] params...
//	continue: [label:4 type:2 msg:2] params...
//	end:      [label:4 type:2 msg:2] params...
//
// NOSP counts every packet of the message, the start included.

import (
	"errors"
	"fmt"
)

const (
	pktSingle   = 0
	pktStart    = 1
	pktContinue = 2
	pktEnd      = 3
)

// MinSignalMTU is the smallest signalling MTU: L2CAP's BR/EDR minimum
// (Core Vol 3 Part A §5.1), which AVDTP does not raise.
const MinSignalMTU = 48

// maxMessage bounds a reassembled message. The largest real one is a
// capabilities list of a few dozen bytes; this only stops a peer making us
// buffer without limit.
const maxMessage = 4096

// Message is one whole signalling message.
type Message struct {
	Label  uint8 // transaction label, 0-15
	Type   MessageType
	Signal SignalID
	Params []byte
}

// Fragment encodes m as signalling packets of at most mtu bytes each.
func Fragment(m Message, mtu int) ([][]byte, error) {
	if mtu < MinSignalMTU {
		return nil, fmt.Errorf("avdtp: signalling MTU %d below %d", mtu, MinSignalMTU)
	}
	if m.Label > 0x0F || m.Signal > 0x3F || m.Type > 3 {
		return nil, fmt.Errorf("avdtp: bad header fields %+v", m)
	}
	h := func(pt byte) byte { return m.Label<<4 | pt<<2 | byte(m.Type) }
	if 2+len(m.Params) <= mtu {
		return [][]byte{append([]byte{h(pktSingle), byte(m.Signal)}, m.Params...)}, nil
	}
	first := mtu - 3
	rest := len(m.Params) - first
	nosp := 1 + (rest+mtu-2)/(mtu-1)
	if nosp > 255 {
		return nil, fmt.Errorf("avdtp: %d-byte message needs %d packets", len(m.Params), nosp)
	}
	out := make([][]byte, 0, nosp)
	out = append(out, append([]byte{h(pktStart), byte(nosp), byte(m.Signal)}, m.Params[:first]...))
	p := m.Params[first:]
	for len(p) > 0 {
		n := min(len(p), mtu-1)
		pt := byte(pktContinue)
		if n == len(p) {
			pt = pktEnd
		}
		out = append(out, append([]byte{h(pt)}, p[:n]...))
		p = p[n:]
	}
	return out, nil
}

var errFragment = errors.New("avdtp: bad fragment")

// Reassembler joins fragmented messages. Packets of one message arrive
// consecutively (§8.4.2), so one in progress is all it holds; a new start or
// single packet abandons it.
type Reassembler struct {
	active bool
	head   Message
	nosp   int
	got    int
}

// Feed takes one packet; it returns a message once one is whole.
func (r *Reassembler) Feed(pkt []byte) (Message, bool, error) {
	if len(pkt) < 1 {
		return Message{}, false, fmt.Errorf("%w: empty", errFragment)
	}
	label, pt, mt := pkt[0]>>4, (pkt[0]>>2)&3, MessageType(pkt[0]&3)
	switch pt {
	case pktSingle:
		r.active = false
		if len(pkt) < 2 {
			return Message{}, false, fmt.Errorf("%w: short single", errFragment)
		}
		return Message{label, mt, SignalID(pkt[1] & 0x3F), clone(pkt[2:])}, true, nil
	case pktStart:
		r.active = false
		if len(pkt) < 3 || pkt[1] < 2 {
			return Message{}, false, fmt.Errorf("%w: bad start", errFragment)
		}
		r.active, r.nosp, r.got = true, int(pkt[1]), 1
		r.head = Message{label, mt, SignalID(pkt[2] & 0x3F), clone(pkt[3:])}
		return Message{}, false, nil
	}
	if !r.active || label != r.head.Label || mt != r.head.Type {
		r.active = false
		return Message{}, false, fmt.Errorf("%w: continuation without its start", errFragment)
	}
	r.got++
	last := pt == pktEnd
	if last != (r.got == r.nosp) || len(r.head.Params)+len(pkt)-1 > maxMessage {
		r.active = false
		return Message{}, false, fmt.Errorf("%w: packet %d of %d (end=%v)", errFragment, r.got, r.nosp, last)
	}
	r.head.Params = append(r.head.Params, pkt[1:]...)
	if !last {
		return Message{}, false, nil
	}
	r.active = false
	return r.head, true, nil
}

func clone(b []byte) []byte { return append([]byte(nil), b...) }
