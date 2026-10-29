package avdtp

// A2DP media packets: an RTP header (RFC 3550 §5.1) then, for SBC, the
// one-byte media payload header and whole SBC frames (A2DP 1.3 §4.3.4).
//
//	RTP:  [V=2 P X CC:4] [M PT:7] [seq:16] [timestamp:32] [SSRC:32]
//	SBC:  [F S L rfa NumberOfFrames:4]
//
// The timestamp counts samples at the stream's sampling rate (A2DP §4.3.3).

import (
	"encoding/binary"
	"errors"
	"fmt"
)

const (
	RTPHeaderLen = 12
	// PayloadType is the dynamic RTP payload type A2DP sources use; A2DP
	// leaves the value open and sinks do not check it.
	PayloadType = 96
	// MaxSBCFrames is NumberOfFrames' ceiling: four bits.
	MaxSBCFrames = 15

	sbcHeaderLen = 1
)

var (
	ErrNoFrames      = errors.New("avdtp: no frames to send")
	ErrFrameTooLarge = errors.New("avdtp: frame does not fit the media MTU")
)

// Packetiser builds a stream's media packets. Seq and Timestamp are the next
// packet's; set them to random values to start (RFC 3550 §5.1).
type Packetiser struct {
	MTU       int // the media channel's negotiated outgoing MTU
	SSRC      uint32
	Seq       uint16
	Timestamp uint32
}

// FramesPerPacket is how many frames of frameLen bytes fit one packet.
func FramesPerPacket(mtu, frameLen int) int {
	if frameLen <= 0 {
		return 0
	}
	return max(0, min(MaxSBCFrames, (mtu-RTPHeaderLen-sbcHeaderLen)/frameLen))
}

// Pack builds one packet from as many leading frames as fit, and returns it
// with the number consumed. samplesPerFrame is SBC's blocks × subbands.
// Frames are never split: A2DP's fragmentation bits exist for a frame larger
// than the MTU, which no SBC configuration we send produces, so that frame is
// an error.
func (p *Packetiser) Pack(frames [][]byte, samplesPerFrame int) ([]byte, int, error) {
	if len(frames) == 0 {
		return nil, 0, ErrNoFrames
	}
	room := p.MTU - RTPHeaderLen - sbcHeaderLen
	n, size := 0, 0
	for n < len(frames) && n < MaxSBCFrames && size+len(frames[n]) <= room {
		size += len(frames[n])
		n++
	}
	if n == 0 {
		return nil, 0, fmt.Errorf("%w: %d bytes, MTU %d", ErrFrameTooLarge, len(frames[0]), p.MTU)
	}
	pkt := make([]byte, RTPHeaderLen+sbcHeaderLen, RTPHeaderLen+sbcHeaderLen+size)
	pkt[0] = 0x80 // V=2, no padding, extension or CSRCs
	pkt[1] = PayloadType
	binary.BigEndian.PutUint16(pkt[2:4], p.Seq)
	binary.BigEndian.PutUint32(pkt[4:8], p.Timestamp)
	binary.BigEndian.PutUint32(pkt[8:12], p.SSRC)
	pkt[12] = byte(n)
	for _, f := range frames[:n] {
		pkt = append(pkt, f...)
	}
	p.Seq++
	p.Timestamp += uint32(n * samplesPerFrame)
	return pkt, n, nil
}

// MediaPacket is a decoded A2DP SBC media packet.
type MediaPacket struct {
	PayloadType uint8
	Marker      bool
	Seq         uint16
	Timestamp   uint32
	SSRC        uint32
	Fragmented  bool
	Start, Last bool
	Frames      int
	Payload     []byte // the SBC frames, aliasing the input
}

// ParseMediaPacket decodes a packet, honouring RTP's CSRC list, extension and
// padding although no A2DP source sends them.
func ParseMediaPacket(b []byte) (MediaPacket, error) {
	var m MediaPacket
	if len(b) < RTPHeaderLen+sbcHeaderLen {
		return m, fmt.Errorf("avdtp: %d-byte media packet", len(b))
	}
	if b[0]>>6 != 2 {
		return m, fmt.Errorf("avdtp: RTP version %d", b[0]>>6)
	}
	end := len(b)
	if b[0]&0x20 != 0 { // padding: the last byte counts it
		pad := int(b[end-1])
		if pad == 0 || pad > end-RTPHeaderLen {
			return m, errors.New("avdtp: bad RTP padding")
		}
		end -= pad
	}
	off := RTPHeaderLen + 4*int(b[0]&0x0F)
	if b[0]&0x10 != 0 {
		if off+4 > end {
			return m, errors.New("avdtp: truncated RTP extension")
		}
		off += 4 + 4*int(binary.BigEndian.Uint16(b[off+2:off+4]))
	}
	if off+sbcHeaderLen > end {
		return m, errors.New("avdtp: truncated media packet")
	}
	m.Marker, m.PayloadType = b[1]&0x80 != 0, b[1]&0x7F
	m.Seq = binary.BigEndian.Uint16(b[2:4])
	m.Timestamp = binary.BigEndian.Uint32(b[4:8])
	m.SSRC = binary.BigEndian.Uint32(b[8:12])
	h := b[off]
	m.Fragmented, m.Start, m.Last = h&0x80 != 0, h&0x40 != 0, h&0x20 != 0
	m.Frames = int(h & 0x0F)
	m.Payload = b[off+sbcHeaderLen : end]
	return m, nil
}
