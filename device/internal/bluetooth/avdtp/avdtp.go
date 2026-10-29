// Package avdtp is the Audio/Video Distribution Transport Protocol (AVDTP
// 1.3) and A2DP's RTP media packets, for a source streaming to a Bluetooth
// speaker. It does no I/O: the caller carries PDUs over the two L2CAP
// channels on PSM 0x0019 — signalling first, then media — and feeds inbound
// signalling to Session.Feed.
//
// Codec-specific bytes (SBC's capability IE) are opaque here; package sbc
// owns them.
package avdtp

import "fmt"

// PSM is AVDTP's L2CAP PSM, for both the signalling and the media channel.
const PSM = 0x0019

// SignalID identifies a signalling procedure (AVDTP 1.3 §8.5).
type SignalID uint8

const (
	SigDiscover           SignalID = 0x01
	SigGetCapabilities    SignalID = 0x02
	SigSetConfiguration   SignalID = 0x03
	SigGetConfiguration   SignalID = 0x04
	SigReconfigure        SignalID = 0x05
	SigOpen               SignalID = 0x06
	SigStart              SignalID = 0x07
	SigClose              SignalID = 0x08
	SigSuspend            SignalID = 0x09
	SigAbort              SignalID = 0x0A
	SigSecurityControl    SignalID = 0x0B
	SigGetAllCapabilities SignalID = 0x0C
	SigDelayReport        SignalID = 0x0D
)

var sigNames = map[SignalID]string{
	SigDiscover: "DISCOVER", SigGetCapabilities: "GET_CAPABILITIES",
	SigSetConfiguration: "SET_CONFIGURATION", SigGetConfiguration: "GET_CONFIGURATION",
	SigReconfigure: "RECONFIGURE", SigOpen: "OPEN", SigStart: "START", SigClose: "CLOSE",
	SigSuspend: "SUSPEND", SigAbort: "ABORT", SigSecurityControl: "SECURITY_CONTROL",
	SigGetAllCapabilities: "GET_ALL_CAPABILITIES", SigDelayReport: "DELAYREPORT",
}

func (s SignalID) String() string {
	if n, ok := sigNames[s]; ok {
		return n
	}
	return fmt.Sprintf("signal 0x%02x", uint8(s))
}

// MessageType is the header's 2-bit message type (§8.4.3).
type MessageType uint8

const (
	MsgCommand       MessageType = 0
	MsgGeneralReject MessageType = 1
	MsgAccept        MessageType = 2
	MsgReject        MessageType = 3
)

// ErrorCode is a reject's error code (§8.20.6.2).
type ErrorCode uint8

const (
	CodeBadHeaderFormat          ErrorCode = 0x01
	CodeBadLength                ErrorCode = 0x11
	CodeBadACPSEID               ErrorCode = 0x12
	CodeSEPInUse                 ErrorCode = 0x13
	CodeSEPNotInUse              ErrorCode = 0x14
	CodeBadServCategory          ErrorCode = 0x17
	CodeBadPayloadFormat         ErrorCode = 0x18
	CodeNotSupportedCommand      ErrorCode = 0x19
	CodeInvalidCapabilities      ErrorCode = 0x1A
	CodeBadRecoveryType          ErrorCode = 0x22
	CodeBadMediaTransportFormat  ErrorCode = 0x23
	CodeBadRecoveryFormat        ErrorCode = 0x25
	CodeBadROHCFormat            ErrorCode = 0x26
	CodeBadCPFormat              ErrorCode = 0x27
	CodeBadMultiplexingFormat    ErrorCode = 0x28
	CodeUnsupportedConfiguration ErrorCode = 0x29
	CodeBadState                 ErrorCode = 0x31
)

func (e ErrorCode) String() string { return fmt.Sprintf("0x%02x", uint8(e)) }

// MediaType is a stream endpoint's media type (Assigned Numbers).
type MediaType uint8

const (
	MediaAudio      MediaType = 0x00
	MediaVideo      MediaType = 0x01
	MediaMultimedia MediaType = 0x02
)

// SEPType is the TSEP bit: which end of the stream an endpoint is.
type SEPType uint8

const (
	SEPSource SEPType = 0
	SEPSink   SEPType = 1
)

// SEPInfo is one entry of a DISCOVER response (§8.6.2).
type SEPInfo struct {
	SEID  uint8
	InUse bool
	Media MediaType
	Type  SEPType
}

// SEIDs are 6 bits, and 0x00 and 0x3F are forbidden (§8.20.1).
func validSEID(s uint8) bool { return s >= 0x01 && s <= 0x3E }

// RejectError is a peer's refusal of one of our commands. Category is set
// only for SET_CONFIGURATION and RECONFIGURE, SEID only for START and SUSPEND.
type RejectError struct {
	Signal   SignalID
	General  bool // a General Reject: the peer does not know the signal
	Category Category
	SEID     uint8
	Code     ErrorCode
}

func (e *RejectError) Error() string {
	if e.General {
		return fmt.Sprintf("avdtp: %v: general reject", e.Signal)
	}
	return fmt.Sprintf("avdtp: %v rejected: error %v", e.Signal, e.Code)
}

// CodeError lets a Select or Accept callback choose the error code a
// configuration is refused with; any other error means
// UNSUPPORTED_CONFIGURATION.
type CodeError struct{ Code ErrorCode }

func (e *CodeError) Error() string { return fmt.Sprintf("avdtp: error %v", e.Code) }
