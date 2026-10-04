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
	// The status file must be the PLAYBACK substream of the PCM the speaker
	// opens (biscuit: card 0 device 23, pinned in pkg/board's tests). Checking
	// pcm0p (Android's own) reads "closed" and proves nothing, which cost a
	// wrong conclusion during the #80 hunt.
	want := "/proc/asound/card0/pcm23p/sub0/status"
	if got := statusPath(0, 23); got != want {
		t.Fatalf("statusPath = %q, want %q", got, want)
	}
}

// Read off a real device's running stream (runningStatus, above).
func TestPcmDelayReadsARunningStream(t *testing.T) {
	if n, ok := pcmDelay(runningStatus); !ok || n != 8064 {
		t.Errorf("got %d %v, want 8064 true", n, ok)
	}
	if _, ok := pcmDelay(heldStatus); ok {
		t.Error("a PREPARED stream held by another process gave a delay")
	}
}

// No delay is trusted from a stream that is not running, or one whose delay
// cannot be read: Sendspin would schedule against a clock that is not there.
func TestPcmDelayRefusesAnythingButARunningStream(t *testing.T) {
	for name, status := range map[string]string{
		"closed":   "closed\n",
		"prepared": "state: PREPARED\ndelay       : 0\n",
		"xrun":     "state: XRUN\ndelay       : 4096\n",
		"no delay": "state: RUNNING\navail       : 879\n",
		"garbage":  "state: RUNNING\ndelay       : lots\n",
	} {
		if _, ok := pcmDelay(status); ok {
			t.Errorf("%s: accepted", name)
		}
	}
}
