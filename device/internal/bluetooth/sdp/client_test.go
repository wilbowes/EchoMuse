package sdp

import (
	"errors"
	"testing"
)

// serverRequester runs the client against a Server, as two ends of one L2CAP
// channel with the given MTU.
type serverRequester struct {
	s   *Server
	mtu int
}

func (r serverRequester) Request(pdu []byte) ([]byte, error) { return r.s.Handle(pdu, r.mtu), nil }

type funcRequester func([]byte) ([]byte, error)

func (f funcRequester) Request(pdu []byte) ([]byte, error) { return f(pdu) }

// speaker is a typical sink's database: A2DP sink with speaker features, an
// AVRCP 1.5 target offering category 2 (volume), an AVRCP 1.4 controller, and
// unrelated records that must not confuse the search.
func speaker() *Server {
	avctp := func(v uint16) Element {
		return Seq(Seq(UUID16(UUIDL2CAP), Uint16(PSMAVCTP)), Seq(UUID16(UUIDAVCTP), Uint16(v)))
	}
	return NewServer(
		SDPServerRecord(),
		Record{Handle: 0x10000, Attrs: []Attr{
			{AttrServiceClassIDList, Seq(UUID16(0x111E))}, // Handsfree
			{AttrProtocolDescriptorList, Seq(Seq(UUID16(UUIDL2CAP)), Seq(UUID16(0x0003), Uint8(1)))},
		}},
		Record{Handle: 0x10001, Attrs: []Attr{
			{AttrServiceClassIDList, Seq(UUID128(UUIDFrom16(UUIDAudioSink)))},
			{AttrProtocolDescriptorList, Seq(Seq(UUID16(UUIDL2CAP), Uint16(PSMAVDTP)), Seq(UUID16(UUIDAVDTP), Uint16(0x0103)))},
			{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAdvancedAudio), Uint16(0x0104)))},
			{AttrServiceName, Text("Audio Sink with a long enough name to need several fragments")},
			{AttrSupportedFeatures, Uint16(0x0002)},
		}},
		Record{Handle: 0x10002, Attrs: []Attr{
			{AttrServiceClassIDList, Seq(UUID16(UUIDAVRCPTarget))},
			{AttrProtocolDescriptorList, avctp(0x0104)},
			{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAVRCP), Uint16(0x0105)))},
			{AttrSupportedFeatures, Uint16(0x0002)},
		}},
		Record{Handle: 0x10003, Attrs: []Attr{
			{AttrServiceClassIDList, Seq(UUID16(UUIDAVRCP))},
			{AttrProtocolDescriptorList, avctp(0x0103)},
			{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAVRCP), Uint16(0x0104)))},
			{AttrSupportedFeatures, Uint16(0x0001)},
		}},
	)
}

func TestFindA2DPSink(t *testing.T) {
	// 48 is L2CAP's minimum MTU, so the answer arrives in many fragments.
	for _, mtu := range []int{48, 672} {
		info, err := NewClient(serverRequester{speaker(), mtu}).FindA2DPSink()
		if err != nil {
			t.Fatalf("mtu %d: %v", mtu, err)
		}
		want := SinkInfo{AVDTPPSM: 0x0019, AVDTPVersion: 0x0103, A2DPVersion: 0x0104, Features: 0x0002}
		if info != want {
			t.Errorf("mtu %d: %+v, want %+v", mtu, info, want)
		}
	}
}

func TestFindA2DPSinkAbsent(t *testing.T) {
	_, err := NewClient(serverRequester{defaultServer(), 672}).FindA2DPSink()
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("our own database has no sink: %v", err)
	}
}

func TestFindAVRCP(t *testing.T) {
	recs, err := NewClient(serverRequester{speaker(), 48}).FindAVRCP()
	if err != nil || len(recs) != 2 {
		t.Fatalf("%+v %v", recs, err)
	}
	tg, ct := recs[0], recs[1]
	if !tg.Target || tg.Controller || tg.Version != 0x0105 || !tg.Category(2) || tg.Category(1) || !tg.AbsoluteVolume() {
		t.Errorf("target: %+v", tg)
	}
	if !ct.Controller || ct.Target || ct.AVCTPVersion != 0x0103 || !ct.Category(1) {
		t.Errorf("controller: %+v", ct)
	}
	if (AVRCPInfo{Version: 0x0103}).AbsoluteVolume() {
		t.Error("AVRCP 1.3 has no absolute volume")
	}
	if (AVRCPInfo{Features: 0xFF}).Category(5) || (AVRCPInfo{Features: 0xFF}).Category(0) {
		t.Error("categories are 1-4")
	}
}

