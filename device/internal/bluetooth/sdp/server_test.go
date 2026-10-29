package sdp

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func defaultServer() *Server { return NewServer(DefaultRecords()...) }

func errorCode(t *testing.T, rsp []byte) uint16 {
	t.Helper()
	p, err := ParsePDU(rsp)
	if err != nil || p.ID != PDUErrorResponse || len(p.Params) != 2 {
		t.Fatalf("not an ErrorResponse: %x", rsp)
	}
	return binary.BigEndian.Uint16(p.Params)
}

// The request side of BlueZ unit/test-sdp.c's PTS error cases (TP/SERVER/SS,
// SA, SSA BI-xx-C): each must draw the error code PTS expects, with the
// request's transaction ID.
func TestBlueZErrorVectors(t *testing.T) {
	cases := []struct {
		name string
		req  []byte
		code uint16
	}{
		{"SS/BI-01-C/UUID-16", []byte{0x02, 0x00, 0x01, 0x00, 0x0d, 0x35, 0x03, 0x19, 0x01, 0x00, 0x00, 0x05, 0x00}, ErrInvalidPDUSize},
		{"SS/BI-01-C/UUID-32", []byte{0x02, 0x00, 0x01, 0x00, 0x0f, 0x35, 0x05, 0x1a, 0x00, 0x00, 0x01, 0x00, 0x00, 0x05, 0x00}, ErrInvalidPDUSize},
		{"SS/BI-02-C/UUID-16", []byte{0x02, 0x00, 0x01, 0x00, 0x06, 0x35, 0x03, 0x19, 0x01, 0x00, 0x00}, ErrInvalidSyntax},
		{"SS/BI-02-C/UUID-32", []byte{0x02, 0x00, 0x01, 0x00, 0x08, 0x35, 0x05, 0x1a, 0x00, 0x00, 0x01, 0x00, 0x00}, ErrInvalidSyntax},
		{"SS/BI-02-C/UUID-128", []byte{0x02, 0x00, 0x01, 0x00, 0x14, 0x35, 0x11, 0x1c,
			0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5f, 0x9b, 0x34, 0xfb, 0x00}, ErrInvalidSyntax},
		{"SA/BI-01-C", []byte{0x04, 0x00, 0x01, 0x00, 0x0c, 0xff, 0xff, 0xff, 0xff, 0x00, 0x07, 0x35, 0x03, 0x09, 0x00, 0x01, 0x00}, ErrInvalidHandle},
		{"SA/BI-02-C", []byte{0x04, 0x00, 0x01, 0x00, 0x0a, 0x00, 0x01, 0x00, 0x00, 0x35, 0x03, 0x09, 0x00, 0x01, 0x00}, ErrInvalidSyntax},
		{"SA/BI-03-C", []byte{0x04, 0x00, 0x01, 0x00, 0x11, 0x00, 0x01, 0x00, 0x00, 0x00, 0x07, 0x35, 0x03, 0x09, 0x00, 0x01, 0x00}, ErrInvalidPDUSize},
		{"SSA/BI-01-C/UUID-16", []byte{0x06, 0x00, 0x01, 0x00, 0x0b, 0x35, 0x03, 0x19, 0x01, 0x00, 0x35, 0x03, 0x09, 0x00, 0x01, 0x00}, ErrInvalidSyntax},
		{"SSA/BI-02-C/UUID-16", []byte{0x06, 0x00, 0x01, 0x00, 0x12, 0x35, 0x03, 0x19, 0x01, 0x00, 0xff, 0xff, 0x35, 0x03, 0x09, 0x00, 0x01, 0x00}, ErrInvalidPDUSize},
	}
	s := defaultServer()
	for _, c := range cases {
		rsp := s.Handle(c.req, 48)
		if got := errorCode(t, rsp); got != c.code {
			t.Errorf("%s: code 0x%04x, want 0x%04x", c.name, got, c.code)
		}
		if rsp[1] != 0x00 || rsp[2] != 0x01 {
			t.Errorf("%s: transaction ID not echoed: %x", c.name, rsp)
		}
	}
}

