package speaker

import (
	"fmt"
	"strconv"
	"strings"
)

// The playback substream's status file, which is how we find out whether
// anyone else holds the speaker BEFORE trying to open it.
//
// This exists because tinyalsa's pcm_open is open(fn, O_RDWR) with no
// O_NONBLOCK, and we link -ltinyalsa from the compiler image's sysroot rather
// than vendoring its source — so the open itself cannot be made non-blocking
// without patching the toolchain's library. ALSA core parks a blocking open on
// pcm->open_wait for as long as the substream is busy, with no timeout, which
// on this hardware means forever.
//
// Measured on a stranded device (issue #80, 2026-08-09): a Dot booted with a
// headphone plug inserted has Android's mediaserver holding this substream,
// and our thread sat in snd_pcm_open for eighteen minutes with the whole of
// main() behind it — no buttons, no wake word, no controller registration.
func statusPath(card, device int) string {
	return fmt.Sprintf("/proc/asound/card%d/pcm%dp/sub0/status", card, device)
}

// pcmFree reports whether the substream is available to open.
//
// A free substream's status file contains exactly "closed"; a held one leads
// with "state: <STATE>" and an owner_pid. Anything we do not recognise counts
// as FREE, deliberately: this guard exists to avoid a hang we know how to
// detect, and treating an unfamiliar format as busy would refuse to open a
// perfectly good speaker on some device whose procfs we have never seen.
// Failing open costs us the old behaviour; failing closed costs the speaker.
func pcmFree(status string) bool {
	first := strings.TrimSpace(strings.SplitN(strings.TrimSpace(status), "\n", 2)[0])
	if strings.EqualFold(first, "closed") {
		return true
	}
	// Busy has one shape and we match it POSITIVELY: a held substream leads
	// with "state: <STATE>". Treating everything-that-is-not-closed as busy
	// would make an unfamiliar procfs format indistinguishable from a device
	// we cannot have, and refuse to open a speaker that was never held.
	key, _, found := strings.Cut(first, ":")
	return !(found && strings.EqualFold(strings.TrimSpace(key), "state"))
}

// pcmOwner returns the pid holding the substream, or 0 if the status does not
// name one. Reported in the log line so a stall names the culprit — the whole
// diagnosis of #80 turned on finding "owner_pid: 659" in this file, and that
// took a day of hardware round trips to reach.
func pcmOwner(status string) int {
	for _, line := range strings.Split(status, "\n") {
		key, val, found := strings.Cut(line, ":")
		if !found || strings.TrimSpace(key) != "owner_pid" {
			continue
		}
		if n, err := strconv.Atoi(strings.TrimSpace(val)); err == nil {
			return n
		}
	}
	return 0
}

// pcmDelay reads the frames queued ahead of the DAC from a RUNNING
// substream's status: the ALSA buffer not yet played plus the driver's own
// FIFO. Reading the file makes the kernel sync hw_ptr from the DMA position
// first, and that position is real rather than period bookkeeping (measured
// 2026-08-10: sub-period steps, 47973 fps against 48000), which is what lets
// Sendspin place a period to well under a millisecond.
//
// ok is false for anything but a running stream with a readable delay: a
// guess here would schedule audio against a clock that is not there.
func pcmDelay(status string) (frames int, ok bool) {
	running := false
	for _, line := range strings.Split(status, "\n") {
		key, val, found := strings.Cut(line, ":")
		if !found {
			continue
		}
		switch strings.TrimSpace(key) {
		case "state":
			running = strings.TrimSpace(val) == "RUNNING"
		case "delay":
			n, err := strconv.Atoi(strings.TrimSpace(val))
			if err != nil || n < 0 {
				return 0, false
			}
			frames, ok = n, true
		}
	}
	return frames, ok && running
}
