package sdp

import (
	"bytes"
	"strings"
	"testing"
)

// BlueZ unit/test-sdp.c "/sdp/DE/ATTR/*": one vector per type and width.
func TestDecodeBlueZVectors(t *testing.T) {
	ones := func(n int) []byte { return bytes.Repeat([]byte{0xff}, n) }
	min := func(n int) []byte { b := make([]byte, n); b[0] = 0x80; return b }
	cases := []struct {
		name string
		in   []byte
		typ  Type
		raw  []byte
	}{
		{"TEXT_STR8/empty", []byte{0x25, 0x00}, TypeText, []byte{}},
		{"TEXT_STR8", []byte{0x25, 0x04, 'A', 'B', 'C', 'D'}, TypeText, []byte("ABCD")},
		{"TEXT_STR16", []byte{0x26, 0x00, 0x04, 'A', 'B', 'C', 'D'}, TypeText, []byte("ABCD")},
		{"URL_STR8", append([]byte{0x45, 0x15}, "http://www.bluez.org/"...), TypeURL, []byte("http://www.bluez.org/")},
		{"URL_STR16", append([]byte{0x46, 0x00, 0x15}, "http://www.bluez.org/"...), TypeURL, []byte("http://www.bluez.org/")},
		{"NIL", []byte{0x00}, TypeNil, nil},
		{"UINT8", []byte{0x08, 0xff}, TypeUint, ones(1)},
		{"INT8", []byte{0x10, 0x80}, TypeInt, min(1)},
		{"UINT16", []byte{0x09, 0xff, 0xff}, TypeUint, ones(2)},
		{"INT16", []byte{0x11, 0x80, 0x00}, TypeInt, min(2)},
		{"UINT32", append([]byte{0x0A}, ones(4)...), TypeUint, ones(4)},
		{"INT32", append([]byte{0x12}, min(4)...), TypeInt, min(4)},
		{"UINT64", append([]byte{0x0B}, ones(8)...), TypeUint, ones(8)},
		{"INT64", append([]byte{0x13}, min(8)...), TypeInt, min(8)},
		{"UINT128", append([]byte{0x0C}, ones(16)...), TypeUint, ones(16)},
		{"INT128", append([]byte{0x14}, min(16)...), TypeInt, min(16)},
	}
	for _, c := range cases {
		e, err := DecodeAll(c.in)
		if err != nil {
			t.Errorf("%s: %v", c.name, err)
			continue
		}
		if e.Type != c.typ || !bytes.Equal(e.Raw, c.raw) && len(c.raw)+len(e.Raw) != 0 {
			t.Errorf("%s: got type %d raw %x", c.name, e.Type, e.Raw)
		}
		// The STR16 vectors use a longer prefix than their length needs; the
		// rest are in the shortest form and must re-encode exactly.
		if strings.HasSuffix(c.name, "STR16") {
			continue
		}
		if out, err := e.Marshal(); err != nil || !bytes.Equal(out, c.in) {
			t.Errorf("%s: re-encoded %x, want %x (%v)", c.name, out, c.in, err)
		}
	}
	b, err := DecodeAll([]byte{0x28, 0x01})
	if err != nil || b.Type != TypeBool || !b.Bool {
		t.Errorf("BOOL: %+v %v", b, err)
	}
}

func TestIntegerValues(t *testing.T) {
	if v, ok := Int8(-128).AsInt(); !ok || v != -128 {
		t.Errorf("int8: %d", v)
	}
	if v, ok := Int64(-2).AsInt(); !ok || v != -2 {
		t.Errorf("int64: %d", v)
	}
	if v, ok := Uint64(1<<63 + 5).AsUint(); !ok || v != 1<<63+5 {
		t.Errorf("uint64: %d", v)
	}
	big := Element{Type: TypeUint, Raw: make([]byte, 16)}
	if _, ok := big.AsUint(); ok {
		t.Error("a 128-bit integer has no uint64 value")
	}
	if _, ok := Uint32(7).AsUint16(); ok {
		t.Error("AsUint16 must refuse a uint32: the field's width is part of its type")
	}
}

// Vol 3 Part B 3.3, Table 3.2: every length-prefix width decodes, and the
// encoder picks the shortest that fits.
func TestVariableLengthPrefixes(t *testing.T) {
	for _, n := range []int{0, 1, 255, 256, 65535, 65536, 70000} {
		s := strings.Repeat("x", n)
		enc, err := Text(s).Marshal()
		if err != nil {
			t.Fatal(err)
		}
		want := byte(5)
		switch {
		case n > 0xFFFF:
			want = 7
		case n > 0xFF:
			want = 6
		}
		if enc[0] != byte(TypeText)<<3|want {
			t.Errorf("len %d: header %02x, want size index %d", n, enc[0], want)
		}
		e, err := DecodeAll(enc)
		if got, _ := e.AsText(); err != nil || got != s {
			t.Errorf("len %d: round trip failed: %v", n, err)
		}
	}
	// A long form for a short body is legal and must decode.
	for _, in := range [][]byte{
		{0x26, 0x00, 0x01, 'a'},
		{0x27, 0x00, 0x00, 0x00, 0x01, 'a'},
		{0x37, 0x00, 0x00, 0x00, 0x02, 0x08, 0x01},
	} {
		if _, err := DecodeAll(in); err != nil {
			t.Errorf("%x: %v", in, err)
		}
	}
}

