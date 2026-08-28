package speaker

import "testing"

// Verbatim from Test Device (G090LF1180570SPJ) 2026-08-09, while Android's
// mediaserver held the speaker and the server was stranded in snd_pcm_open.
const heldStatus = `state: PREPARED
owner_pid   : 659
trigger_time: 0.000000000
tstamp      : 1239.509457377
delay       : 0
avail       : 3072
avail_max   : 0
-----
hw_ptr      : 0
appl_ptr    : 0
`

// The same file once our own thread had taken the device over.
const runningStatus = `state: RUNNING
owner_pid   : 1094
trigger_time: 1786260229.525790163
tstamp      : 1786260262.082172242
delay       : 8064
avail       : 128
avail_max   : 6256
-----
hw_ptr      : 1562816
appl_ptr    : 1570880
`

func TestPcmFreeWhenClosed(t *testing.T) {
	if !pcmFree("closed\n") {
		t.Fatal("a closed substream must read as free")
	}
}

func TestPcmBusyWhenHeld(t *testing.T) {
	if pcmFree(heldStatus) {
		t.Fatal("a PREPARED substream held by another process must read as busy")
	}
	if pcmFree(runningStatus) {
		t.Fatal("a RUNNING substream must read as busy")
	}
}

// Unknown content must not block the open. Refusing on an unrecognised format
// would disable the speaker on hardware whose procfs we have never seen, to
// avoid a hang that device may not even have.
func TestUnknownStatusFailsOpen(t *testing.T) {
	for _, s := range []string{"", "   ", "something we have never seen"} {
		if !pcmFree(s) {
			t.Fatalf("unrecognised status %q must read as free, not busy", s)
		}
	}
}

func TestPcmOwner(t *testing.T) {
	if got := pcmOwner(heldStatus); got != 659 {
		t.Fatalf("owner_pid = %d, want 659", got)
	}
	if got := pcmOwner(runningStatus); got != 1094 {
		t.Fatalf("owner_pid = %d, want 1094", got)
	}
	if got := pcmOwner("closed\n"); got != 0 {
		t.Fatalf("a closed substream names no owner, got %d", got)
	}
}

func TestStatusPathMatchesTheDeviceWeOpen(t *testing.T) {
	// Pins the path against biscuit's card/device numbers (0/23; the mic is
	// 24 — checking pcm0p, Android's own device, reads "closed" and proves
	// nothing, which cost a wrong conclusion during the #80 hunt). Board
	// numbers now live in each tagged binding file (pcm_speaker.go for
	// `server`), so this test pins the literal values directly rather than
	// importing a per-board constant.
	want := "/proc/asound/card0/pcm23p/sub0/status"
	if got := statusPath(0, 23); got != want {
		t.Fatalf("statusPath = %q, want %q", got, want)
	}
}
