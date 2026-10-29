package sdp

// SDP PDUs (Vol 3 Part B 4): PDU ID (1), Transaction ID (2), Parameter Length
// (2), parameters. Every request and response that can be split ends in a
// continuation state: one InfoLength octet (0-16) and that many opaque bytes.

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// PDU IDs (Vol 3 Part B 4.2).
const (
	PDUErrorResponse      = 0x01
	PDUSearchRequest      = 0x02
	PDUSearchResponse     = 0x03
	PDUAttrRequest        = 0x04
	PDUAttrResponse       = 0x05
	PDUSearchAttrRequest  = 0x06
	PDUSearchAttrResponse = 0x07
)

// Error codes (Vol 3 Part B 4.4.1).
const (
	ErrInvalidVersion      uint16 = 0x0001
	ErrInvalidHandle       uint16 = 0x0002
	ErrInvalidSyntax       uint16 = 0x0003
	ErrInvalidPDUSize      uint16 = 0x0004
	ErrInvalidContinuation uint16 = 0x0005
	ErrInsufficientRes     uint16 = 0x0006
)

const (
	hdrLen  = 5
	maxCont = 16 // InfoLength's ceiling (Vol 3 Part B 4.3)

	// maxPattern is the ServiceSearchPattern ceiling (Vol 3 Part B 4.5.1).
	maxPattern = 12

	// minAttrBytes is the smallest MaximumAttributeByteCount a request may
	// carry (Vol 3 Part B 4.6.1).
	minAttrBytes = 7
)

var (
	errSyntax  = errors.New("sdp: invalid request syntax")
	errPDUSize = errors.New("sdp: parameter length does not match the PDU")
)

// PDU is one SDP PDU with its parameters undecoded.
type PDU struct {
	ID     uint8
	TID    uint16
	Params []byte
}

// ParsePDU checks the header against the bytes received: a parameter length
// that disagrees with the PDU is the spec's InvalidPduSize, never truncated
// or padded into something else.
func ParsePDU(b []byte) (PDU, error) {
	if len(b) < hdrLen {
		return PDU{}, errPDUSize
	}
	if int(binary.BigEndian.Uint16(b[3:5])) != len(b)-hdrLen {
		return PDU{}, errPDUSize
	}
	return PDU{ID: b[0], TID: binary.BigEndian.Uint16(b[1:3]), Params: b[hdrLen:]}, nil
}

// Marshal frames p.
func (p PDU) Marshal() []byte {
	b := make([]byte, hdrLen, hdrLen+len(p.Params))
	b[0] = p.ID
	binary.BigEndian.PutUint16(b[1:3], p.TID)
	binary.BigEndian.PutUint16(b[3:5], uint16(len(p.Params)))
	return append(b, p.Params...)
}

func appendCont(b, cont []byte) []byte {
	return append(append(b, byte(len(cont))), cont...)
}

// readCont reads a continuation state that must be all of b: the parameters
// are parsed forward and the state is whatever is left, so bytes after it are
// a syntax error rather than ignored.
func readCont(b []byte) ([]byte, error) {
	if len(b) < 1 || int(b[0]) > maxCont || len(b) != 1+int(b[0]) {
		return nil, errSyntax
	}
	if b[0] == 0 {
		return nil, nil
	}
	return append([]byte(nil), b[1:]...), nil
}

// AttrRange is an inclusive attribute ID range; Lo == Hi is a single ID.
type AttrRange struct{ Lo, Hi uint16 }

// AllAttrs asks for every attribute.
var AllAttrs = []AttrRange{{0x0000, 0xFFFF}}

func (r AttrRange) has(id uint16) bool { return id >= r.Lo && id <= r.Hi }

func patternElement(p []UUID) Element {
	items := make([]Element, len(p))
	for i, u := range p {
		items[i] = UUIDElement(u)
	}
	return Seq(items...)
}

// readPattern reads a ServiceSearchPattern: a sequence of 1-12 UUIDs.
func readPattern(b []byte) ([]UUID, []byte, error) {
	e, rest, err := Decode(b)
	if err != nil || e.Type != TypeSeq || len(e.Items) == 0 || len(e.Items) > maxPattern {
		return nil, nil, errSyntax
	}
	out := make([]UUID, len(e.Items))
	for i, it := range e.Items {
		u, ok := it.AsUUID()
		if !ok {
			return nil, nil, errSyntax
		}
		out[i] = u
	}
	return out, rest, nil
}

