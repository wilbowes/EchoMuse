package sdp

// The SDP server: answers a peer's searches over our own records.
//
// A speaker that connects to us (reconnecting after a power cycle, or pairing
// from its own button) asks what we are before it opens AVDTP or AVCTP; with
// no answer some refuse the link outright.

import (
	"encoding/binary"
	"hash/crc32"
	"sort"
)

// Attr is one attribute of a record.
type Attr struct {
	ID    uint16
	Value Element
}

// Record is one service record. Attrs are kept in ascending ID order, which a
// response must use (Vol 3 Part B 4.6.2).
type Record struct {
	Handle uint32
	Attrs  []Attr
}

// Attr looks up one attribute.
func (r Record) Attr(id uint16) (Element, bool) {
	for _, a := range r.Attrs {
		if a.ID == id {
			return a.Value, true
		}
	}
	return Element{}, false
}

// contains reports whether u appears anywhere in the record's values, which is
// what a ServiceSearchPattern matches on (Vol 3 Part B 2.5.2).
func (r Record) contains(u UUID) bool {
	for _, a := range r.Attrs {
		if elementHas(a.Value, u) {
			return true
		}
	}
	return false
}

func elementHas(e Element, u UUID) bool {
	if v, ok := e.AsUUID(); ok {
		return v == u
	}
	for _, it := range e.Items {
		if elementHas(it, u) {
			return true
		}
	}
	return false
}

func (r Record) matches(pattern []UUID) bool {
	for _, u := range pattern {
		if !r.contains(u) {
			return false
		}
	}
	return true
}

// list encodes the record's attributes inside rs as an attribute list: a
// sequence of ID, value pairs.
func (r Record) list(rs []AttrRange) Element {
	var items []Element
	for _, a := range r.Attrs {
		for _, rg := range rs {
			if rg.has(a.ID) {
				items = append(items, Uint16(a.ID), a.Value)
				break
			}
		}
	}
	return Seq(items...)
}

// Server answers requests over a fixed set of records. It is stateless across
// requests, so one Server can serve every channel at once: a continuation
// state carries the offset to resume from and a checksum of the request it
// belongs to.
type Server struct {
	records []Record
}

// NewServer sorts each record's attributes and fills in ServiceRecordHandle
// where a record left it out.
func NewServer(records ...Record) *Server {
	s := &Server{}
	for _, r := range records {
		r.Attrs = append([]Attr(nil), r.Attrs...)
		if _, ok := r.Attr(AttrServiceRecordHandle); !ok {
			r.Attrs = append(r.Attrs, Attr{AttrServiceRecordHandle, Uint32(r.Handle)})
		}
		sort.SliceStable(r.Attrs, func(i, j int) bool { return r.Attrs[i].ID < r.Attrs[j].ID })
		s.records = append(s.records, r)
	}
	return s
}

// minMTU is L2CAP's floor for a BR/EDR signalling-capable channel (Vol 3 Part
// A 5.1); a caller passing less is clamped rather than trusted.
const minMTU = 48

// contLen is our continuation state: CRC-32 of the request, then the offset.
const contLen = 8

// Handle answers one request PDU. mtu is the peer's receive MTU on this
// channel; no response is longer.
func (s *Server) Handle(req []byte, mtu int) []byte {
	if mtu < minMTU {
		mtu = minMTU
	}
	p, err := ParsePDU(req)
	if err != nil {
		var tid uint16
		if len(req) >= 3 {
			tid = binary.BigEndian.Uint16(req[1:3])
		}
		return ErrorPDU(tid, ErrInvalidPDUSize)
	}
	switch p.ID {
	case PDUSearchRequest:
		return s.search(p, mtu)
	case PDUAttrRequest:
		return s.attr(p, mtu)
	case PDUSearchAttrRequest:
		return s.searchAttr(p, mtu)
	}
	return ErrorPDU(p.TID, ErrInvalidSyntax)
}

// requestSum identifies a request without its continuation state, so a state
// presented with a different request is refused.
func requestSum(id uint8, params, cont []byte) uint32 {
	body := params[:len(params)-1-len(cont)]
	return crc32.Update(crc32.ChecksumIEEE([]byte{id}), crc32.IEEETable, body)
}

