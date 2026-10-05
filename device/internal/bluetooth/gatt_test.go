package bluetooth

import (
	"bytes"
	"encoding/binary"
	"errors"
	"reflect"
	"testing"
)

// A 128-bit UUID as ATT carries it (little-endian) and as people write it.
var (
	uuid128Wire = UUID{0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x01, 0x00, 0x40, 0x6E}
	uuid128Text = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
)

func TestUUIDForms(t *testing.T) {
	if got := UUID16(0x2A00).String(); got != "00002a00-0000-1000-8000-00805f9b34fb" {
		t.Fatalf("16-bit on the base UUID: %s", got)
	}
	if got := uuid128Wire.String(); got != uuid128Text {
		t.Fatalf("128-bit: %s", got)
	}
	if !UUID16(0x2803).Is16(0x2803) || uuid128Wire.Is16(0xCA9E) {
		t.Fatal("Is16")
	}
}

func TestATTRequestLayouts(t *testing.T) {
	for _, c := range []struct {
		name      string
		got, want []byte
	}{
		{"mtu", encodeMTUReq(185), []byte{0x02, 0xB9, 0x00}},
		{"read by group", encodeReadByGroupReq(1, 0xFFFF, UUID16(0x2800)), []byte{0x10, 0x01, 0x00, 0xFF, 0xFF, 0x00, 0x28}},
		{"read by type", encodeReadByTypeReq(0x0010, 0x0020, UUID16(0x2803)), []byte{0x08, 0x10, 0x00, 0x20, 0x00, 0x03, 0x28}},
		{"find info", encodeFindInfoReq(0x0003, 0x0004), []byte{0x04, 0x03, 0x00, 0x04, 0x00}},
		{"read", encodeReadReq(0x1234), []byte{0x0A, 0x34, 0x12}},
		{"read blob", encodeReadBlobReq(0x1234, 22), []byte{0x0C, 0x34, 0x12, 0x16, 0x00}},
		{"write", encodeWriteReq(0x0012, []byte{1, 0}), []byte{0x12, 0x12, 0x00, 0x01, 0x00}},
		{"write empty", encodeWriteReq(0x0012, nil), []byte{0x12, 0x12, 0x00}},
		{"write command", encodeWriteCmd(0x0012, []byte{0xAA}), []byte{0x52, 0x12, 0x00, 0xAA}},
		{"error", attErrorRsp(0x08, 0x0001, 0x06), []byte{0x01, 0x08, 0x01, 0x00, 0x06}},
	} {
		if !bytes.Equal(c.got, c.want) {
			t.Errorf("%s: got % x want % x", c.name, c.got, c.want)
		}
	}
}

func TestATTResponsesAccepted(t *testing.T) {
	if mtu, err := parseMTURsp([]byte{0x03, 23, 0}); err != nil || mtu != 23 {
		t.Fatalf("minimum mtu: %d %v", mtu, err)
	}
	if mtu, err := parseMTURsp([]byte{0x03, 0xFF, 0xFF}); err != nil || mtu != 0xFFFF {
		t.Fatalf("maximum mtu: %d %v", mtu, err)
	}
	// An empty value is a legal read.
	if v, err := parseReadRsp(attOpReadReq, []byte{0x0B}); err != nil || len(v) != 0 {
		t.Fatalf("empty read: % x %v", v, err)
	}
	if v, err := parseReadRsp(attOpReadBlobReq, []byte{0x0D, 1, 2}); err != nil || !bytes.Equal(v, []byte{1, 2}) {
		t.Fatalf("read blob: % x %v", v, err)
	}
	if err := parseWriteRsp([]byte{0x13}); err != nil {
		t.Fatal(err)
	}
	h, v, ind, ok := parseHandleValue([]byte{0x1D, 0x10, 0x00, 7})
	if !ok || !ind || h != 0x10 || !bytes.Equal(v, []byte{7}) {
		t.Fatalf("indication: %v %v %#x % x", ok, ind, h, v)
	}
	if _, v, ind, ok := parseHandleValue([]byte{0x1B, 0x10, 0x00}); !ok || ind || len(v) != 0 {
		t.Fatal("empty notification")
	}
	// An Error Response naming our request comes back as *ATTError.
	_, err := parseReadRsp(attOpReadReq, []byte{0x01, 0x0A, 0x34, 0x12, 0x02})
	var e *ATTError
	if !errors.As(err, &e) || e.Code != 0x02 || e.Handle != 0x1234 || e.ReqOp != 0x0A {
		t.Fatalf("error response: %v", err)
	}
}

