// Package sdp is the Service Discovery Protocol (Core Spec v5.4 Vol 3 Part
// B): the data element codec, the PDUs, a client that finds a speaker's A2DP
// sink and AVRCP records, and a server holding our own records.
//
// It is pure: nothing here knows about HCI or L2CAP. The caller gives the
// client a Requester over one L2CAP channel on PSM 0x0001, and feeds the
// server each request PDU from the peer's SDP channel.
package sdp

// Data elements (Vol 3 Part B 3): a one-byte header holding the type in the
// top five bits and a size index in the bottom three, then the data.

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// Type is a data element type descriptor (Vol 3 Part B 3.2, Table 3.1).
type Type uint8

const (
	TypeNil  Type = 0
	TypeUint Type = 1
	TypeInt  Type = 2
	TypeUUID Type = 3
	TypeText Type = 4
	TypeBool Type = 5
	TypeSeq  Type = 6
	TypeAlt  Type = 7
	TypeURL  Type = 8
)

// maxDepth bounds sequence nesting on decode. A response nests four deep at
// most (list of lists → attribute list → ProtocolDescriptorList → protocol
// descriptor); the bound only stops hostile input from recursing the stack.
const maxDepth = 16

var (
	errShort   = errors.New("sdp: data element truncated")
	errBadSize = errors.New("sdp: size index not permitted for this type")
	errBadType = errors.New("sdp: reserved data element type")
	errDepth   = errors.New("sdp: data element nested too deep")
)

// Element is one decoded data element.
//
// Numbers and UUIDs keep their encoded width in Raw (big-endian, len 1-16), so
// a value re-encodes exactly as it arrived; a 128-bit integer has no Go type
// and is only ever carried this way.
type Element struct {
	Type  Type
	Raw   []byte    // uint, int, UUID: the value; text, URL: the bytes
	Bool  bool      // TypeBool
	Items []Element // TypeSeq, TypeAlt
}

func be(v uint64, n int) []byte {
	b := make([]byte, n)
	for i := n - 1; i >= 0; i-- {
		b[i] = byte(v)
		v >>= 8
	}
	return b
}

func Nil() Element                 { return Element{Type: TypeNil} }
func Uint8(v uint8) Element        { return Element{Type: TypeUint, Raw: be(uint64(v), 1)} }
func Uint16(v uint16) Element      { return Element{Type: TypeUint, Raw: be(uint64(v), 2)} }
func Uint32(v uint32) Element      { return Element{Type: TypeUint, Raw: be(uint64(v), 4)} }
func Uint64(v uint64) Element      { return Element{Type: TypeUint, Raw: be(v, 8)} }
func Int8(v int8) Element          { return Element{Type: TypeInt, Raw: be(uint64(v), 1)} }
func Int16(v int16) Element        { return Element{Type: TypeInt, Raw: be(uint64(v), 2)} }
func Int32(v int32) Element        { return Element{Type: TypeInt, Raw: be(uint64(v), 4)} }
func Int64(v int64) Element        { return Element{Type: TypeInt, Raw: be(uint64(v), 8)} }
func UUID16(v uint16) Element      { return Element{Type: TypeUUID, Raw: be(uint64(v), 2)} }
func UUID32(v uint32) Element      { return Element{Type: TypeUUID, Raw: be(uint64(v), 4)} }
func UUID128(u UUID) Element       { return Element{Type: TypeUUID, Raw: append([]byte(nil), u[:]...)} }
func Text(s string) Element        { return Element{Type: TypeText, Raw: []byte(s)} }
func URL(s string) Element         { return Element{Type: TypeURL, Raw: []byte(s)} }
func Bool(v bool) Element          { return Element{Type: TypeBool, Bool: v} }
func Seq(items ...Element) Element { return Element{Type: TypeSeq, Items: items} }
func Alt(items ...Element) Element { return Element{Type: TypeAlt, Items: items} }

// UUIDElement encodes u in the shortest form the Base UUID allows, which is
// what the spec asks of a sender (Vol 3 Part B 2.5.1).
func UUIDElement(u UUID) Element {
	if v, ok := u.Short(); ok {
		if v <= 0xFFFF {
			return UUID16(uint16(v))
		}
		return UUID32(v)
	}
	return UUID128(u)
}

// AsUint is the value of an unsigned integer up to 64 bits wide.
func (e Element) AsUint() (uint64, bool) {
	if e.Type != TypeUint || len(e.Raw) == 0 || len(e.Raw) > 8 {
		return 0, false
	}
	var v uint64
	for _, b := range e.Raw {
		v = v<<8 | uint64(b)
	}
	return v, true
}

// AsUint16 is AsUint for a field the spec types as uint16 exactly.
func (e Element) AsUint16() (uint16, bool) {
	if e.Type != TypeUint || len(e.Raw) != 2 {
		return 0, false
	}
	return binary.BigEndian.Uint16(e.Raw), true
}

// AsInt is the value of a signed integer up to 64 bits wide, sign-extended.
func (e Element) AsInt() (int64, bool) {
	if e.Type != TypeInt || len(e.Raw) == 0 || len(e.Raw) > 8 {
		return 0, false
	}
	v := int64(int8(e.Raw[0]))
	for _, b := range e.Raw[1:] {
		v = v<<8 | int64(b)
	}
	return v, true
}

