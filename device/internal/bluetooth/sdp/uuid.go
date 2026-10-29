package sdp

import "fmt"

// UUID is a full 128-bit UUID in network (big-endian) order. 16- and 32-bit
// UUIDs are aliases inside the Base UUID (Vol 3 Part B 2.5.1), so every UUID
// compares in this one form: a search for 0x110B must match a record that
// spells it as 0x0000110B or as the full 128 bits.
type UUID [16]byte

// baseUUID is 00000000-0000-1000-8000-00805F9B34FB.
var baseUUID = UUID{0, 0, 0, 0, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB}

// UUIDFrom16 expands a 16-bit UUID into the Base UUID.
func UUIDFrom16(v uint16) UUID { return UUIDFrom32(uint32(v)) }

// UUIDFrom32 expands a 32-bit UUID into the Base UUID.
func UUIDFrom32(v uint32) UUID {
	u := baseUUID
	u[0], u[1], u[2], u[3] = byte(v>>24), byte(v>>16), byte(v>>8), byte(v)
	return u
}

// Short is the 32-bit alias when u lies in the Base UUID's range.
func (u UUID) Short() (uint32, bool) {
	if [12]byte(u[4:]) != [12]byte(baseUUID[4:]) {
		return 0, false
	}
	return uint32(u[0])<<24 | uint32(u[1])<<16 | uint32(u[2])<<8 | uint32(u[3]), true
}

func (u UUID) String() string {
	if v, ok := u.Short(); ok {
		if v <= 0xFFFF {
			return fmt.Sprintf("0x%04X", v)
		}
		return fmt.Sprintf("0x%08X", v)
	}
	return fmt.Sprintf("%X-%X-%X-%X-%X", u[0:4], u[4:6], u[6:8], u[8:10], u[10:16])
}

// Assigned Numbers: the protocol and service class UUIDs this package uses.
const (
	UUIDSDP              = 0x0001
	UUIDAVCTP            = 0x0017
	UUIDAVDTP            = 0x0019
	UUIDL2CAP            = 0x0100
	UUIDSDPServer        = 0x1000
	UUIDPublicBrowseRoot = 0x1002
	UUIDAudioSource      = 0x110A
	UUIDAudioSink        = 0x110B
	UUIDAVRCPTarget      = 0x110C
	UUIDAdvancedAudio    = 0x110D // A2DP's profile descriptor
	UUIDAVRCP            = 0x110E // AVRCP's profile descriptor, and the CT class
	UUIDAVRCPController  = 0x110F
)

// Universal attribute IDs (Vol 3 Part B 5.1) and the profile-specific ones
// read or served here.
const (
	AttrServiceRecordHandle    = 0x0000
	AttrServiceClassIDList     = 0x0001
	AttrProtocolDescriptorList = 0x0004
	AttrBrowseGroupList        = 0x0005
	AttrLanguageBaseIDList     = 0x0006
	AttrProfileDescriptorList  = 0x0009
	AttrServiceName            = 0x0100 // primary language base + 0
	AttrVersionNumberList      = 0x0200 // SDP server record only
	AttrServiceDatabaseState   = 0x0201 // SDP server record only
	AttrSupportedFeatures      = 0x0311 // A2DP and AVRCP
)

// L2CAP PSMs fixed by Assigned Numbers.
const (
	PSMSDP   = 0x0001
	PSMAVCTP = 0x0017
	PSMAVDTP = 0x0019
)
