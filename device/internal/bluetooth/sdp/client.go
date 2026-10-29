package sdp

// The SDP client: what a speaker offers, read before we open AVDTP to it.

import (
	"errors"
	"fmt"
)

// Requester sends one request PDU on an SDP channel and returns the response
// PDU. The client issues one request at a time.
type Requester interface {
	Request(pdu []byte) ([]byte, error)
}

const (
	// maxClientBytes caps what one search may accumulate across
	// continuations. A speaker's whole database is a few hundred bytes; this
	// only stops a peer from feeding us continuation states for ever.
	maxClientBytes = 64 << 10
	// maxRounds is the same guard counted in requests.
	maxRounds = 256
)

// ErrNotFound is a search that matched no record.
var ErrNotFound = errors.New("sdp: no matching record")

// Client runs searches over one Requester.
type Client struct {
	r   Requester
	tid uint16
}

func NewClient(r Requester) *Client { return &Client{r: r} }

// SearchAttributes runs a ServiceSearchAttributeRequest to completion,
// following continuation states, and returns each matching record with the
// attributes asked for.
func (c *Client) SearchAttributes(pattern []UUID, attrs []AttrRange) ([]Record, error) {
	var all, cont []byte
	for round := 0; ; round++ {
		if round == maxRounds {
			return nil, fmt.Errorf("sdp: no end after %d continuations", maxRounds)
		}
		c.tid++
		req := SearchAttrRequest{Pattern: pattern, MaxBytes: 0xFFFF, Attrs: attrs, Cont: cont}
		raw, err := c.r.Request(req.Marshal(c.tid))
		if err != nil {
			return nil, err
		}
		p, err := ParsePDU(raw)
		if err != nil {
			return nil, err
		}
		if p.TID != c.tid {
			return nil, fmt.Errorf("sdp: response for transaction %d, sent %d", p.TID, c.tid)
		}
		switch p.ID {
		case PDUSearchAttrResponse:
		case PDUErrorResponse:
			code := uint16(0)
			if len(p.Params) >= 2 {
				code = uint16(p.Params[0])<<8 | uint16(p.Params[1])
			}
			return nil, &RemoteError{Code: code}
		default:
			return nil, fmt.Errorf("sdp: unexpected PDU 0x%02x", p.ID)
		}
		rsp, err := ParseAttrResponse(p.Params)
		if err != nil {
			return nil, err
		}
		if len(all)+len(rsp.Lists) > maxClientBytes {
			return nil, fmt.Errorf("sdp: response over %d bytes", maxClientBytes)
		}
		all = append(all, rsp.Lists...)
		if len(rsp.Cont) == 0 {
			break
		}
		cont = rsp.Cont
	}
	return parseLists(all)
}

// parseLists decodes AttributeLists: a sequence of attribute lists, each a
// sequence of uint16 ID, value pairs. Nothing at all is no records.
func parseLists(b []byte) ([]Record, error) {
	if len(b) == 0 {
		return nil, nil
	}
	top, err := DecodeAll(b)
	if err != nil {
		return nil, err
	}
	if top.Type != TypeSeq {
		return nil, errors.New("sdp: attribute lists are not a sequence")
	}
	var out []Record
	for _, l := range top.Items {
		if l.Type != TypeSeq || len(l.Items)%2 != 0 {
			return nil, errors.New("sdp: malformed attribute list")
		}
		var rec Record
		for i := 0; i < len(l.Items); i += 2 {
			id, ok := l.Items[i].AsUint16()
			if !ok {
				return nil, errors.New("sdp: attribute ID is not a uint16")
			}
			rec.Attrs = append(rec.Attrs, Attr{id, l.Items[i+1]})
		}
		if h, ok := rec.Attr(AttrServiceRecordHandle); ok {
			if v, ok := h.AsUint(); ok {
				rec.Handle = uint32(v)
			}
		}
		out = append(out, rec)
	}
	return out, nil
}

// protocolVersion returns the first uint parameter of protocol u in a
// ProtocolDescriptorList (L2CAP's is the PSM, AVDTP's and AVCTP's the
// version). A list may come as an alternative of lists (Vol 3 Part B
// 5.1.5); the first alternative is the one to use.
func protocolParam(list Element, u UUID) (uint16, bool) {
	if list.Type == TypeAlt && len(list.Items) > 0 {
		list = list.Items[0]
	}
	if list.Type != TypeSeq {
		return 0, false
	}
	for _, d := range list.Items {
		if d.Type != TypeSeq || len(d.Items) == 0 {
			continue
		}
		if v, ok := d.Items[0].AsUUID(); !ok || v != u {
			continue
		}
		if len(d.Items) > 1 {
			if p, ok := d.Items[1].AsUint16(); ok {
				return p, true
			}
		}
		return 0, true
	}
	return 0, false
}

// profileVersion reads profile u's version from a ProfileDescriptorList.
func profileVersion(list Element, u UUID) (uint16, bool) {
	for _, d := range list.Items {
		if d.Type != TypeSeq || len(d.Items) < 2 {
			continue
		}
		if v, ok := d.Items[0].AsUUID(); ok && v == u {
			return d.Items[1].AsUint16()
		}
	}
	return 0, false
}

