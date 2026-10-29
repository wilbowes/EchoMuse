package avdtp

// Service capabilities (AVDTP 1.3 §8.21): a list of [category][LOSC][info]
// elements, used by GET_(ALL_)CAPABILITIES, SET_CONFIGURATION,
// GET_CONFIGURATION and RECONFIGURE.

import "fmt"

// Category is a service capability category (§8.21.1).
type Category uint8

const (
	CatMediaTransport    Category = 0x01
	CatReporting         Category = 0x02
	CatRecovery          Category = 0x03
	CatContentProtection Category = 0x04
	CatHeaderCompression Category = 0x05
	CatMultiplexing      Category = 0x06
	CatMediaCodec        Category = 0x07
	CatDelayReporting    Category = 0x08
)

// CodecType is a media codec type (A2DP 1.3 §4.2 / Assigned Numbers).
type CodecType uint8

const (
	CodecSBC    CodecType = 0x00
	CodecMPEG12 CodecType = 0x01
	CodecAAC    CodecType = 0x02
	CodecATRAC  CodecType = 0x04
	CodecVendor CodecType = 0xFF
)

// Codec is the media codec capability; Info is codec-specific and opaque.
type Codec struct {
	Media MediaType
	Type  CodecType
	Info  []byte
}

// Recovery is the RFC 2733 recovery capability (§8.21.4).
type Recovery struct{ Type, MRWS, MNMP uint8 }

// ContentProtection is one content protection capability (§8.21.5).
type ContentProtection struct {
	Type  uint16
	Value []byte
}

// RawCapability is a category this package does not define.
type RawCapability struct {
	Category Category
	Info     []byte
}

// Capabilities is a decoded capability list. A nil pointer or false is an
// absent category.
type Capabilities struct {
	MediaTransport    bool
	Reporting         bool
	Recovery          *Recovery
	ContentProtection []ContentProtection
	HeaderCompression *uint8 // the B7..B5 flags byte
	Multiplexing      []byte // nil when absent; at least one byte when present
	Codec             *Codec
	DelayReporting    bool
	Unknown           []RawCapability
}

// CapError is a capability list that fails §8.21's format rules; Category and
// Code are what a SET_CONFIGURATION reject carries back.
type CapError struct {
	Category Category
	Code     ErrorCode
}

func (e *CapError) Error() string {
	return fmt.Sprintf("avdtp: capability 0x%02x: error %v", uint8(e.Category), e.Code)
}

// Encode writes the list in category order.
func (c Capabilities) Encode() []byte {
	var b []byte
	put := func(cat Category, info []byte) {
		if len(info) > 255 {
			info = info[:255] // LOSC is one byte; nothing real comes near
		}
		b = append(b, byte(cat), byte(len(info)))
		b = append(b, info...)
	}
	if c.MediaTransport {
		put(CatMediaTransport, nil)
	}
	if c.Reporting {
		put(CatReporting, nil)
	}
	if r := c.Recovery; r != nil {
		put(CatRecovery, []byte{r.Type, r.MRWS, r.MNMP})
	}
	for _, cp := range c.ContentProtection {
		put(CatContentProtection, append([]byte{byte(cp.Type), byte(cp.Type >> 8)}, cp.Value...))
	}
	if c.HeaderCompression != nil {
		put(CatHeaderCompression, []byte{*c.HeaderCompression})
	}
	if c.Multiplexing != nil {
		put(CatMultiplexing, c.Multiplexing)
	}
	if c.Codec != nil {
		put(CatMediaCodec, append([]byte{byte(c.Codec.Media) << 4, byte(c.Codec.Type)}, c.Codec.Info...))
	}
	if c.DelayReporting {
		put(CatDelayReporting, nil)
	}
	for _, u := range c.Unknown {
		put(u.Category, u.Info)
	}
	return b
}

