package speaker

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
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

// pcmFreeTimeout bounds the wait for another process to release the speaker.
//
// Generous, because the wait is the good outcome: releasing takes Android well
// under a second once `stop media` lands, and a device that waits ten seconds
// and then works is enormously better than one that gives up early. It is a
// backstop against a holder that never lets go, not a tuning parameter.
const pcmFreeTimeout = 10 * time.Second

// waitForFreePcm blocks until the playback substream is released, or the
// timeout expires.
//
// On timeout it RETURNS ANYWAY and lets the open proceed. That open may then
// block forever, which is the pre-existing behaviour — this function's job is
// to make the common case work and to leave a log line naming the holder when
// it does not. Refusing to open would be a bigger behaviour change than the
// bug warrants, and the proper fix for a permanently-held device is to stop
// gating the rest of main() on the speaker at all.
//
// Untagged: pure procfs polling, no ALSA client involved, shared by every
// board's binding.
func waitForFreePcm(card, device int, timeout time.Duration) {
	path := statusPath(card, device)
	b, err := os.ReadFile(path)
	if err != nil || pcmFree(string(b)) {
		return // free, or no status file to consult — open immediately
	}

	log.Printf("[speaker] %s held by pid %d — waiting up to %s", path, pcmOwner(string(b)), timeout)
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		time.Sleep(200 * time.Millisecond)
		b, err := os.ReadFile(path)
		if err != nil || pcmFree(string(b)) {
			log.Printf("[speaker] speaker released after %s", time.Since(deadline.Add(-timeout)).Round(time.Millisecond))
			return
		}
	}
	b, _ = os.ReadFile(path)
	log.Printf("[speaker] speaker STILL held by pid %d after %s — opening anyway, this may block",
		pcmOwner(string(b)), timeout)
}
