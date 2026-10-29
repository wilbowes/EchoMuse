package avrcp

import (
	"encoding/binary"
	"fmt"
)

// AV/C command types and response codes (AV/C General 4.0 §5.3.1-5.3.2).
const (
	CtypeControl         = 0x0
	CtypeStatus          = 0x1
	CtypeSpecificInquiry = 0x2
	CtypeNotify          = 0x3
	CtypeGeneralInquiry  = 0x4

	RespNotImplemented = 0x8
	RespAccepted       = 0x9
	RespRejected       = 0xA
	RespInTransition   = 0xB
	RespStable         = 0xC // also IMPLEMENTED
	RespChanged        = 0xD
	RespInterim        = 0xF
)

const (
	// Subunit byte: type(5) | ID(3). AVRCP addresses the panel subunit, ID 0;
	// UNIT INFO and SUBUNIT INFO address the unit itself (type 0x1F, ID 7).
	subunitPanel = 0x09<<3 | 0
	subunitUnit  = 0x1F<<3 | 7

	opVendor      = 0x00
	opUnitInfo    = 0x30
	opSubunitInfo = 0x31
	opPassThrough = 0x7C

	// CompanyBTSIG is the Bluetooth SIG's IEEE company ID, which every AVRCP
	// vendor-dependent PDU carries (AVRCP 1.6 §6.3.1).
	CompanyBTSIG = 0x001958
)

// frame is one AV/C frame: code (ctype or response), subunit, opcode and the
// opcode's operands.
type frame struct {
	code     uint8
	subunit  uint8
	opcode   uint8
	operands []byte
}

func (f frame) encode() []byte {
	return append([]byte{f.code & 0x0F, f.subunit, f.opcode}, f.operands...)
}

// parseFrame reads an AV/C frame. The top nibble of the first byte is
// reserved and zero (AV/C §5.3), and AVRCP never uses the extended subunit
// forms, so either is rejected rather than misparsed.
func parseFrame(b []byte) (frame, error) {
	if len(b) < 3 || len(b) > MaxFrame || b[0]&0xF0 != 0 {
		return frame{}, ErrMalformed
	}
	if t, id := b[1]>>3, b[1]&0x07; t == 0x1E || id == 5 {
		return frame{}, ErrMalformed
	}
	return frame{b[0], b[1], b[2], append([]byte(nil), b[3:]...)}, nil
}

// Op is a PASS THROUGH operation ID (AV/C Panel Subunit 1.23 Table 9.21).
type Op uint8

const (
	OpVolumeUp   Op = 0x41
	OpVolumeDown Op = 0x42
	OpMute       Op = 0x43
	OpPlay       Op = 0x44
	OpStop       Op = 0x45
	OpPause      Op = 0x46
	OpForward    Op = 0x4B
	OpBackward   Op = 0x4C

	opVendorUnique Op = 0x7E
)

// supportedOps are the operations we act on as a category 1 target; anything
// else gets NOT IMPLEMENTED (AVRCP 1.6 §4.6.1, Table 4.5).
var supportedOps = map[Op]bool{
	OpVolumeUp: true, OpVolumeDown: true, OpMute: true, OpPlay: true,
	OpStop: true, OpPause: true, OpForward: true, OpBackward: true,
}

func (o Op) String() string {
	switch o {
	case OpVolumeUp:
		return "volume_up"
	case OpVolumeDown:
		return "volume_down"
	case OpMute:
		return "mute"
	case OpPlay:
		return "play"
	case OpStop:
		return "stop"
	case OpPause:
		return "pause"
	case OpForward:
		return "forward"
	case OpBackward:
		return "backward"
	}
	return fmt.Sprintf("op_0x%02x", uint8(o))
}

// PASS THROUGH operands: state(1) | operation ID(7), then the operation data
// length and data (Panel Subunit §9.4). The state bit is 0 pressed, 1 released.
func passThroughOperands(op Op, released bool) []byte {
	b := uint8(op) & 0x7F
	if released {
		b |= 0x80
	}
	return []byte{b, 0}
}