func TestUUIDWidths(t *testing.T) {
	want := UUIDFrom16(0x110B)
	for _, in := range [][]byte{
		{0x19, 0x11, 0x0B},
		{0x1A, 0x00, 0x00, 0x11, 0x0B},
		{0x1C, 0x00, 0x00, 0x11, 0x0B, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB},
	} {
		e, err := DecodeAll(in)
		if u, ok := e.AsUUID(); err != nil || !ok || u != want {
			t.Errorf("%x: %v %v", in, u, err)
		}
	}
	if enc, _ := UUIDElement(want).Marshal(); !bytes.Equal(enc, []byte{0x19, 0x11, 0x0B}) {
		t.Errorf("shortest form of 0x110B: %x", enc)
	}
	if enc, _ := UUIDElement(UUIDFrom32(0x12345678)).Marshal(); len(enc) != 5 {
		t.Errorf("a 32-bit alias encodes in 4 bytes: %x", enc)
	}
	custom := UUID{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
	if _, ok := custom.Short(); ok {
		t.Error("a UUID outside the Base UUID has no short form")
	}
	if enc, _ := UUIDElement(custom).Marshal(); len(enc) != 17 {
		t.Errorf("custom UUID: %x", enc)
	}
}

// Forbidden forms from Table 3.2, and truncation at every point.
func TestDecodeRejects(t *testing.T) {
	bad := map[string][]byte{
		"empty":                {},
		"nil with size 1":      {0x01},
		"bool with size 1":     {0x29, 0x00, 0x01},
		"uint size index 5":    {0x0D, 0x01, 0x00},
		"uuid of 1 byte":       {0x18, 0x01},
		"uuid of 8 bytes":      {0x1B, 0, 0, 0, 0, 0, 0, 0, 0},
		"text with fixed size": {0x20, 'a'},
		"seq with fixed size":  {0x31, 0x08, 0x01},
		"reserved type 9":      {0x48, 0x00},
		"reserved type 31":     {0xF8},
		"short uint16":         {0x09, 0x01},
		"short length 16":      {0x26, 0x00},
		"length past end":      {0x25, 0x05, 'a'},
		"length32 past end":    {0x27, 0xFF, 0xFF, 0xFF, 0xFF},
		"bad child":            {0x35, 0x02, 0x09, 0x01},
		"child past parent":    {0x35, 0x01, 0x09, 0x00, 0x01},
	}
	for name, in := range bad {
		if _, err := DecodeAll(in); err == nil {
			t.Errorf("%s (%x) decoded", name, in)
		}
	}
	if _, err := DecodeAll([]byte{0x08, 0x01, 0x08}); err == nil {
		t.Error("trailing bytes after one element must be refused by DecodeAll")
	}
}

func TestNestingIsBounded(t *testing.T) {
	// maxDepth sequences nest fine; one more does not.
	in := []byte{0x00}
	for i := 0; i < maxDepth; i++ {
		in = append([]byte{0x35, byte(len(in))}, in...)
	}
	if _, err := DecodeAll(in); err != nil {
		t.Fatalf("depth %d: %v", maxDepth, err)
	}
	deeper := append([]byte{0x35, byte(len(in))}, in...)
	if _, err := DecodeAll(deeper); err == nil {
		t.Errorf("depth %d decoded", maxDepth+1)
	}
}

func TestEncodeRejectsBadWidths(t *testing.T) {
	for _, e := range []Element{
		{Type: TypeUint, Raw: make([]byte, 3)},
		{Type: TypeUUID, Raw: make([]byte, 1)},
		{Type: TypeUUID, Raw: make([]byte, 8)},
		{Type: 9},
		Seq(Element{Type: TypeInt}),
	} {
		if _, err := e.Marshal(); err == nil {
			t.Errorf("%+v encoded", e)
		}
	}
}

func FuzzDecode(f *testing.F) {
	for _, seed := range [][]byte{
		{0x35, 0x03, 0x19, 0x11, 0x0B},
		{0x27, 0xFF, 0xFF, 0xFF, 0xFF},
		{0x36, 0x00, 0x05, 0x09, 0x00, 0x01, 0x08, 0x02},
		{0x3D, 0x02, 0x00, 0x00},
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, in []byte) {
		e, rest, err := Decode(in)
		if err != nil {
			return
		}
		// Whatever decodes re-encodes to something that decodes the same.
		enc, err := e.Marshal()
		if err != nil {
			t.Fatalf("decoded %x but cannot encode it: %v", in, err)
		}
		e2, err := DecodeAll(enc)
		if err != nil {
			t.Fatalf("re-encoding %x does not decode: %v", enc, err)
		}
		if enc2, _ := e2.Marshal(); !bytes.Equal(enc, enc2) {
			t.Fatalf("not stable: %x then %x", enc, enc2)
		}
		if len(rest) > len(in) {
			t.Fatal("rest longer than input")
		}
	})
}