func TestATTResponsesRefused(t *testing.T) {
	bad := func(name string, err error) {
		t.Helper()
		if !errors.Is(err, errATTMalformed) {
			t.Errorf("%s: got %v", name, err)
		}
	}
	_, err := parseMTURsp(nil)
	bad("empty pdu", err)
	_, err = parseMTURsp([]byte{0x03, 22, 0})
	bad("mtu below 23", err)
	_, err = parseMTURsp([]byte{0x03, 23})
	bad("short mtu", err)
	_, err = parseMTURsp([]byte{0x03, 23, 0, 0})
	bad("long mtu", err)
	_, err = parseMTURsp([]byte{0x0B, 23, 0})
	bad("another response's opcode", err)
	_, err = parseMTURsp([]byte{0x01, 0x0A, 0x00, 0x00, 0x06})
	bad("error response for a different request", err)
	_, err = parseMTURsp([]byte{0x01, 0x02, 0x00, 0x00})
	bad("short error response", err)
	bad("write response with a body", parseWriteRsp([]byte{0x13, 0}))

	_, err = parseReadByGroupRsp([]byte{0x11})
	bad("group: no length", err)
	_, err = parseReadByGroupRsp([]byte{0x11, 6})
	bad("group: no entries", err)
	_, err = parseReadByGroupRsp([]byte{0x11, 3, 1, 0, 2})
	bad("group: entry shorter than its handles", err)
	_, err = parseReadByGroupRsp([]byte{0x11, 6, 1, 0, 5, 0, 0, 0x18, 6, 0})
	bad("group: trailing partial entry", err)
	_, err = parseReadByGroupRsp([]byte{0x11, 0, 1, 0})
	bad("group: zero length", err)

	_, err = parseReadByTypeRsp([]byte{0x09, 1, 1})
	bad("type: entry shorter than its handle", err)
	_, err = parseReadByTypeRsp([]byte{0x09, 7, 2, 0, 0x02, 3, 0, 0x00})
	bad("type: partial entry", err)
	_, err = parseReadByTypeRsp([]byte{0x09, 0})
	bad("type: zero length", err)

	_, err = parseFindInfoRsp([]byte{0x05, 3, 1, 0, 0, 0x29})
	bad("find info: unknown format", err)
	_, err = parseFindInfoRsp([]byte{0x05, 1})
	bad("find info: no entries", err)
	_, err = parseFindInfoRsp([]byte{0x05, 2, 1, 0, 0, 0x29})
	bad("find info: 128-bit format with a 16-bit entry", err)

	if _, _, _, ok := parseHandleValue([]byte{0x1B, 0x10}); ok {
		t.Error("notification with no handle")
	}
	if _, _, _, ok := parseHandleValue([]byte{0x0B, 0x10, 0x00}); ok {
		t.Error("a read response is not a notification")
	}
}

// ── A server to discover against ─────────────────────────────────────────────

type attr struct {
	handle uint16
	typ    UUID
	value  []byte
}

// fakeServer answers the three discovery requests from an attribute table,
// packing each response as the spec requires: entries of one length only, and
// no more than fits the MTU. So a table of any size takes several round trips.
type fakeServer struct {
	attrs    []attr
	mtu      int
	lastEnd  uint16 // group end handle reported for the last service
	requests int
	notLong  bool // answer Read Blob on a short value with Attribute Not Long
}

func (s *fakeServer) add(typ UUID, value []byte) uint16 {
	h := uint16(len(s.attrs) + 1)
	s.attrs = append(s.attrs, attr{h, typ, value})
	return h
}

func (s *fakeServer) service(u UUID) { s.add(UUID16(uuidPrimaryService), u) }

func (s *fakeServer) characteristic(props byte, u UUID, descriptors ...UUID) {
	decl := uint16(len(s.attrs) + 1)
	v := []byte{props, byte(decl + 1), byte((decl + 1) >> 8)}
	s.add(UUID16(uuidCharacteristic), append(v, u...))
	s.add(u, nil)
	for _, d := range descriptors {
		s.add(d, nil)
	}
}

func (s *fakeServer) groupEnd(i int) uint16 {
	for j := i + 1; j < len(s.attrs); j++ {
		if s.attrs[j].typ.Is16(uuidPrimaryService) {
			return s.attrs[j].handle - 1
		}
	}
	if s.lastEnd != 0 {
		return s.lastEnd
	}
	return s.attrs[len(s.attrs)-1].handle
}