// attrListElement encodes an AttributeIDList: a single ID as uint16, a range
// as uint32 with the low ID in the high half (Vol 3 Part B 4.6.1).
func attrListElement(rs []AttrRange) Element {
	items := make([]Element, len(rs))
	for i, r := range rs {
		if r.Lo == r.Hi {
			items[i] = Uint16(r.Lo)
		} else {
			items[i] = Uint32(uint32(r.Lo)<<16 | uint32(r.Hi))
		}
	}
	return Seq(items...)
}

// readAttrList reads an AttributeIDList. Ascending order is what a sender
// should produce; a peer that sends them out of order still gets an answer.
func readAttrList(b []byte) ([]AttrRange, []byte, error) {
	e, rest, err := Decode(b)
	if err != nil || e.Type != TypeSeq || len(e.Items) == 0 {
		return nil, nil, errSyntax
	}
	out := make([]AttrRange, len(e.Items))
	for i, it := range e.Items {
		if it.Type != TypeUint {
			return nil, nil, errSyntax
		}
		switch len(it.Raw) {
		case 2:
			id := binary.BigEndian.Uint16(it.Raw)
			out[i] = AttrRange{id, id}
		case 4:
			lo, hi := binary.BigEndian.Uint16(it.Raw[0:2]), binary.BigEndian.Uint16(it.Raw[2:4])
			if lo > hi {
				return nil, nil, errSyntax
			}
			out[i] = AttrRange{lo, hi}
		default:
			return nil, nil, errSyntax
		}
	}
	return out, rest, nil
}

func mustAppend(b []byte, e Element) []byte {
	b, err := e.Append(b)
	if err != nil {
		// Only reachable from a malformed Element built in this package.
		panic(fmt.Sprintf("sdp: %v", err))
	}
	return b
}

// SearchRequest is ServiceSearchRequest (Vol 3 Part B 4.5.1).
type SearchRequest struct {
	Pattern    []UUID
	MaxRecords uint16
	Cont       []byte
}

func (r SearchRequest) Marshal(tid uint16) []byte {
	p := mustAppend(nil, patternElement(r.Pattern))
	p = binary.BigEndian.AppendUint16(p, r.MaxRecords)
	return PDU{PDUSearchRequest, tid, appendCont(p, r.Cont)}.Marshal()
}

func ParseSearchRequest(params []byte) (SearchRequest, error) {
	pat, rest, err := readPattern(params)
	if err != nil || len(rest) < 2 {
		return SearchRequest{}, errSyntax
	}
	r := SearchRequest{Pattern: pat, MaxRecords: binary.BigEndian.Uint16(rest)}
	if r.Cont, err = readCont(rest[2:]); err != nil {
		return SearchRequest{}, err
	}
	return r, nil
}

// SearchResponse is ServiceSearchResponse (Vol 3 Part B 4.5.2). Total is the
// count across every fragment; Handles are this fragment's.
type SearchResponse struct {
	Total   uint16
	Handles []uint32
	Cont    []byte
}

func (r SearchResponse) Marshal(tid uint16) []byte {
	p := binary.BigEndian.AppendUint16(nil, r.Total)
	p = binary.BigEndian.AppendUint16(p, uint16(len(r.Handles)))
	for _, h := range r.Handles {
		p = binary.BigEndian.AppendUint32(p, h)
	}
	return PDU{PDUSearchResponse, tid, appendCont(p, r.Cont)}.Marshal()
}

func ParseSearchResponse(params []byte) (SearchResponse, error) {
	if len(params) < 4 {
		return SearchResponse{}, errSyntax
	}
	r := SearchResponse{Total: binary.BigEndian.Uint16(params)}
	n := int(binary.BigEndian.Uint16(params[2:]))
	if n > int(r.Total) || len(params) < 4+4*n {
		return SearchResponse{}, errSyntax
	}
	for i := 0; i < n; i++ {
		r.Handles = append(r.Handles, binary.BigEndian.Uint32(params[4+4*i:]))
	}
	var err error
	if r.Cont, err = readCont(params[4+4*n:]); err != nil {
		return SearchResponse{}, err
	}
	return r, nil
}

