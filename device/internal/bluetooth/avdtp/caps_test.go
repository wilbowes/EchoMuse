package avdtp

import (
	"bytes"
	"errors"
	"reflect"
	"testing"
)

// A typical A2DP SET_CONFIGURATION body after the SEIDs: media transport,
// then SBC 44.1kHz joint stereo, 16 blocks, 8 subbands, loudness, bitpool
// 2-53.
var sbcConfigBytes = []byte{0x01, 0x00, 0x07, 0x06, 0x00, 0x00, 0x21, 0x15, 0x02, 0x35}

func TestCapabilitiesKnownBytes(t *testing.T) {
	c, err := ParseCapabilities(sbcConfigBytes)
	if err != nil {
		t.Fatal(err)
	}
	want := Capabilities{MediaTransport: true, Codec: &Codec{MediaAudio, CodecSBC, []byte{0x21, 0x15, 0x02, 0x35}}}
	if !reflect.DeepEqual(c, want) {
		t.Fatalf("got %+v", c)
	}
	if got := c.Encode(); !bytes.Equal(got, sbcConfigBytes) {
		t.Fatalf("encoded % x", got)
	}
}

func TestEveryCategoryRoundTrips(t *testing.T) {
	hc := uint8(0xE0)
	in := Capabilities{
		MediaTransport:    true,
		Reporting:         true,
		Recovery:          &Recovery{1, 0x18, 0x01},
		ContentProtection: []ContentProtection{{0x0002, nil}, {0x0001, []byte{9}}},
		HeaderCompression: &hc,
		Multiplexing:      []byte{0x80, 0x10},
		Codec:             &Codec{MediaAudio, CodecAAC, []byte{1, 2, 3, 4, 5, 6}},
		DelayReporting:    true,
		Unknown:           []RawCapability{{0x09, []byte{7}}},
	}
	b := in.Encode()
	// SCMS-T is 0x0002, little-endian on the wire (§8.21.5).
	if !bytes.Contains(b, []byte{0x04, 0x02, 0x02, 0x00}) {
		t.Fatalf("content protection type not little-endian: % x", b)
	}
	out, err := ParseCapabilities(b)
	if err != nil {
		t.Fatal(err)
	}
	out.ContentProtection[0].Value = nil // parsed as empty, encoded from nil
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("\n in %+v\nout %+v", in, out)
	}
}

func TestCapabilityFormatErrors(t *testing.T) {
	for name, tc := range map[string]struct {
		b    []byte
		cat  Category
		code ErrorCode
	}{
		"truncated header":    {[]byte{0x01}, 0, CodeBadPayloadFormat},
		"LOSC past the end":   {[]byte{0x07, 0x06, 0x00, 0x00}, CatMediaCodec, CodeBadPayloadFormat},
		"transport with info": {[]byte{0x01, 0x01, 0x00}, CatMediaTransport, CodeBadMediaTransportFormat},
		"reporting with info": {[]byte{0x02, 0x01, 0x00}, CatReporting, CodeBadPayloadFormat},
		"recovery short":      {[]byte{0x03, 0x02, 0x01, 0x01}, CatRecovery, CodeBadRecoveryFormat},
		"recovery type 2":     {[]byte{0x03, 0x03, 0x02, 0x01, 0x01}, CatRecovery, CodeBadRecoveryType},
		"recovery MRWS 0":     {[]byte{0x03, 0x03, 0x01, 0x00, 0x01}, CatRecovery, CodeBadRecoveryFormat},
		"recovery MNMP 0x19":  {[]byte{0x03, 0x03, 0x01, 0x01, 0x19}, CatRecovery, CodeBadRecoveryFormat},
		"CP one byte":         {[]byte{0x04, 0x01, 0x02}, CatContentProtection, CodeBadCPFormat},
		"ROHC two bytes":      {[]byte{0x05, 0x02, 0x00, 0x00}, CatHeaderCompression, CodeBadROHCFormat},
		"ROHC empty":          {[]byte{0x05, 0x00}, CatHeaderCompression, CodeBadROHCFormat},
		"multiplexing empty":  {[]byte{0x06, 0x00}, CatMultiplexing, CodeBadMultiplexingFormat},
		"codec one byte":      {[]byte{0x07, 0x01, 0x00}, CatMediaCodec, CodeBadPayloadFormat},
		"delay with info":     {[]byte{0x08, 0x01, 0x00}, CatDelayReporting, CodeBadPayloadFormat},
		"transport twice":     {[]byte{0x01, 0x00, 0x01, 0x00}, CatMediaTransport, CodeBadPayloadFormat},
	} {
		_, err := ParseCapabilities(tc.b)
		var ce *CapError
		if !errors.As(err, &ce) || ce.Category != tc.cat || ce.Code != tc.code {
			t.Errorf("%s: got %v, want category 0x%02x code %v", name, err, tc.cat, tc.code)
		}
	}
}

func TestParseCopiesItsInput(t *testing.T) {
	b := append([]byte(nil), sbcConfigBytes...)
	c, _ := ParseCapabilities(b)
	b[6] = 0xFF
	if c.Codec.Info[0] != 0x21 {
		t.Fatal("codec info aliases the packet")
	}
}

func TestSEPListBytes(t *testing.T) {
	seps := []SEPInfo{{1, false, MediaAudio, SEPSink}, {0x3E, true, MediaVideo, SEPSource}}
	b := encodeSEPs(seps)
	if !bytes.Equal(b, []byte{0x04, 0x08, 0xFA, 0x10}) {
		t.Fatalf("got % x", b)
	}
	got, err := parseSEPs(b)
	if err != nil || !reflect.DeepEqual(got, seps) {
		t.Fatalf("got %+v %v", got, err)
	}
	for name, bad := range map[string][]byte{
		"empty": {}, "odd": {0x04}, "SEID 0": {0x00, 0x08}, "SEID 0x3F": {0xFC, 0x08},
	} {
		if _, err := parseSEPs(bad); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// Whatever parses must encode back to something that parses to the same.
func FuzzParseCapabilities(f *testing.F) {
	f.Add(sbcConfigBytes)
	f.Add([]byte{0x03, 0x03, 0x01, 0x18, 0x18, 0x04, 0x02, 0x02, 0x00, 0x08, 0x00, 0x2A, 0x01, 0xFF})
	f.Fuzz(func(t *testing.T, b []byte) {
		c, err := ParseCapabilities(b)
		if err != nil {
			return
		}
		again, err := ParseCapabilities(c.Encode())
		if err != nil {
			t.Fatalf("re-encoded list does not parse: %v", err)
		}
		if !reflect.DeepEqual(norm(c), norm(again)) {
			t.Fatalf("\n%+v\n%+v", c, again)
		}
	})
}

// norm makes empty and nil byte slices compare equal.
func norm(c Capabilities) Capabilities {
	for i := range c.ContentProtection {
		if len(c.ContentProtection[i].Value) == 0 {
			c.ContentProtection[i].Value = nil
		}
	}
	for i := range c.Unknown {
		if len(c.Unknown[i].Info) == 0 {
			c.Unknown[i].Info = nil
		}
	}
	if c.Codec != nil && len(c.Codec.Info) == 0 {
		cc := *c.Codec
		cc.Info = nil
		c.Codec = &cc
	}
	return c
}
