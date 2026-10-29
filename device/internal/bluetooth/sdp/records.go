package sdp

// Our own service records: what a speaker reads before it streams from us or
// sends us its buttons.

// Profile and protocol versions we claim. A2DP 1.3 and AVDTP 1.3 are what an
// SBC-only source with delay reporting implements; AVRCP 1.6 over AVCTP 1.4
// is the version sinks look for before offering absolute volume (1.4 and
// later), without browsing, which we do not support.
const (
	VersionA2DP  = 0x0103
	VersionAVDTP = 0x0103
	VersionAVRCP = 0x0106
	VersionAVCTP = 0x0104
)

// A2DP SupportedFeatures for a source (A2DP 1.3 5.3.1): bit 0 Player.
const A2DPSourcePlayer = 0x0001

// AVRCP SupportedFeatures (AVRCP 1.6 8): bits 0-3 are categories 1-4.
// Category 1 is player/recorder (play, pause from a speaker's buttons);
// category 2 is monitor/amplifier (volume).
const (
	AVRCPCategory1 = 0x0001
	AVRCPCategory2 = 0x0002
)

// Record handles. 0 is reserved for the SDP server's own record (Vol 3 Part B
// 2.2); ours start at 0x00010000 as BlueZ's do.
const (
	HandleSDPServer       = 0x00000000
	HandleA2DPSource      = 0x00010001
	HandleAVRCPController = 0x00010002
	HandleAVRCPTarget     = 0x00010003
)

// languageBase declares English, UTF-8 (MIBenum 106), names at 0x0100 (Vol 3
// Part B 5.1.8), so ServiceName is readable at AttrServiceName.
func languageBase() Element {
	return Seq(Seq(Uint16(0x656E), Uint16(106), Uint16(0x0100)))
}

func publicBrowse() Element { return Seq(UUID16(UUIDPublicBrowseRoot)) }

// SDPServerRecord is the server's own record (Vol 3 Part B 5.2).
func SDPServerRecord() Record {
	return Record{Handle: HandleSDPServer, Attrs: []Attr{
		{AttrServiceRecordHandle, Uint32(HandleSDPServer)},
		{AttrServiceClassIDList, Seq(UUID16(UUIDSDPServer))},
		{AttrVersionNumberList, Seq(Uint16(0x0100))},
		{AttrServiceDatabaseState, Uint32(1)},
	}}
}

// A2DPSourceRecord is A2DP 1.3 5.3.1, Figure 5.2.
func A2DPSourceRecord() Record {
	return Record{Handle: HandleA2DPSource, Attrs: []Attr{
		{AttrServiceClassIDList, Seq(UUID16(UUIDAudioSource))},
		{AttrProtocolDescriptorList, Seq(
			Seq(UUID16(UUIDL2CAP), Uint16(PSMAVDTP)),
			Seq(UUID16(UUIDAVDTP), Uint16(VersionAVDTP)),
		)},
		{AttrBrowseGroupList, publicBrowse()},
		{AttrLanguageBaseIDList, languageBase()},
		{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAdvancedAudio), Uint16(VersionA2DP)))},
		{AttrServiceName, Text("EchoMuse Audio Source")},
		{AttrSupportedFeatures, Uint16(A2DPSourcePlayer)},
	}}
}

func avctp() Element {
	return Seq(
		Seq(UUID16(UUIDL2CAP), Uint16(PSMAVCTP)),
		Seq(UUID16(UUIDAVCTP), Uint16(VersionAVCTP)),
	)
}

// AVRCPControllerRecord is AVRCP 1.6 8, Table 8.1: the role that sends
// SetAbsoluteVolume to the speaker, so category 2.
func AVRCPControllerRecord() Record {
	return Record{Handle: HandleAVRCPController, Attrs: []Attr{
		{AttrServiceClassIDList, Seq(UUID16(UUIDAVRCP), UUID16(UUIDAVRCPController))},
		{AttrProtocolDescriptorList, avctp()},
		{AttrBrowseGroupList, publicBrowse()},
		{AttrLanguageBaseIDList, languageBase()},
		{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAVRCP), Uint16(VersionAVRCP)))},
		{AttrServiceName, Text("EchoMuse Remote Control")},
		{AttrSupportedFeatures, Uint16(AVRCPCategory2)},
	}}
}

// AVRCPTargetRecord is AVRCP 1.6 8, Table 8.2: the role that receives play
// and pause from the speaker's buttons, so category 1.
func AVRCPTargetRecord() Record {
	return Record{Handle: HandleAVRCPTarget, Attrs: []Attr{
		{AttrServiceClassIDList, Seq(UUID16(UUIDAVRCPTarget))},
		{AttrProtocolDescriptorList, avctp()},
		{AttrBrowseGroupList, publicBrowse()},
		{AttrLanguageBaseIDList, languageBase()},
		{AttrProfileDescriptorList, Seq(Seq(UUID16(UUIDAVRCP), Uint16(VersionAVRCP)))},
		{AttrServiceName, Text("EchoMuse Media Player")},
		{AttrSupportedFeatures, Uint16(AVRCPCategory1)},
	}}
}

// DefaultRecords is everything an A2DP source with AVRCP serves.
func DefaultRecords() []Record {
	return []Record{SDPServerRecord(), A2DPSourceRecord(), AVRCPControllerRecord(), AVRCPTargetRecord()}
}
