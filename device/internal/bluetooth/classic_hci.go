package bluetooth

// HCI commands and events for BR/EDR ("Classic"), which a Bluetooth speaker
// needs: A2DP has no LE transport. Core Spec v5.x Vol 4 Part E §7.
//
// Only what an audio source uses is here: discovery, connecting, pairing
// (Secure Simple Pairing, and legacy PIN for speakers that predate it), and
// encryption. Builders return parameter bytes for hciHost.Cmd; parsers take
// an H4 event packet (type, code, length, params).

import (
	"encoding/binary"
	"fmt"
	"net"
	"strings"
)

const (
	// Link Control (OGF 0x01).
	opInquiry              = 0x01<<10 | 0x0001
	opInquiryCancel        = 0x01<<10 | 0x0002
	opCreateConn           = 0x01<<10 | 0x0005
	opAcceptConnReq        = 0x01<<10 | 0x0009
	opRejectConnReq        = 0x01<<10 | 0x000A
	opLinkKeyReqReply      = 0x01<<10 | 0x000B
	opLinkKeyReqNegReply   = 0x01<<10 | 0x000C
	opPinCodeReqReply      = 0x01<<10 | 0x000D
	opPinCodeReqNegReply   = 0x01<<10 | 0x000E
	opAuthRequested        = 0x01<<10 | 0x0011
	opSetConnEncryption    = 0x01<<10 | 0x0013
	opRemoteNameReq        = 0x01<<10 | 0x0019
	opIOCapReqReply        = 0x01<<10 | 0x002B
	opUserConfirmReqReply  = 0x01<<10 | 0x002C
	opUserConfirmNegReply  = 0x01<<10 | 0x002D
	opIOCapReqNegReply     = 0x01<<10 | 0x0034
	opWriteDefLinkPolicy   = 0x02<<10 | 0x000F
	opSetEventMask         = 0x03<<10 | 0x0001
	opWriteLocalName       = 0x03<<10 | 0x0013
	opWritePageTimeout     = 0x03<<10 | 0x0018
	opWriteScanEnable      = 0x03<<10 | 0x001A
	opWriteClassOfDevice   = 0x03<<10 | 0x0024
	opWriteInquiryMode     = 0x03<<10 | 0x0045
	opWriteSimplePairing   = 0x03<<10 | 0x0056
	opWriteLEHostSupported = 0x03<<10 | 0x006D

	evtInquiryComplete     = 0x01
	evtInquiryResult       = 0x02
	evtConnComplete        = 0x03
	evtConnRequest         = 0x04
	evtAuthComplete        = 0x06
	evtRemoteNameComplete  = 0x07
	evtEncryptionChange    = 0x08
	evtRoleChange          = 0x12
	evtModeChange          = 0x14
	evtPinCodeRequest      = 0x16
	evtLinkKeyRequest      = 0x17
	evtLinkKeyNotification = 0x18
	evtInquiryResultRSSI   = 0x22
	evtExtInquiryResult    = 0x2F
	evtIOCapRequest        = 0x31
	evtIOCapResponse       = 0x32
	evtUserConfirmRequest  = 0x33
	evtSimplePairingDone   = 0x36

	// Class of Device for the inquiry filter (Assigned Numbers, Baseband):
	// major class Audio/Video, and the Rendering/Audio service bits a speaker
	// sets.
	codMajorMask     = 0x1F00
	codMajorAudio    = 0x0400
	codServiceRender = 1 << 18
	codServiceAudio  = 1 << 21

	// Ours: Audio/Video, minor "Hi-Fi Audio Device", Audio service.
	ownClassOfDevice = codServiceAudio | codMajorAudio | 0x0A<<2

	// The General/Unlimited Inquiry Access Code, LAP 0x9E8B33.
	giacLAP = 0x9E8B33

	ioCapNoInputNoOutput = 0x03
	// General Bonding, no MITM: the only one Just Works can satisfy, and the
	// one that makes the controller hand us a key to keep (Vol 4 Part E
	// §7.1.29).
	authReqGeneralBonding = 0x04

	linkTypeACL = 0x01

	scanPage = 0x02 // page scan on, inquiry scan off: connectable, not discoverable
)

// eventMaskClassic enables what Classic needs on top of LE (bit n = event
// code n+1, Vol 4 Part E §7.3.1). The default after Reset stops at event 0x2C,
// which leaves out every Secure Simple Pairing event; without them the
// controller pairs nothing and says nothing. Same value BlueZ sets for a dual
// mode controller.
const eventMaskClassic = 0x3DBFF807FFFBFFFF

func eventMaskParams() []byte {
	b := make([]byte, 8)
	binary.LittleEndian.PutUint64(b, eventMaskClassic)
	return b
}