// A sink record whose protocol list is an alternative of lists, and which
// omits the L2CAP PSM, still yields AVDTP on 0x0019.
func TestSinkAlternativeAndDefaultPSM(t *testing.T) {
	s := NewServer(Record{Handle: 0x10001, Attrs: []Attr{
		{AttrServiceClassIDList, Seq(UUID16(UUIDAudioSink))},
		{AttrProtocolDescriptorList, Alt(Seq(Seq(UUID16(UUIDL2CAP)), Seq(UUID16(UUIDAVDTP), Uint16(0x0102))))},
	}})
	info, err := NewClient(serverRequester{s, 672}).FindA2DPSink()
	if err != nil || info.AVDTPPSM != PSMAVDTP || info.AVDTPVersion != 0x0102 {
		t.Errorf("%+v %v", info, err)
	}
}

func TestClientFailures(t *testing.T) {
	pattern := []UUID{UUIDFrom16(UUIDAudioSink)}
	tidOf := func(pdu []byte) uint16 { return uint16(pdu[1])<<8 | uint16(pdu[2]) }

	wrongTID := funcRequester(func(pdu []byte) ([]byte, error) {
		return AttrResponse{Lists: []byte{0x35, 0x00}}.Marshal(PDUSearchAttrResponse, tidOf(pdu)+1), nil
	})
	if _, err := NewClient(wrongTID).SearchAttributes(pattern, AllAttrs); err == nil {
		t.Error("a response to another transaction was accepted")
	}

	refused := funcRequester(func(pdu []byte) ([]byte, error) { return ErrorPDU(tidOf(pdu), ErrInsufficientRes), nil })
	var re *RemoteError
	if _, err := NewClient(refused).SearchAttributes(pattern, AllAttrs); !errors.As(err, &re) || re.Code != ErrInsufficientRes {
		t.Errorf("error response: %v", err)
	}

	// A peer that answers every request with another continuation state.
	forever := funcRequester(func(pdu []byte) ([]byte, error) {
		return AttrResponse{Lists: []byte{0x35}, Cont: []byte{1}}.Marshal(PDUSearchAttrResponse, tidOf(pdu)), nil
	})
	if _, err := NewClient(forever).SearchAttributes(pattern, AllAttrs); err == nil {
		t.Error("an endless continuation was followed for ever")
	}

	// One that sends 1KB fragments until the size cap trips first.
	big := make([]byte, 1024)
	flood := funcRequester(func(pdu []byte) ([]byte, error) {
		return AttrResponse{Lists: big, Cont: []byte{1}}.Marshal(PDUSearchAttrResponse, tidOf(pdu)), nil
	})
	if _, err := NewClient(flood).SearchAttributes(pattern, AllAttrs); err == nil {
		t.Error("a response past the size cap was accepted")
	}

	wrongPDU := funcRequester(func(pdu []byte) ([]byte, error) {
		return SearchResponse{}.Marshal(tidOf(pdu)), nil
	})
	if _, err := NewClient(wrongPDU).SearchAttributes(pattern, AllAttrs); err == nil {
		t.Error("a ServiceSearchResponse was accepted for a ServiceSearchAttributeRequest")
	}

	// Structurally wrong lists: not a sequence; an odd item count; a uint32 ID.
	for _, lists := range [][]byte{
		{0x09, 0x00, 0x01},
		{0x35, 0x05, 0x35, 0x03, 0x09, 0x00, 0x01},
		{0x35, 0x09, 0x35, 0x07, 0x0A, 0x00, 0x00, 0x00, 0x01, 0x08, 0x01},
	} {
		lists := lists
		r := funcRequester(func(pdu []byte) ([]byte, error) {
			return AttrResponse{Lists: lists}.Marshal(PDUSearchAttrResponse, tidOf(pdu)), nil
		})
		if _, err := NewClient(r).SearchAttributes(pattern, AllAttrs); err == nil {
			t.Errorf("lists %x accepted", lists)
		}
	}

	// An empty answer is no records, not an error.
	empty := funcRequester(func(pdu []byte) ([]byte, error) {
		return AttrResponse{}.Marshal(PDUSearchAttrResponse, tidOf(pdu)), nil
	})
	if recs, err := NewClient(empty).SearchAttributes(pattern, AllAttrs); err != nil || len(recs) != 0 {
		t.Errorf("empty: %v %v", recs, err)
	}
}

