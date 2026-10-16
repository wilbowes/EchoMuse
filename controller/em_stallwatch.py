"""Stack dump when the event loop stalls, off unless asked.

`event_loop_lag_monitor` already warns at 250ms. This adds, behind
EM_LOOP_WATCHDOG, a faulthandler dump once the stall passes a second, and
a line naming the turn or upload that was in progress. The monitor re-arms
the dump on every tick: the timeout is the sleep plus one second, so a
healthy tick cancels it before it fires.

Logging only. Nothing here changes a turn.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys

log = logging.getLogger("echomuse")

_TRUTHY = {"1", "true", "yes", "on"}


def enabled(env=None) -> bool:
    if env is None:
        env = os.environ
    return str(env.get("EM_LOOP_WATCHDOG", "")).strip().lower() in _TRUTHY


def arm(timeout: float, file=None) -> None:
    """Schedule a traceback if the loop does not come back within `timeout`."""
    if not enabled():
        return
    faulthandler.dump_traceback_later(
        timeout, repeat=False, file=file or sys.stderr, exit=False)


def disarm() -> None:
    if not enabled():
        return
    faulthandler.cancel_dump_traceback_later()


def activity() -> str:
    """Which turn holds a voice lock, and which uploads are in progress."""
    turns = []
    try:
        import em_state
        for dev in list(em_state.devices.values()):
            lock = getattr(dev, "voice_lock", None)
            if lock is not None and lock.locked():
                turns.append(str(getattr(dev, "device_id", "?")))
    except Exception:
        log.debug("stall watchdog could not read turns", exc_info=True)
    uploads = []
    try:
        import em_ota
        uploads = sorted(em_ota.in_progress)
    except Exception:
        log.debug("stall watchdog could not read uploads", exc_info=True)
    return (f"turns={','.join(turns) or '-'} "
            f"uploads={','.join(uploads) or '-'}")


def note(lag_ms: float) -> None:
    if enabled() and lag_ms >= 1000:
        log.warning(f"[loop] stall {lag_ms:.0f}ms blocking — {activity()}")


async def measure(loop, interval: float) -> float:
    """Sleep `interval` and return how late the wake was, in milliseconds.

    Arms the dump for `interval + 1s` first and cancels it on the way out,
    so the next call from the stall checker is what re-arms it.
    """
    arm(interval + 1.0)
    t0 = loop.time()
    try:
        await asyncio.sleep(interval)
    finally:
        disarm()
    lag_ms = (loop.time() - t0 - interval) * 1000
    note(lag_ms)
    return lag_ms