// bdaddrLE is a BD_ADDR in the order HCI carries it (least significant byte
// first), from the usual colon form.
func bdaddrLE(addr string) ([]byte, error) {
	mac, err := net.ParseMAC(addr)
	if err != nil || len(mac) != 6 {
		return nil, fmt.Errorf("bluetooth: %q is not a BD_ADDR", addr)
	}
	b := make([]byte, 6)
	for i := 0; i < 6; i++ {
		b[i] = mac[5-i]
	}
	return b, nil
}

// inquiryParams asks for ~1.28s × units of general inquiry, unlimited results.
func inquiryParams(units byte) []byte {
	return []byte{giacLAP & 0xFF, giacLAP >> 8 & 0xFF, giacLAP >> 16, units, 0}
}

// createConnParamsBREDR pages one device. Packet types are every ACL type
// (the controller masks what it cannot do); page scan repetition mode R1 is
// right for nearly everything and the controller learns better from a recent
// inquiry anyway. Role switch allowed: a speaker that wants to be central for
// its own multipoint logic may have it.
func createConnParamsBREDR(addr []byte, clockOffset uint16) []byte {
	p := make([]byte, 13)
	copy(p[0:6], addr)
	binary.LittleEndian.PutUint16(p[6:8], 0xCC18) // DM1 DH1 DM3 DH3 DM5 DH5
	p[8] = 0x01                                   // page scan repetition mode R1
	p[9] = 0x00
	if clockOffset != 0 {
		clockOffset |= 0x8000 // valid
	}
	binary.LittleEndian.PutUint16(p[10:12], clockOffset)
	p[12] = 0x01 // allow role switch
	return p
}

// localNameParams pads a UTF-8 name to the 248-byte field, cut on a rune
// boundary.
func localNameParams(name string) []byte {
	p := make([]byte, 248)
	n := 0
	for _, r := range name {
		w := len(string(r))
		if n+w > 247 {
			break
		}
		n += copy(p[n:], string(r))
	}
	return p
}

func codParams(cod uint32) []byte { return []byte{byte(cod), byte(cod >> 8), byte(cod >> 16)} }

func handleParams(handle uint16) []byte { return []byte{byte(handle), byte(handle >> 8)} }

// InquiryResult is one device found by discovery.
type InquiryResult struct {
	Addr        string `json:"addr"`
	Name        string `json:"name,omitempty"`
	Class       uint32 `json:"class"`
	RSSI        int    `json:"rssi,omitempty"`
	clockOffset uint16
}

// IsAudioSink reports whether the Class of Device says the device renders
// audio: a speaker, headphones or a soundbar.
func (r InquiryResult) IsAudioSink() bool {
	return r.Class&codMajorMask == codMajorAudio || r.Class&codServiceRender != 0 && r.Class&codServiceAudio != 0
}

// parseInquiryResults reads any of the three inquiry result events. The
// standard and RSSI forms are arrays laid out field by field across all
// responses (Vol 4 Part E §7.7.2, §7.7.33); the extended form carries one
// response and its EIR, which is where a name usually is (§7.7.38).
func parseInquiryResults(pkt []byte) []InquiryResult {
	if len(pkt) < 4 || pkt[0] != h4TypeEvent {
		return nil
	}
	p := pkt[3:]
	if len(p) != int(pkt[2]) || len(p) < 1 {
		return nil
	}
	switch pkt[1] {
	case evtInquiryResult, evtInquiryResultRSSI:
		n := int(p[0])
		rec := 14 // addr 6, psrm 1, reserved 2, class 3, clock 2
		if pkt[1] == evtInquiryResultRSSI {
			rec = 14 // addr 6, psrm 1, reserved 1, class 3, clock 2, rssi 1
		}
		if n == 0 || len(p) != 1+n*rec {
			return nil
		}
		f := p[1:]
		out := make([]InquiryResult, n)
		for i := 0; i < n; i++ {
			out[i].Addr = formatBdAddr(f[i*6 : i*6+6])
		}
		off := n * 6
		off += n // page scan repetition mode
		if pkt[1] == evtInquiryResult {
			off += 2 * n // two reserved bytes each
		} else {
			off += n // one reserved byte each
		}
		for i := 0; i < n; i++ {
			c := f[off+i*3 : off+i*3+3]
			out[i].Class = uint32(c[0]) | uint32(c[1])<<8 | uint32(c[2])<<16
		}
		off += 3 * n
		for i := 0; i < n; i++ {
			out[i].clockOffset = binary.LittleEndian.Uint16(f[off+i*2:]) & 0x7FFF
		}
		off += 2 * n
		if pkt[1] == evtInquiryResultRSSI {
			for i := 0; i < n; i++ {
				out[i].RSSI = int(int8(f[off+i]))
			}
		}
		return out
	case evtExtInquiryResult:
		// num(1)=1, addr 6, psrm 1, reserved 1, class 3, clock 2, rssi 1, eir 240
		if len(p) != 255 || p[0] != 1 {
			return nil
		}
		r := InquiryResult{
			Addr:        formatBdAddr(p[1:7]),
			Class:       uint32(p[9]) | uint32(p[10])<<8 | uint32(p[11])<<16,
			clockOffset: binary.LittleEndian.Uint16(p[12:14]) & 0x7FFF,
			RSSI:        int(int8(p[14])),
			Name:        eirName(p[15:]),
		}
		return []InquiryResult{r}
	}
	return nil
}