func makeCont(sum uint32, off int) []byte {
	b := binary.BigEndian.AppendUint32(nil, sum)
	return binary.BigEndian.AppendUint32(b, uint32(off))
}

// resume is the offset a request's continuation state asks for: 0 with no
// state, false for one we did not issue for this request.
func resume(sum uint32, cont []byte, limit int) (int, bool) {
	if len(cont) == 0 {
		return 0, true
	}
	if len(cont) != contLen || binary.BigEndian.Uint32(cont) != sum {
		return 0, false
	}
	off := binary.BigEndian.Uint32(cont[4:])
	if uint64(off) > uint64(limit) {
		return 0, false
	}
	return int(off), true
}

func (s *Server) search(p PDU, mtu int) []byte {
	r, err := ParseSearchRequest(p.Params)
	if err != nil || r.MaxRecords == 0 {
		return ErrorPDU(p.TID, ErrInvalidSyntax)
	}
	var all []uint32
	for _, rec := range s.records {
		if rec.matches(r.Pattern) {
			all = append(all, rec.Handle)
		}
	}
	if len(all) > int(r.MaxRecords) {
		all = all[:r.MaxRecords]
	}
	off, ok := resume(requestSum(p.ID, p.Params, r.Cont), r.Cont, len(all))
	if !ok {
		return ErrorPDU(p.TID, ErrInvalidContinuation)
	}
	// Header, the two counts, the largest continuation state we send.
	per := (mtu - hdrLen - 4 - 1 - contLen) / 4
	end := off + per
	var cont []byte
	if end < len(all) {
		cont = makeCont(requestSum(p.ID, p.Params, r.Cont), end)
	} else {
		end = len(all)
	}
	return SearchResponse{Total: uint16(len(all)), Handles: all[off:end], Cont: cont}.Marshal(p.TID)
}

func (s *Server) attr(p PDU, mtu int) []byte {
	r, err := ParseAttrRequest(p.Params)
	if err != nil || r.MaxBytes < minAttrBytes {
		return ErrorPDU(p.TID, ErrInvalidSyntax)
	}
	for _, rec := range s.records {
		if rec.Handle == r.Handle {
			full := mustAppend(nil, rec.list(r.Attrs))
			return fragment(p, r.Cont, full, int(r.MaxBytes), mtu, PDUAttrResponse)
		}
	}
	return ErrorPDU(p.TID, ErrInvalidHandle)
}

func (s *Server) searchAttr(p PDU, mtu int) []byte {
	r, err := ParseSearchAttrRequest(p.Params)
	if err != nil || r.MaxBytes < minAttrBytes {
		return ErrorPDU(p.TID, ErrInvalidSyntax)
	}
	var lists []Element
	for _, rec := range s.records {
		if !rec.matches(r.Pattern) {
			continue
		}
		// A matching record with none of the asked-for attributes adds
		// nothing; omitting it is what BlueZ does and what clients expect.
		if l := rec.list(r.Attrs); len(l.Items) > 0 {
			lists = append(lists, l)
		}
	}
	full := mustAppend(nil, Seq(lists...))
	return fragment(p, r.Cont, full, int(r.MaxBytes), mtu, PDUSearchAttrResponse)
}

// fragment answers with the part of full that starts at the request's
// continuation offset and fits both MaximumAttributeByteCount and the MTU.
// The full list is rebuilt for every fragment: our records are a few hundred
// bytes and never change, so state between requests would buy nothing.
func fragment(p PDU, cont, full []byte, maxBytes, mtu int, id uint8) []byte {
	sum := requestSum(p.ID, p.Params, cont)
	off, ok := resume(sum, cont, len(full))
	if !ok {
		return ErrorPDU(p.TID, ErrInvalidContinuation)
	}
	chunk := mtu - hdrLen - 2 - 1 - contLen
	if maxBytes < chunk {
		chunk = maxBytes
	}
	end := off + chunk
	var next []byte
	if end < len(full) {
		next = makeCont(sum, end)
	} else {
		end = len(full)
	}
	return AttrResponse{Lists: full[off:end], Cont: next}.Marshal(id, p.TID)
}
