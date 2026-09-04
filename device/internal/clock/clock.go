// Package clock sets the device's wall clock from the controller.
//
// An Echo has no RTC that survives a power cut and boots reading 2010. Under
// FireOS Android's own time service eventually fixes that when it has
// internet; under emOS nothing does, and it cannot: bionic resolves hostnames
// through Android's property service rather than /etc/resolv.conf, so no
// bionic-linked binary on that system has DNS at all, and `ntpd` cannot look
// up a pool server. Pointing it at the gateway works only on networks whose
// router happens to serve NTP.
//
// So the controller tells us instead, over the authenticated outbound link the
// device already trusts. No DNS, no listening socket, no dependency on
// anything outside the user's own network — the same properties that make the
// shell plane acceptable.
//
// **This does not affect any of the project's timing instrumentation, and must
// not.** The firmware never sends a timestamp: RTT, wake-crossing ages and
// stall gaps are all measured with time.Since against Go's monotonic clock,
// which settimeofday does not perturb — a time.Time from time.Now() carries a
// monotonic reading and Sub uses it. Fixing the wall clock makes log lines
// correlatable with the controller's; it changes no measurement.
//
// It also does not affect TLS. The device clamps its verification clock to the
// firmware build time precisely because it cannot trust its own clock at dial
// time, and that clamp stays: this runs after a connection exists, so it can
// never be what a connection depends on.
package clock

import (
	"time"

	"golang.org/x/sys/unix"
)

// StepThreshold is how wrong the clock has to be before we touch it.
//
// Sized against the link, not against ambition. The ack crosses a network
// measured at 1-2s RTT excursions on this fleet and carries no delay
// compensation, so the answer is good to about a second and no better —
// stepping for a sub-second disagreement would be churn that cannot even be
// verified. It is also comfortably below the fault this exists for, which is a
// clock reading fifteen years out.
//
// The generous threshold has a second job on FireOS, where Android's own time
// service is also setting the clock: two setters an hour apart is fine, two
// setters fighting over half a second is not.
const StepThreshold = 30 * time.Second

// ShouldStep says whether `serverUnixMs` is far enough from the device's own
// clock to be worth applying. Pure, so the rule is testable without root.
//
// A zero or negative server time means the controller did not send one — an
// older controller, or a field that did not survive a round trip. That is
// "no opinion", never "the epoch", and must leave the clock alone: setting a
// device to 1970 is strictly worse than leaving it in 2010, because it is a
// value that looks deliberate.
func ShouldStep(deviceNow time.Time, serverUnixMs int64) bool {
	if serverUnixMs <= 0 {
		return false
	}
	drift := time.UnixMilli(serverUnixMs).Sub(deviceNow)
	if drift < 0 {
		drift = -drift
	}
	return drift >= StepThreshold
}

// Step sets CLOCK_REALTIME to serverUnixMs.
//
// clock_settime rather than settimeofday: arm64 is a 64-bit-time-only
// architecture and does not implement the settimeofday syscall at all, so the
// obvious call is one that compiles everywhere and fails on the hardware we
// ship to.
func Step(serverUnixMs int64) error {
	ts := unix.NsecToTimespec(serverUnixMs * int64(time.Millisecond))
	return unix.ClockSettime(unix.CLOCK_REALTIME, &ts)
}