// eirName pulls the complete (0x09) or, failing that, shortened (0x08) local
// name from Extended Inquiry Response data: length-type-value structures, a
// zero length ending the significant part (Core Spec Supplement Part A §1.2).
func eirName(eir []byte) string {
	var short string
	for len(eir) >= 2 {
		l := int(eir[0])
		if l == 0 || 1+l > len(eir) {
			break
		}
		typ, val := eir[1], eir[2:1+l]
		switch typ {
		case 0x09:
			return strings.ToValidUTF8(string(val), "")
		case 0x08:
			short = strings.ToValidUTF8(string(val), "")
		}
		eir = eir[1+l:]
	}
	return short
}

// parseRemoteName reads Remote Name Request Complete: status, addr, 248-byte
// NUL-padded name.
func parseRemoteName(pkt []byte) (addr, name string, ok bool) {
	if len(pkt) < 3+1+6 || pkt[1] != evtRemoteNameComplete || pkt[3] != 0 {
		return "", "", false
	}
	n := pkt[10:]
	if i := strings.IndexByte(string(n), 0); i >= 0 {
		n = n[:i]
	}
	return formatBdAddr(pkt[4:10]), strings.ToValidUTF8(string(n), ""), true
}

// classicConnComplete is the BR/EDR Connection Complete event (§7.7.3).
type classicConnComplete struct {
	status   byte
	handle   uint16
	addr     string
	linkType byte
	encrypt  bool
}

func parseClassicConnComplete(pkt []byte) (classicConnComplete, bool) {
	// status 1, handle 2, addr 6, link type 1, encryption 1
	if len(pkt) < 3+11 || pkt[0] != h4TypeEvent || pkt[1] != evtConnComplete {
		return classicConnComplete{}, false
	}
	p := pkt[3:]
	return classicConnComplete{
		status:   p[0],
		handle:   binary.LittleEndian.Uint16(p[1:3]) & 0x0FFF,
		addr:     formatBdAddr(p[3:9]),
		linkType: p[9],
		encrypt:  p[10] != 0,
	}, true
}

// eventAddr returns the BD_ADDR that leads the parameters of the pairing
// events (Connection Request, PIN/Link Key Request, IO Capability Request,
// User Confirmation Request, Simple Pairing Complete after its status).
func eventAddr(pkt []byte, offset int) (string, bool) {
	if len(pkt) < 3+offset+6 {
		return "", false
	}
	return formatBdAddr(pkt[3+offset : 3+offset+6]), true
}

// eventHandleStatus reads the status + handle pair that Authentication
// Complete and Encryption Change lead with.
func eventHandleStatus(pkt []byte) (status byte, handle uint16, ok bool) {
	if len(pkt) < 3+3 {
		return 0, 0, false
	}
	return pkt[3], binary.LittleEndian.Uint16(pkt[4:6]) & 0x0FFF, true
}

// parseLinkKeyNotification: addr 6, key 16, key type 1.
func parseLinkKeyNotification(pkt []byte) (addr string, key [16]byte, keyType byte, ok bool) {
	if len(pkt) < 3+23 || pkt[1] != evtLinkKeyNotification {
		return "", key, 0, false
	}
	copy(key[:], pkt[9:25])
	return formatBdAddr(pkt[3:9]), key, pkt[25], true
}

func linkKeyReplyParams(addr []byte, key [16]byte) []byte {
	return append(append([]byte{}, addr...), key[:]...)
}

func ioCapReplyParams(addr []byte) []byte {
	// io capability, OOB data not present, authentication requirements
	return append(append([]byte{}, addr...), ioCapNoInputNoOutput, 0x00, authReqGeneralBonding)
}

// pinReplyParams: addr, PIN length, 16-byte PIN field. Legacy pairing only;
// the speakers that still use it nearly all take 0000.
func pinReplyParams(addr []byte, pin string) []byte {
	p := make([]byte, 6+1+16)
	copy(p, addr)
	p[6] = byte(copy(p[7:], pin))
	return p
}

// acceptConnParams: addr, and role — 0x01 stays peripheral, which a speaker
// paging us expects; asking for central here makes some refuse the link.
func acceptConnParams(addr []byte) []byte { return append(append([]byte{}, addr...), 0x01) }

func rejectConnParams(addr []byte, reason byte) []byte {
	return append(append([]byte{}, addr...), reason)
}