// value answers the read and write requests, on any attribute in the table.
func (s *fakeServer) value(req []byte) ([]byte, bool) {
	op := req[0]
	if op != attOpReadReq && op != attOpReadBlobReq && op != attOpWriteReq && op != attOpWriteCmd {
		return nil, false
	}
	if len(req) < 3 {
		return attErrorRsp(op, 0, 0x04), true // Invalid PDU
	}
	h := binary.LittleEndian.Uint16(req[1:3])
	if h == 0 || int(h) > len(s.attrs) {
		return attErrorRsp(op, h, 0x01), true // Invalid Handle
	}
	a := &s.attrs[h-1]
	switch op {
	case attOpWriteReq, attOpWriteCmd:
		a.value = append([]byte(nil), req[3:]...)
		if op == attOpWriteCmd {
			return nil, true
		}
		return []byte{attOpWriteRsp}, true
	case attOpReadBlobReq:
		off := int(binary.LittleEndian.Uint16(req[3:5]))
		if s.notLong && len(a.value) <= s.mtu-1 {
			return attErrorRsp(op, h, 0x0B), true // Attribute Not Long
		}
		if off > len(a.value) {
			return attErrorRsp(op, h, 0x07), true // Invalid Offset
		}
		v := a.value[off:]
		if len(v) > s.mtu-1 {
			v = v[:s.mtu-1]
		}
		return append([]byte{attOpReadBlobRsp}, v...), true
	}
	v := a.value
	if len(v) > s.mtu-1 {
		v = v[:s.mtu-1]
	}
	return append([]byte{attOpReadRsp}, v...), true
}

func (s *fakeServer) Request(req []byte) ([]byte, error) {
	s.requests++
	if rsp, ok := s.value(req); ok {
		return rsp, nil
	}
	if len(req) < 5 {
		return attErrorRsp(req[0], 0, attErrReqNotSupport), nil
	}
	start := binary.LittleEndian.Uint16(req[1:3])
	end := binary.LittleEndian.Uint16(req[3:5])
	typ := UUID(req[5:])
	var rsp []byte
	size := 0
	for i, a := range s.attrs {
		if a.handle < start || a.handle > end {
			continue
		}
		var entry []byte
		switch req[0] {
		case attOpReadByGroupReq:
			if !bytes.Equal(a.typ, typ) {
				continue
			}
			ge := s.groupEnd(i)
			entry = append([]byte{byte(a.handle), byte(a.handle >> 8), byte(ge), byte(ge >> 8)}, a.value...)
		case attOpReadByTypeReq:
			if !bytes.Equal(a.typ, typ) {
				continue
			}
			entry = append([]byte{byte(a.handle), byte(a.handle >> 8)}, a.value...)
		case attOpFindInfoReq:
			entry = append([]byte{byte(a.handle), byte(a.handle >> 8)}, a.typ...)
		default:
			return attErrorRsp(req[0], start, attErrReqNotSupport), nil
		}
		if rsp == nil {
			size = len(entry)
			lead := byte(size)
			if req[0] == attOpFindInfoReq {
				lead = attFindInfoFormat16
				if size == 18 {
					lead = attFindInfoFormat128
				}
			}
			rsp = []byte{req[0] + 1, lead}
		}
		if len(entry) != size || len(rsp)+size > s.mtu {
			break
		}
		rsp = append(rsp, entry...)
	}
	if rsp == nil {
		return attErrorRsp(req[0], start, attErrAttrNotFound), nil
	}
	return rsp, nil
}