func TestOtherErrors(t *testing.T) {
	s := defaultServer()
	if got := errorCode(t, s.Handle([]byte{0x01, 0, 9}, 672)); got != ErrInvalidPDUSize {
		t.Errorf("short header: 0x%04x", got)
	}
	if got := errorCode(t, s.Handle(PDU{0x03, 4, []byte{0, 0, 0, 0, 0}}.Marshal(), 672)); got != ErrInvalidSyntax {
		t.Errorf("a response PDU sent as a request: 0x%04x", got)
	}
	zero := SearchRequest{Pattern: []UUID{UUIDFrom16(UUIDL2CAP)}, MaxRecords: 0}.Marshal(5)
	if got := errorCode(t, s.Handle(zero, 672)); got != ErrInvalidSyntax {
		t.Errorf("MaximumServiceRecordCount 0: 0x%04x", got)
	}
	small := AttrRequest{Handle: HandleA2DPSource, MaxBytes: 6, Attrs: AllAttrs}.Marshal(6)
	if got := errorCode(t, s.Handle(small, 672)); got != ErrInvalidSyntax {
		t.Errorf("MaximumAttributeByteCount 6: 0x%04x", got)
	}
	thirteen := make([]UUID, 13)
	for i := range thirteen {
		thirteen[i] = UUIDFrom16(UUIDL2CAP)
	}
	if got := errorCode(t, s.Handle(SearchRequest{Pattern: thirteen, MaxRecords: 1}.Marshal(7), 672)); got != ErrInvalidSyntax {
		t.Errorf("13 UUIDs in a pattern: 0x%04x", got)
	}
	backwards := []byte{0x04, 0, 8, 0, 0x0e, 0x00, 0x01, 0x00, 0x01, 0x00, 0x40, 0x35, 0x05, 0x0a, 0x00, 0x09, 0x00, 0x01, 0x00}
	if got := errorCode(t, s.Handle(backwards, 672)); got != ErrInvalidSyntax {
		t.Errorf("a range with low > high: 0x%04x", got)
	}
	long := AttrRequest{Handle: HandleA2DPSource, MaxBytes: 100, Attrs: AllAttrs, Cont: make([]byte, 17)}.Marshal(9)
	if got := errorCode(t, s.Handle(long, 672)); got != ErrInvalidSyntax {
		t.Errorf("a 17-byte continuation state: 0x%04x", got)
	}
}

// TP/SERVER/ROB/BI-01-C's point, against our own state: a continuation state
// we did not issue, or issued for another request, is refused.
func TestForeignContinuationRefused(t *testing.T) {
	s := defaultServer()
	first := AttrRequest{Handle: HandleA2DPSource, MaxBytes: 7, Attrs: AllAttrs}
	rsp := mustAttr(t, s.Handle(first.Marshal(1), 48))
	if len(rsp.Cont) == 0 {
		t.Fatal("7 bytes per fragment must need a continuation")
	}
	for name, req := range map[string]AttrRequest{
		"made up":         {Handle: HandleA2DPSource, MaxBytes: 7, Attrs: AllAttrs, Cont: []byte{1, 2, 3, 4, 5, 6, 7, 8}},
		"wrong length":    {Handle: HandleA2DPSource, MaxBytes: 7, Attrs: AllAttrs, Cont: rsp.Cont[:4]},
		"another record":  {Handle: HandleAVRCPTarget, MaxBytes: 7, Attrs: AllAttrs, Cont: rsp.Cont},
		"other attrs":     {Handle: HandleA2DPSource, MaxBytes: 7, Attrs: []AttrRange{{1, 1}}, Cont: rsp.Cont},
		"other max bytes": {Handle: HandleA2DPSource, MaxBytes: 8, Attrs: AllAttrs, Cont: rsp.Cont},
	} {
		if got := errorCode(t, s.Handle(req.Marshal(2), 48)); got != ErrInvalidContinuation {
			t.Errorf("%s: 0x%04x", name, got)
		}
	}
	past := append([]byte(nil), rsp.Cont...)
	binary.BigEndian.PutUint32(past[4:], 1<<20)
	req := first
	req.Cont = past
	if got := errorCode(t, s.Handle(req.Marshal(3), 48)); got != ErrInvalidContinuation {
		t.Errorf("offset past the end: 0x%04x", got)
	}
}