func classes(r Record) []UUID {
	e, _ := r.Attr(AttrServiceClassIDList)
	var out []UUID
	for _, it := range e.Items {
		if u, ok := it.AsUUID(); ok {
			out = append(out, u)
		}
	}
	return out
}

func hasClass(r Record, v uint16) bool {
	for _, u := range classes(r) {
		if u == UUIDFrom16(v) {
			return true
		}
	}
	return false
}

func features(r Record) uint16 {
	e, _ := r.Attr(AttrSupportedFeatures)
	v, _ := e.AsUint16()
	return v
}

// SinkInfo is what we need from a speaker's A2DP sink record.
type SinkInfo struct {
	AVDTPPSM     uint16
	AVDTPVersion uint16
	A2DPVersion  uint16
	// Features is A2DP SupportedFeatures for a sink: bit 0 headphone,
	// 1 speaker, 2 recorder, 3 amplifier. 0 when absent (it is optional).
	Features uint16
}

var discoveryAttrs = []AttrRange{
	{AttrServiceRecordHandle, AttrServiceClassIDList},
	{AttrProtocolDescriptorList, AttrProtocolDescriptorList},
	{AttrProfileDescriptorList, AttrProfileDescriptorList},
	{AttrSupportedFeatures, AttrSupportedFeatures},
}

// FindA2DPSink reads the remote's Audio Sink record. A record that names
// AVDTP without a PSM gets 0x0019, the only PSM AVDTP is assigned.
func (c *Client) FindA2DPSink() (SinkInfo, error) {
	recs, err := c.SearchAttributes([]UUID{UUIDFrom16(UUIDAudioSink)}, discoveryAttrs)
	if err != nil {
		return SinkInfo{}, err
	}
	for _, r := range recs {
		if !hasClass(r, UUIDAudioSink) {
			continue
		}
		pdl, _ := r.Attr(AttrProtocolDescriptorList)
		info := SinkInfo{AVDTPPSM: PSMAVDTP, Features: features(r)}
		if psm, ok := protocolParam(pdl, UUIDFrom16(UUIDL2CAP)); ok && psm != 0 {
			info.AVDTPPSM = psm
		}
		info.AVDTPVersion, _ = protocolParam(pdl, UUIDFrom16(UUIDAVDTP))
		prof, _ := r.Attr(AttrProfileDescriptorList)
		info.A2DPVersion, _ = profileVersion(prof, UUIDFrom16(UUIDAdvancedAudio))
		return info, nil
	}
	return SinkInfo{}, ErrNotFound
}

// AVRCPInfo is one of a speaker's AVRCP records.
type AVRCPInfo struct {
	Target       bool // 0x110C in the class list
	Controller   bool // 0x110E or 0x110F in the class list
	AVCTPPSM     uint16
	AVCTPVersion uint16
	Version      uint16 // AVRCP profile version, e.g. 0x0106
	Features     uint16
}

// Category reports AVRCP category n (1-4) from SupportedFeatures.
func (a AVRCPInfo) Category(n int) bool {
	return n >= 1 && n <= 4 && a.Features&(1<<(n-1)) != 0
}

// AbsoluteVolume is whether the record's version can carry
// SetAbsoluteVolume and the volume-changed notification: AVRCP 1.4 and later.
// Whether a sink's target honours it is only known by asking it.
func (a AVRCPInfo) AbsoluteVolume() bool { return a.Version >= 0x0104 }

// FindAVRCP reads every AVRCP record the remote has, target and controller
// alike, by searching for AVCTP, which all of them carry.
func (c *Client) FindAVRCP() ([]AVRCPInfo, error) {
	recs, err := c.SearchAttributes([]UUID{UUIDFrom16(UUIDAVCTP)}, discoveryAttrs)
	if err != nil {
		return nil, err
	}
	var out []AVRCPInfo
	for _, r := range recs {
		info := AVRCPInfo{
			Target:     hasClass(r, UUIDAVRCPTarget),
			Controller: hasClass(r, UUIDAVRCP) || hasClass(r, UUIDAVRCPController),
			AVCTPPSM:   PSMAVCTP,
			Features:   features(r),
		}
		if !info.Target && !info.Controller {
			continue
		}
		pdl, _ := r.Attr(AttrProtocolDescriptorList)
		if psm, ok := protocolParam(pdl, UUIDFrom16(UUIDL2CAP)); ok && psm != 0 {
			info.AVCTPPSM = psm
		}
		info.AVCTPVersion, _ = protocolParam(pdl, UUIDFrom16(UUIDAVCTP))
		prof, _ := r.Attr(AttrProfileDescriptorList)
		info.Version, _ = profileVersion(prof, UUIDFrom16(UUIDAVRCP))
		out = append(out, info)
	}
	if len(out) == 0 {
		return nil, ErrNotFound
	}
	return out, nil
}