// AttrRequest is ServiceAttributeRequest (Vol 3 Part B 4.6.1).
type AttrRequest struct {
	Handle   uint32
	MaxBytes uint16
	Attrs    []AttrRange
	Cont     []byte
}

func (r AttrRequest) Marshal(tid uint16) []byte {
	p := binary.BigEndian.AppendUint32(nil, r.Handle)
	p = binary.BigEndian.AppendUint16(p, r.MaxBytes)
	p = mustAppend(p, attrListElement(r.Attrs))
	return PDU{PDUAttrRequest, tid, appendCont(p, r.Cont)}.Marshal()
}

func ParseAttrRequest(params []byte) (AttrRequest, error) {
	if len(params) < 6 {
		return AttrRequest{}, errSyntax
	}
	r := AttrRequest{Handle: binary.BigEndian.Uint32(params), MaxBytes: binary.BigEndian.Uint16(params[4:])}
	attrs, rest, err := readAttrList(params[6:])
	if err != nil {
		return AttrRequest{}, err
	}
	r.Attrs = attrs
	if r.Cont, err = readCont(rest); err != nil {
		return AttrRequest{}, err
	}
	return r, nil
}

// SearchAttrRequest is ServiceSearchAttributeRequest (Vol 3 Part B 4.7.1).
type SearchAttrRequest struct {
	Pattern  []UUID
	MaxBytes uint16
	Attrs    []AttrRange
	Cont     []byte
}

func (r SearchAttrRequest) Marshal(tid uint16) []byte {
	p := mustAppend(nil, patternElement(r.Pattern))
	p = binary.BigEndian.AppendUint16(p, r.MaxBytes)
	p = mustAppend(p, attrListElement(r.Attrs))
	return PDU{PDUSearchAttrRequest, tid, appendCont(p, r.Cont)}.Marshal()
}

func ParseSearchAttrRequest(params []byte) (SearchAttrRequest, error) {
	pat, rest, err := readPattern(params)
	if err != nil || len(rest) < 2 {
		return SearchAttrRequest{}, errSyntax
	}
	r := SearchAttrRequest{Pattern: pat, MaxBytes: binary.BigEndian.Uint16(rest)}
	attrs, rest, err := readAttrList(rest[2:])
	if err != nil {
		return SearchAttrRequest{}, err
	}
	r.Attrs = attrs
	if r.Cont, err = readCont(rest); err != nil {
		return SearchAttrRequest{}, err
	}
	return r, nil
}

// AttrResponse is ServiceAttributeResponse and ServiceSearchAttributeResponse
// (Vol 3 Part B 4.6.2, 4.7.2), which share a shape: a byte count, that many
// bytes of the (possibly split) attribute list(s), a continuation state.
type AttrResponse struct {
	Lists []byte
	Cont  []byte
}

func (r AttrResponse) Marshal(id uint8, tid uint16) []byte {
	p := binary.BigEndian.AppendUint16(nil, uint16(len(r.Lists)))
	p = append(p, r.Lists...)
	return PDU{id, tid, appendCont(p, r.Cont)}.Marshal()
}

func ParseAttrResponse(params []byte) (AttrResponse, error) {
	if len(params) < 2 {
		return AttrResponse{}, errSyntax
	}
	n := int(binary.BigEndian.Uint16(params))
	if len(params) < 2+n {
		return AttrResponse{}, errSyntax
	}
	r := AttrResponse{Lists: append([]byte(nil), params[2:2+n]...)}
	var err error
	if r.Cont, err = readCont(params[2+n:]); err != nil {
		return AttrResponse{}, err
	}
	return r, nil
}

// ErrorPDU is an ErrorResponse (Vol 3 Part B 4.4.1). The ErrorInfo field of
// earlier versions is gone; nothing is sent after the code.
func ErrorPDU(tid, code uint16) []byte {
	return PDU{PDUErrorResponse, tid, binary.BigEndian.AppendUint16(nil, code)}.Marshal()
}

// RemoteError is an ErrorResponse from the peer.
type RemoteError struct{ Code uint16 }

func (e *RemoteError) Error() string { return fmt.Sprintf("sdp: peer answered error 0x%04x", e.Code) }