func mustAttr(t *testing.T, rsp []byte) AttrResponse {
	t.Helper()
	p, err := ParsePDU(rsp)
	if err != nil || (p.ID != PDUAttrResponse && p.ID != PDUSearchAttrResponse) {
		t.Fatalf("not an attribute response: %x", rsp)
	}
	r, err := ParseAttrResponse(p.Params)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

// Fetching a record 7 bytes at a time must reassemble to the one-shot answer,
// with no fragment over MaxBytes or the MTU.
func TestAttrContinuationReassembles(t *testing.T) {
	s := defaultServer()
	whole := mustAttr(t, s.Handle(AttrRequest{Handle: HandleA2DPSource, MaxBytes: 0xFFFF, Attrs: AllAttrs}.Marshal(1), 672))
	if len(whole.Cont) != 0 {
		t.Fatal("the record fits one 672-byte response")
	}
	var got, cont []byte
	rounds := 0
	for {
		rounds++
		raw := s.Handle(AttrRequest{Handle: HandleA2DPSource, MaxBytes: 7, Attrs: AllAttrs, Cont: cont}.Marshal(uint16(rounds)), 48)
		if len(raw) > 48 {
			t.Fatalf("response of %d bytes over a 48-byte MTU", len(raw))
		}
		r := mustAttr(t, raw)
		if len(r.Lists) > 7 {
			t.Fatalf("fragment of %d bytes over MaximumAttributeByteCount 7", len(r.Lists))
		}
		got = append(got, r.Lists...)
		if len(r.Cont) == 0 {
			break
		}
		cont = r.Cont
	}
	if !bytes.Equal(got, whole.Lists) || rounds < 10 {
		t.Errorf("%d rounds reassembled %x, want %x", rounds, got, whole.Lists)
	}
	rec, err := DecodeAll(got)
	if err != nil || rec.Type != TypeSeq {
		t.Fatalf("reassembled list does not decode: %v", err)
	}
	// Ascending attribute IDs (Vol 3 Part B 4.6.2).
	last := -1
	for i := 0; i < len(rec.Items); i += 2 {
		id, _ := rec.Items[i].AsUint16()
		if int(id) <= last {
			t.Errorf("attribute 0x%04x after 0x%04x", id, last)
		}
		last = int(id)
	}
}

// The MTU bounds the fragment even when MaxBytes allows more.
func TestMTUBoundsFragments(t *testing.T) {
	s := defaultServer()
	req := SearchAttrRequest{Pattern: []UUID{UUIDFrom16(UUIDL2CAP)}, MaxBytes: 0xFFFF, Attrs: AllAttrs}
	raw := s.Handle(req.Marshal(1), 48)
	if len(raw) > 48 {
		t.Fatalf("%d bytes over a 48-byte MTU", len(raw))
	}
	if r := mustAttr(t, raw); len(r.Cont) == 0 {
		t.Error("every record in one 48-byte response is impossible")
	}
	if raw := s.Handle(req.Marshal(1), 10); len(raw) > 48 {
		t.Errorf("an MTU under L2CAP's 48-byte floor is clamped to it, not exceeded: %d", len(raw))
	}
}

// ServiceSearch splits across responses by MTU, and Total counts all of them.
func TestSearchContinuation(t *testing.T) {
	var recs []Record
	for i := 0; i < 30; i++ {
		recs = append(recs, Record{Handle: uint32(0x10000 + i), Attrs: []Attr{{AttrServiceClassIDList, Seq(UUID16(UUIDAudioSink))}}})
	}
	s := NewServer(recs...)
	var handles []uint32
	var cont []byte
	for rounds := 0; ; rounds++ {
		if rounds > 30 {
			t.Fatal("no end")
		}
		raw := s.Handle(SearchRequest{Pattern: []UUID{UUIDFrom16(UUIDAudioSink)}, MaxRecords: 25, Cont: cont}.Marshal(1), 48)
		if len(raw) > 48 {
			t.Fatalf("%d bytes over MTU", len(raw))
		}
		p, _ := ParsePDU(raw)
		r, err := ParseSearchResponse(p.Params)
		if err != nil {
			t.Fatal(err)
		}
		if r.Total != 25 {
			t.Errorf("Total %d, want MaximumServiceRecordCount 25", r.Total)
		}
		handles = append(handles, r.Handles...)
		if len(r.Cont) == 0 {
			break
		}
		cont = r.Cont
	}
	if len(handles) != 25 || handles[0] != 0x10000 || handles[24] != 0x10018 {
		t.Errorf("handles %x", handles)
	}
}

// A pattern matches however its UUIDs, and the record's, are spelled.
func TestSearchUUIDEquivalence(t *testing.T) {
	s := NewServer(Record{Handle: 0x10001, Attrs: []Attr{
		{AttrServiceClassIDList, Seq(UUID128(UUIDFrom16(UUIDAudioSource)))},
	}}, A2DPSourceRecord())
	pattern := func(b []byte) []byte {
		params := append(append([]byte{0x35, byte(len(b))}, b...), 0x00, 0x10, 0x00)
		return PDU{PDUSearchRequest, 1, params}.Marshal()
	}
	for _, uuid := range [][]byte{
		{0x19, 0x11, 0x0A},
		{0x1A, 0x00, 0x00, 0x11, 0x0A},
		{0x1C, 0x00, 0x00, 0x11, 0x0A, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB},
	} {
		p, _ := ParsePDU(s.Handle(pattern(uuid), 672))
		r, err := ParseSearchResponse(p.Params)
		if err != nil || r.Total != 2 {
			t.Errorf("%x: %+v %v", uuid, r, err)
		}
	}
	// Every UUID in the pattern must be present: AVDTP and AVCTP together
	// match nothing.
	both := SearchRequest{Pattern: []UUID{UUIDFrom16(UUIDAVDTP), UUIDFrom16(UUIDAVCTP)}, MaxRecords: 9}
	p, _ := ParsePDU(defaultServer().Handle(both.Marshal(2), 672))
	if r, _ := ParseSearchResponse(p.Params); r.Total != 0 {
		t.Errorf("AVDTP+AVCTP matched %d records", r.Total)
	}
}

func TestAttrRanges(t *testing.T) {
	s := defaultServer()
	get := func(rs []AttrRange) []uint16 {
		r := mustAttr(t, s.Handle(AttrRequest{Handle: HandleA2DPSource, MaxBytes: 0xFFFF, Attrs: rs}.Marshal(1), 672))
		e, err := DecodeAll(r.Lists)
		if err != nil {
			t.Fatal(err)
		}
		var ids []uint16
		for i := 0; i < len(e.Items); i += 2 {
			id, _ := e.Items[i].AsUint16()
			ids = append(ids, id)
		}
		return ids
	}
	if ids := get([]AttrRange{{0x0004, 0x0009}}); len(ids) != 4 || ids[0] != 0x0004 || ids[3] != 0x0009 {
		t.Errorf("range 4-9: %x", ids)
	}
	if ids := get([]AttrRange{{0x0311, 0x0311}, {0x0000, 0x0000}}); len(ids) != 2 || ids[0] != 0 || ids[1] != 0x0311 {
		t.Errorf("two singles out of order still answer in ascending order: %x", ids)
	}
	if ids := get([]AttrRange{{0x0400, 0x0500}}); len(ids) != 0 {
		t.Errorf("a range matching nothing is an empty list: %x", ids)
	}
	if ids := get(AllAttrs); len(ids) != 8 {
		t.Errorf("all attributes: %x", ids)
	}
}

// What a speaker reading our A2DP source record gets: the class, AVDTP on
// PSM 0x19 at version 1.3, A2DP 1.3.
func TestOurRecordsReadBack(t *testing.T) {
	c := NewClient(serverRequester{defaultServer(), 48})
	recs, err := c.SearchAttributes([]UUID{UUIDFrom16(UUIDAudioSource)}, AllAttrs)
	if err != nil || len(recs) != 1 {
		t.Fatalf("%d records, %v", len(recs), err)
	}
	r := recs[0]
	pdl, _ := r.Attr(AttrProtocolDescriptorList)
	if psm, _ := protocolParam(pdl, UUIDFrom16(UUIDL2CAP)); psm != PSMAVDTP {
		t.Errorf("PSM 0x%04x", psm)
	}
	if v, _ := protocolParam(pdl, UUIDFrom16(UUIDAVDTP)); v != 0x0103 {
		t.Errorf("AVDTP 0x%04x", v)
	}
	prof, _ := r.Attr(AttrProfileDescriptorList)
	if v, _ := profileVersion(prof, UUIDFrom16(UUIDAdvancedAudio)); v != 0x0103 {
		t.Errorf("A2DP 0x%04x", v)
	}
	if name, _ := r.Attr(AttrServiceName); name.Type != TypeText {
		t.Error("no ServiceName at the language base")
	}
	sdpRecs, err := c.SearchAttributes([]UUID{UUIDFrom16(UUIDSDPServer)}, AllAttrs)
	if err != nil || len(sdpRecs) != 1 || sdpRecs[0].Handle != 0 {
		t.Errorf("SDP server record: %+v %v", sdpRecs, err)
	}
	ct, err := c.FindAVRCP()
	if err != nil || len(ct) != 2 {
		t.Fatalf("AVRCP records: %+v %v", ct, err)
	}
	for _, a := range ct {
		if a.Version != 0x0106 || a.AVCTPPSM != PSMAVCTP || a.AVCTPVersion != 0x0104 || !a.AbsoluteVolume() {
			t.Errorf("%+v", a)
		}
		if a.Controller && !a.Category(2) || a.Target && !a.Category(1) {
			t.Errorf("categories: %+v", a)
		}
	}
}

func FuzzServerHandle(f *testing.F) {
	s := defaultServer()
	f.Add(SearchAttrRequest{Pattern: []UUID{UUIDFrom16(UUIDL2CAP)}, MaxBytes: 7, Attrs: AllAttrs}.Marshal(1), 48)
	f.Add(AttrRequest{Handle: HandleA2DPSource, MaxBytes: 9, Attrs: AllAttrs, Cont: []byte{1, 2, 3, 4, 0, 0, 0, 9}}.Marshal(2), 672)
	f.Add(SearchRequest{Pattern: []UUID{UUIDFrom16(UUIDAudioSink)}, MaxRecords: 3}.Marshal(3), 100)
	f.Fuzz(func(t *testing.T, req []byte, mtu int) {
		rsp := s.Handle(req, mtu)
		limit := mtu
		if limit < minMTU {
			limit = minMTU
		}
		if len(rsp) > limit {
			t.Fatalf("response of %d bytes over MTU %d", len(rsp), limit)
		}
		if _, err := ParsePDU(rsp); err != nil {
			t.Fatalf("server sent a malformed PDU: %x", rsp)
		}
	})
}