// AsUUID is the full 128-bit UUID, whatever width it was sent in.
func (e Element) AsUUID() (UUID, bool) {
	if e.Type != TypeUUID {
		return UUID{}, false
	}
	switch len(e.Raw) {
	case 2:
		return UUIDFrom16(binary.BigEndian.Uint16(e.Raw)), true
	case 4:
		return UUIDFrom32(binary.BigEndian.Uint32(e.Raw)), true
	case 16:
		var u UUID
		copy(u[:], e.Raw)
		return u, true
	}
	return UUID{}, false
}

// AsText is a text or URL string.
func (e Element) AsText() (string, bool) {
	if e.Type != TypeText && e.Type != TypeURL {
		return "", false
	}
	return string(e.Raw), true
}

// Marshal encodes e. Variable-length types take the shortest length prefix.
func (e Element) Marshal() ([]byte, error) { return e.Append(nil) }

// Append encodes e onto b.
func (e Element) Append(b []byte) ([]byte, error) {
	hdr := byte(e.Type) << 3
	switch e.Type {
	case TypeNil:
		return append(b, hdr), nil
	case TypeBool:
		v := byte(0)
		if e.Bool {
			v = 1
		}
		return append(b, hdr, v), nil
	case TypeUint, TypeInt, TypeUUID:
		si, ok := fixedIndex(e.Type, len(e.Raw))
		if !ok {
			return b, fmt.Errorf("sdp: %d-byte value not encodable as type %d", len(e.Raw), e.Type)
		}
		return append(append(b, hdr|si), e.Raw...), nil
	case TypeText, TypeURL:
		return appendVar(b, hdr, e.Raw), nil
	case TypeSeq, TypeAlt:
		var body []byte
		for _, it := range e.Items {
			var err error
			if body, err = it.Append(body); err != nil {
				return b, err
			}
		}
		return appendVar(b, hdr, body), nil
	}
	return b, errBadType
}

// fixedIndex is the size index for a fixed-width value (Vol 3 Part B 3.3,
// Table 3.2): UUIDs come in 2, 4 or 16 bytes only.
func fixedIndex(t Type, n int) (byte, bool) {
	switch n {
	case 1:
		return 0, t != TypeUUID
	case 2:
		return 1, true
	case 4:
		return 2, true
	case 8:
		return 3, t != TypeUUID
	case 16:
		return 4, true
	}
	return 0, false
}

func appendVar(b []byte, hdr byte, body []byte) []byte {
	n := len(body)
	switch {
	case n <= 0xFF:
		b = append(b, hdr|5, byte(n))
	case n <= 0xFFFF:
		b = append(b, hdr|6, byte(n>>8), byte(n))
	default:
		b = append(b, hdr|7, byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
	}
	return append(b, body...)
}

// Decode reads one data element from the front of b and returns what is left.
// It never reads past b and never allocates more than b's length, so a hostile
// length field fails rather than reserving memory.
func Decode(b []byte) (Element, []byte, error) { return decode(b, 0) }

func decode(b []byte, depth int) (Element, []byte, error) {
	if len(b) == 0 {
		return Element{}, nil, errShort
	}
	t, si := Type(b[0]>>3), b[0]&7
	b = b[1:]
	if t > TypeURL {
		return Element{}, nil, errBadType
	}
	if !sizeAllowed(t, si) {
		return Element{}, nil, errBadSize
	}
	if t == TypeNil {
		return Element{Type: TypeNil}, b, nil
	}
	var n uint64
	switch si {
	case 0, 1, 2, 3, 4:
		n = 1 << si
	case 5:
		if len(b) < 1 {
			return Element{}, nil, errShort
		}
		n, b = uint64(b[0]), b[1:]
	case 6:
		if len(b) < 2 {
			return Element{}, nil, errShort
		}
		n, b = uint64(binary.BigEndian.Uint16(b)), b[2:]
	case 7:
		if len(b) < 4 {
			return Element{}, nil, errShort
		}
		n, b = uint64(binary.BigEndian.Uint32(b)), b[4:]
	}
	if n > uint64(len(b)) {
		return Element{}, nil, errShort
	}
	body, rest := b[:n], b[n:]
	e := Element{Type: t}
	switch t {
	case TypeBool:
		// Any non-zero octet reads as true; the spec defines only 0 and 1 but
		// a stricter reading buys nothing for a flag nobody here acts on.
		e.Bool = body[0] != 0
	case TypeSeq, TypeAlt:
		if depth >= maxDepth {
			return Element{}, nil, errDepth
		}
		for len(body) > 0 {
			it, r, err := decode(body, depth+1)
			if err != nil {
				return Element{}, nil, err
			}
			e.Items = append(e.Items, it)
			body = r
		}
	default:
		e.Raw = append([]byte(nil), body...)
	}
	return e, rest, nil
}

// sizeAllowed is Vol 3 Part B 3.3 Table 3.2: which size indices each type may
// use. Nil is index 0 with no data; Bool is index 0, one byte.
func sizeAllowed(t Type, si byte) bool {
	switch t {
	case TypeNil, TypeBool:
		return si == 0
	case TypeUint, TypeInt:
		return si <= 4
	case TypeUUID:
		return si == 1 || si == 2 || si == 4
	case TypeText, TypeSeq, TypeAlt, TypeURL:
		return si >= 5
	}
	return false
}

// DecodeAll decodes exactly one element filling b.
func DecodeAll(b []byte) (Element, error) {
	e, rest, err := Decode(b)
	if err != nil {
		return Element{}, err
	}
	if len(rest) != 0 {
		return Element{}, fmt.Errorf("sdp: %d bytes after the data element", len(rest))
	}
	return e, nil
}