// The transaction ID advances per request, continuations included.
func TestTransactionIDsAdvance(t *testing.T) {
	var seen []uint16
	s := speaker()
	r := funcRequester(func(pdu []byte) ([]byte, error) {
		seen = append(seen, uint16(pdu[1])<<8|uint16(pdu[2]))
		return s.Handle(pdu, 48), nil
	})
	c := NewClient(r)
	if _, err := c.FindA2DPSink(); err != nil {
		t.Fatal(err)
	}
	if len(seen) < 2 {
		t.Fatalf("expected continuations at MTU 48, got %d requests", len(seen))
	}
	for i := 1; i < len(seen); i++ {
		if seen[i] != seen[i-1]+1 {
			t.Errorf("transaction IDs %v", seen)
			break
		}
	}
}

func TestPDURoundTrips(t *testing.T) {
	ss := SearchRequest{Pattern: []UUID{UUIDFrom16(1), UUIDFrom32(0x12345678)}, MaxRecords: 7, Cont: []byte{9}}
	p, _ := ParsePDU(ss.Marshal(3))
	if got, err := ParseSearchRequest(p.Params); err != nil || len(got.Pattern) != 2 || got.Pattern[1] != ss.Pattern[1] || got.MaxRecords != 7 || got.Cont[0] != 9 {
		t.Errorf("SearchRequest: %+v %v", got, err)
	}
	rsp := SearchResponse{Total: 3, Handles: []uint32{1, 2}, Cont: []byte{1, 2}}
	p, _ = ParsePDU(rsp.Marshal(4))
	if got, err := ParseSearchResponse(p.Params); err != nil || got.Total != 3 || len(got.Handles) != 2 || len(got.Cont) != 2 {
		t.Errorf("SearchResponse: %+v %v", got, err)
	}
	// More handles in a fragment than the total is malformed.
	if _, err := ParseSearchResponse([]byte{0, 1, 0, 2, 0, 0, 0, 1, 0, 0, 0, 2, 0}); err == nil {
		t.Error("CurrentServiceRecordCount over Total accepted")
	}
	ar := AttrRequest{Handle: 5, MaxBytes: 100, Attrs: []AttrRange{{1, 1}, {4, 9}}}
	p, _ = ParsePDU(ar.Marshal(5))
	if got, err := ParseAttrRequest(p.Params); err != nil || got.Handle != 5 || len(got.Attrs) != 2 || got.Attrs[1] != (AttrRange{4, 9}) {
		t.Errorf("AttrRequest: %+v %v", got, err)
	}
	if _, err := ParseAttrResponse([]byte{0, 5, 1, 2}); err == nil {
		t.Error("AttributeListByteCount past the end accepted")
	}
	if _, err := ParseAttrResponse([]byte{0, 1, 1, 0, 0}); err == nil {
		t.Error("bytes after the continuation state accepted")
	}
}

func FuzzParseAttrResponse(f *testing.F) {
	f.Add([]byte{0, 2, 0x35, 0x00, 0x00})
	f.Add([]byte{0, 1, 0x35, 0x08, 1, 2, 3, 4, 5, 6, 7, 8})
	f.Fuzz(func(t *testing.T, params []byte) {
		r, err := ParseAttrResponse(params)
		if err != nil {
			return
		}
		if len(r.Cont) > maxCont {
			t.Fatalf("continuation of %d bytes", len(r.Cont))
		}
		_, _ = parseLists(r.Lists)
	})
}