func TestDiscoverServices(t *testing.T) {
	cccd := UUID16(0x2902)
	userDesc := UUID16(0x2901)
	s := &fakeServer{mtu: attDefaultMTU}
	s.service(UUID16(0x1800)) // 1
	s.characteristic(0x02, UUID16(0x2A00))
	s.characteristic(0x02, UUID16(0x2A01))
	s.service(UUID16(0x1801)) // 6, no characteristics
	s.service(uuid128Wire)    // 7
	s.characteristic(0x10, uuid128Wire, cccd, userDesc)
	s.characteristic(0x0C, UUID16(0x2A19))
	s.characteristic(0x12, UUID16(0x2A1A), cccd)
	s.service(UUID16(0x180A)) // last
	s.characteristic(0x02, UUID16(0x2A29))
	s.characteristic(0x02, UUID16(0x2A24))
	s.characteristic(0x02, UUID16(0x2A25))
	s.characteristic(0x02, UUID16(0x2A26), userDesc)
	s.lastEnd = 0xFFFF // as real servers report the last group

	got, err := DiscoverServices(s)
	if err != nil {
		t.Fatal(err)
	}

	// Rebuild what the table says, independently of the code under test.
	var want []Service
	for i, a := range s.attrs {
		switch {
		case a.typ.Is16(uuidPrimaryService):
			want = append(want, Service{Start: a.handle, End: s.groupEnd(i), UUID: a.value})
		case a.typ.Is16(uuidCharacteristic):
			sv := &want[len(want)-1]
			sv.Characteristics = append(sv.Characteristics, Characteristic{
				Handle: a.handle, ValueHandle: a.handle + 1, Properties: a.value[0], UUID: a.value[3:],
			})
		default:
			sv := &want[len(want)-1]
			c := &sv.Characteristics[len(sv.Characteristics)-1]
			if a.handle != c.ValueHandle {
				c.Descriptors = append(c.Descriptors, Descriptor{Handle: a.handle, UUID: a.typ})
			}
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %+v\nwant %+v", got, want)
	}
	if len(got) != 4 || got[3].End != 0xFFFF || len(got[1].Characteristics) != 0 {
		t.Fatalf("shape: %+v", got)
	}
	// At the minimum MTU the table cannot arrive in one response per
	// procedure: 1 for services, 4 for characteristics, 4 for descriptors.
	if s.requests <= 9 {
		t.Fatalf("only %d requests; the responses were not being continued", s.requests)
	}
}

func TestDiscoverEmptyTable(t *testing.T) {
	got, err := DiscoverServices(&fakeServer{mtu: attDefaultMTU, attrs: []attr{{1, UUID16(0x2A00), nil}}})
	if err != nil || len(got) != 0 {
		t.Fatalf("%v %+v", err, got)
	}
}

// scripted replays fixed responses, for peers that do not follow the spec.
type scripted struct {
	rsps [][]byte
	n    int
}

func (s *scripted) Request([]byte) ([]byte, error) {
	r := s.rsps[s.n%len(s.rsps)]
	s.n++
	return r, nil
}

func TestDiscoverRefusesAPeerThatLoops(t *testing.T) {
	// The same service, forever. Without the ordering check this never ends.
	loop := &scripted{rsps: [][]byte{{0x11, 6, 0x01, 0x00, 0x05, 0x00, 0x00, 0x18}}}
	if _, err := DiscoverServices(loop); !errors.Is(err, errGATTOrder) {
		t.Fatalf("looping services: %v after %d requests", err, loop.n)
	}
	if loop.n > 2 {
		t.Fatalf("took %d requests to notice", loop.n)
	}
	for name, rsps := range map[string][][]byte{
		"a service that ends before it starts": {{0x11, 6, 0x05, 0x00, 0x01, 0x00, 0x00, 0x18}},
		"a service uuid of 3 bytes":            {{0x11, 7, 0x01, 0x00, 0x05, 0x00, 0x00, 0x18, 0x00}},
		"a value handle outside its service": {
			{0x11, 6, 0x01, 0x00, 0x05, 0x00, 0x00, 0x18},
			attErrorRsp(attOpReadByGroupReq, 6, attErrAttrNotFound),
			{0x09, 7, 0x02, 0x00, 0x02, 0x09, 0x00, 0x00, 0x2A},
		},
		"a value handle at its own declaration": {
			{0x11, 6, 0x01, 0x00, 0x05, 0x00, 0x00, 0x18},
			attErrorRsp(attOpReadByGroupReq, 6, attErrAttrNotFound),
			{0x09, 7, 0x02, 0x00, 0x02, 0x02, 0x00, 0x00, 0x2A},
		},
		"a descriptor outside the range asked for": {
			{0x11, 6, 0x01, 0x00, 0x05, 0x00, 0x00, 0x18},
			attErrorRsp(attOpReadByGroupReq, 6, attErrAttrNotFound),
			{0x09, 7, 0x02, 0x00, 0x02, 0x03, 0x00, 0x00, 0x2A},
			attErrorRsp(attOpReadByTypeReq, 3, attErrAttrNotFound),
			{0x05, 1, 0x09, 0x00, 0x02, 0x29},
		},
	} {
		if _, err := DiscoverServices(&scripted{rsps: rsps}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestDiscoverPassesOnAnATTError(t *testing.T) {
	// Insufficient Authentication (0x05) is not "nothing here"; it must reach
	// the caller rather than read as an empty table.
	_, err := DiscoverServices(&scripted{rsps: [][]byte{attErrorRsp(attOpReadByGroupReq, 1, 0x05)}})
	var e *ATTError
	if !errors.As(err, &e) || e.Code != 0x05 {
		t.Fatalf("got %v", err)
	}
}
