"""BLE transport health: deciding when a reset counter is worth a warning.

Split out as pure logic for the reason em_volume.py and em_linkauth.py were.
Here the reason is also mechanical: em_ble_proxy imports zeroconf, which the
CI test job does not install, so a test that reaches this decision through
that module cannot run where it matters (see controller/CLAUDE.md on keeping
the suite to pure-logic modules).

WHY THE COUNTERS MATTER. `/dev/stpbt` is not a Bluetooth device — it is the
MT8163's combo radio behind MediaTek's WMT stack, shared with WiFi. When a
scan session fails, the scanner reopens the transport, and opening it
triggers a BT function-on plus firmware patch download on the chip carrying
the WiFi link. Observed once on Test Echo 1 (2026-08-30): stpbt read failure,
reopen five seconds later, `network is unreachable` two seconds after that.

WHY IT IS A RISE, NOT A VALUE. The device reports counters cumulative since
ITS process start. They never fall on their own, so warning on "non-zero"
would repeat one reset every 30 seconds forever and bury the next genuine
one. And a counter that goes BACKWARDS means the device restarted, which
rebases rather than reading as a negative delta — otherwise the first real
reset after a reboot is swallowed all the way back up to the old total,
which is exactly the window where resets matter most.
"""

from typing import NamedTuple, Optional


class Observation(NamedTuple):
    """The new baseline, and a warning when the counters climbed."""

    restarts: int
    errors: int
    warning: Optional[str]


def observe(
    prev_restarts: int,
    prev_errors: int,
    restarts: int,
    errors: int,
) -> Observation:
    """
    Fold one stats report's BLE counters into the running baseline.

    Returns the values to store plus a warning string when either counter
    rose, or None when nothing happened worth saying — including the reboot
    case, which rebases silently because a device restarting is not a
    transport reset.
    """
    restarts = max(0, int(restarts or 0))
    errors = max(0, int(errors or 0))
    prev_restarts = max(0, int(prev_restarts or 0))
    prev_errors = max(0, int(prev_errors or 0))

    if restarts < prev_restarts or errors < prev_errors:
        return Observation(restarts, errors, None)

    if restarts == prev_restarts and errors == prev_errors:
        return Observation(restarts, errors, None)

    return Observation(
        restarts,
        errors,
        # State what was counted, not why. The line used to assert that
        # "reopening /dev/stpbt re-initialises the radio WiFi shares" — a
        # hypothesis, printed as fact, in the one place someone reads while
        # diagnosing. On 2026-09-01 it produced a confident wrong call: the
        # WiFi link was already failing FOUR SECONDS BEFORE the reopen, and
        # a mic capture stall preceded both by ten. Whatever the cause is,
        # the log line should not have picked one.
        f"BLE transport reset (+{restarts - prev_restarts} restarts, "
        f"+{errors - prev_errors} errors; {restarts}/{errors} total) — "
        f"check the device log for the read error, and for mic or link "
        f"trouble in the same window",
    )