func parsePassThrough(o []byte) (op Op, released bool, err error) {
	if len(o) < 2 || int(o[1]) != len(o)-2 {
		return 0, false, ErrMalformed
	}
	return Op(o[0] & 0x7F), o[0]&0x80 != 0, nil
}

// AVRCP PDU IDs (AVRCP 1.6 §6.4-6.13, Table 4.5).
const (
	pduGetCapabilities           = 0x10
	pduGetPlayStatus             = 0x30
	pduRegisterNotification      = 0x31
	pduRequestContinuingResponse = 0x40
	pduAbortContinuingResponse   = 0x41
	pduSetAbsoluteVolume         = 0x50
)

// GetCapabilities capability IDs (§6.4.1).
const (
	capCompanyID       = 0x02
	capEventsSupported = 0x03
)

// Event is a RegisterNotification event ID (§6.7.2, Table 6.29).
type Event uint8

const (
	EventPlaybackStatusChanged Event = 0x01
	EventTrackChanged          Event = 0x02
	EventVolumeChanged         Event = 0x0D
)

// PlayStatus is the play status a target reports (§6.7.1, Table 6.27).
type PlayStatus uint8

const (
	PlayStopped PlayStatus = 0x00
	PlayPlaying PlayStatus = 0x01
	PlayPaused  PlayStatus = 0x02
	PlayError   PlayStatus = 0xFF
)

// AVRCP error status codes (§6.15.3, Table 6.49).
const (
	StatusInvalidCommand   = 0x00
	StatusInvalidParameter = 0x01
	StatusParameterContent = 0x02
	StatusInternalError    = 0x03
)

// vendorPDU is an AVRCP PDU inside a VENDOR DEPENDENT frame: company ID(3),
// PDU ID(1), packet type(1, low 2 bits), parameter length(2), parameters
// (§6.3.1). Packet types beyond single mark a fragmented response.
type vendorPDU struct {
	id      uint8
	pktType uint8
	params  []byte
}

func (v vendorPDU) operands() []byte {
	b := make([]byte, 7, 7+len(v.params))
	b[0], b[1], b[2] = CompanyBTSIG>>16, CompanyBTSIG>>8&0xFF, CompanyBTSIG&0xFF
	b[3], b[4] = v.id, v.pktType&0x03
	binary.BigEndian.PutUint16(b[5:7], uint16(len(v.params)))
	return append(b, v.params...)
}

// parseVendor reads VENDOR DEPENDENT operands. The company is returned even
// when the rest is unreadable, so a non-SIG PDU can be told apart; the
// declared parameter length must match exactly.
func parseVendor(o []byte) (company uint32, v vendorPDU, err error) {
	if len(o) < 3 {
		return 0, v, ErrMalformed
	}
	company = uint32(o[0])<<16 | uint32(o[1])<<8 | uint32(o[2])
	if len(o) < 7 {
		return company, v, ErrMalformed
	}
	v = vendorPDU{id: o[3], pktType: o[4] & 0x03}
	if int(binary.BigEndian.Uint16(o[5:7])) != len(o)-7 {
		return company, v, ErrMalformed
	}
	v.params = append([]byte(nil), o[7:]...)
	return company, v, nil
}

// vendorFrame wraps an AVRCP PDU in an AV/C frame addressed to the panel.
func vendorFrame(code, id uint8, params []byte) frame {
	return frame{code, subunitPanel, opVendor, vendorPDU{id: id, params: params}.operands()}
}

// VolumeToAbs maps our 0..100 volume to AVRCP's absolute volume, 0..0x7F
// (§6.13.1: 0 is 0%, 0x7F is 100%), clamping out-of-range input.
func VolumeToAbs(pct int) uint8 {
	if pct <= 0 {
		return 0
	}
	if pct >= 100 {
		return 0x7F
	}
	return uint8((pct*0x7F + 50) / 100)
}

// AbsToVolume maps an absolute volume back to 0..100. Bit 7 is reserved
// (§6.13.1) and ignored, as a receiver must.
func AbsToVolume(abs uint8) int {
	return (int(abs&0x7F)*100 + 0x7F/2) / 0x7F
}
