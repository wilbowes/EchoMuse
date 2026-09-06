"""
Coalescing for control-plane RTT excursion log lines.

Every ping over RTT_EXCURSION_MS used to write its own INFO line (#360). On a
fleet of two devices that was 2880 lines in 13 hours — 48% of the controller's whole
log ring — and it displaced the lines a support bundle is collected for. The
measurement itself was never in those lines: `record_rtt` already counts
excursions into `device_metrics` with an idle/busy denominator, so the log was
a duplicate of a stored metric.

Two kinds of excursion, and only one of them is bulk:

- **Busy** — the device was in a turn or playing audio, so the excursion
  delayed something audible. 23 of the 2880. Logged individually, as before.
- **Idle** — the interesting property is the RATE, not any single sample.
  Coalesced into one line per window, carrying the count and the worst.

The tail of a window is emitted by the next excursion rather than by a timer.
A window that never closes means the excursions stopped, which is the outcome
worth having; the persisted counters are the authority either way.

Pure and dependency-free so it can be tested without the websocket stack, for
the reason em_linkauth is: the part worth getting right is a handful of lines
buried in a module that pulls in openwakeword.
"""

from typing import Optional

# How long idle excursions accumulate before a summary line is emitted.
# Ten minutes keeps a bad link visible within a support bundle's reach while
# costing ~6 lines an hour per device instead of ~220.
IDLE_SUMMARY_SEC = 600.0


class ExcursionLog:
    """Per-device state for RTT excursion logging. One line in, one or none out."""

    def __init__(self, device_id: str, summary_sec: float = IDLE_SUMMARY_SEC):
        self.device_id = device_id
        self.summary_sec = summary_sec
        self.idle_count = 0
        self.idle_worst_ms = 0
        # Monotonic time the current idle window opened. None = no window.
        self.idle_since: Optional[float] = None

    def record(self, rtt_ms: int, busy: bool, now: float) -> Optional[str]:
        """
        Returns the line to log, or None.

        `now` is a monotonic clock, passed in rather than read so the window
        is testable.
        """
        if busy:
            return f"[{self.device_id}] RTT excursion: {rtt_ms}ms (busy)"

        if self.idle_since is None:
            self.idle_since = now
        self.idle_count += 1
        self.idle_worst_ms = max(self.idle_worst_ms, rtt_ms)

        if now - self.idle_since < self.summary_sec:
            return None

        # Report the window that actually elapsed, not the nominal one: the
        # closing sample can arrive long after the window expired, and a line
        # claiming 10m for a 3h gap would misstate the rate it exists to show.
        mins = round((now - self.idle_since) / 60)
        line = (f"[{self.device_id}] RTT: {self.idle_count} idle excursions "
                f"in {mins}m, worst {self.idle_worst_ms}ms")
        self.idle_count = 0
        self.idle_worst_ms = 0
        self.idle_since = None
        return line