// ParseCapabilities decodes a list strictly. Undefined categories are kept in
// Unknown rather than refused: a GET_CAPABILITIES answer may carry categories
// newer than us, and only a SET_CONFIGURATION has to refuse them.
func ParseCapabilities(b []byte) (Capabilities, error) {
	var c Capabilities
	seen := map[Category]bool{}
	for len(b) > 0 {
		if len(b) < 2 {
			return c, &CapError{0, CodeBadPayloadFormat}
		}
		cat, losc := Category(b[0]), int(b[1])
		if 2+losc > len(b) {
			return c, &CapError{cat, CodeBadPayloadFormat}
		}
		info := clone(b[2 : 2+losc])
		b = b[2+losc:]
		if seen[cat] && cat != CatContentProtection {
			return c, &CapError{cat, CodeBadPayloadFormat}
		}
		seen[cat] = true
		if err := c.add(cat, info); err != nil {
			return c, err
		}
	}
	return c, nil
}

func (c *Capabilities) add(cat Category, info []byte) error {
	bad := func(code ErrorCode) error { return &CapError{cat, code} }
	switch cat {
	case CatMediaTransport:
		if len(info) != 0 {
			return bad(CodeBadMediaTransportFormat)
		}
		c.MediaTransport = true
	case CatReporting:
		if len(info) != 0 {
			return bad(CodeBadPayloadFormat)
		}
		c.Reporting = true
	case CatRecovery:
		if len(info) != 3 {
			return bad(CodeBadRecoveryFormat)
		}
		if info[0] != 0x01 { // RFC 2733 is the only type defined
			return bad(CodeBadRecoveryType)
		}
		for _, v := range info[1:] { // MRWS and MNMP: 0x01-0x18
			if v < 0x01 || v > 0x18 {
				return bad(CodeBadRecoveryFormat)
			}
		}
		c.Recovery = &Recovery{info[0], info[1], info[2]}
	case CatContentProtection:
		if len(info) < 2 {
			return bad(CodeBadCPFormat)
		}
		c.ContentProtection = append(c.ContentProtection,
			ContentProtection{uint16(info[0]) | uint16(info[1])<<8, info[2:]})
	case CatHeaderCompression:
		if len(info) != 1 {
			return bad(CodeBadROHCFormat)
		}
		v := info[0]
		c.HeaderCompression = &v
	case CatMultiplexing:
		if len(info) < 1 {
			return bad(CodeBadMultiplexingFormat)
		}
		c.Multiplexing = info
	case CatMediaCodec:
		if len(info) < 2 {
			return bad(CodeBadPayloadFormat)
		}
		c.Codec = &Codec{MediaType(info[0] >> 4), CodecType(info[1]), info[2:]}
	case CatDelayReporting:
		if len(info) != 0 {
			return bad(CodeBadPayloadFormat)
		}
		c.DelayReporting = true
	default:
		c.Unknown = append(c.Unknown, RawCapability{cat, info})
	}
	return nil
}

// encodeSEPs and parseSEPs are DISCOVER's response body (§8.6.2):
// [ACP SEID:6 InUse:1 rfa:1] [Media:4 TSEP:1 rfa:3] per endpoint.
func encodeSEPs(seps []SEPInfo) []byte {
	b := make([]byte, 0, 2*len(seps))
	for _, s := range seps {
		inUse := byte(0)
		if s.InUse {
			inUse = 0x02
		}
		b = append(b, s.SEID<<2|inUse, byte(s.Media)<<4|byte(s.Type&1)<<3)
	}
	return b
}

func parseSEPs(b []byte) ([]SEPInfo, error) {
	if len(b) == 0 || len(b)%2 != 0 {
		return nil, fmt.Errorf("avdtp: discover response of %d bytes", len(b))
	}
	out := make([]SEPInfo, 0, len(b)/2)
	for i := 0; i < len(b); i += 2 {
		s := SEPInfo{b[i] >> 2, b[i]&0x02 != 0, MediaType(b[i+1] >> 4), SEPType(b[i+1] >> 3 & 1)}
		if !validSEID(s.SEID) {
			return nil, fmt.Errorf("avdtp: discover response names SEID 0x%02x", s.SEID)
		}
		out = append(out, s)
	}
	return out, nil
}
