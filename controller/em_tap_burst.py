"""
em_tap_burst.py — coalesce a burst of Action-button taps into one event

A tap can only be classified as "single" once no second tap follows it, so
detecting a double means holding every tap for the length of the window.
That cost is only acceptable because `buttonSingleTapEvent` has already
turned a tap into an HA event rather than the start of a voice turn — see
CLAUDE.md's Action Button section.

Each tap restarts the window; when it finally expires the burst reports
once as single/double/triple. Anything beyond three reports as triple:
HA only knows the event types advertised at connect time, so there is no
"quadruple" to send.

Pure asyncio, no imports from the rest of the controller — unit-tested in
tests/test_tap_burst.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

MAX_TAP_COUNT = 3
TAP_EVENT_NAMES = {1: "single", 2: "double", 3: "triple"}


class TapCoalescer:
    """
    One in-flight tap burst. Owned by a Device, and cancelled when that
    device's connection ends.

    `emit(name)` sends the finished burst; `enabled()` is re-checked when
    the window expires, because the setting can be turned off mid-burst
    and firing then would emit an event for a disabled feature.
    """

    def __init__(
        self,
        emit: Callable[[str], None],
        enabled: Callable[[], bool] = lambda: True,
        on_error: Callable[[asyncio.Task], None] | None = None,
    ) -> None:
        self._emit = emit
        self._enabled = enabled
        self._on_error = on_error
        self._count = 0
        self._timer: asyncio.Task | None = None

    @property
    def count(self) -> int:
        """Taps recorded so far in the current burst. For tests/diagnostics."""
        return self._count

    def tap(self, window_ms: int) -> None:
        """Record a tap and (re)start the window."""
        self._count += 1
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._expire(window_ms))
        if self._on_error is not None:
            self._timer.add_done_callback(self._on_error)

    def cancel(self) -> None:
        """Drop any pending burst without emitting."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._count = 0

    async def _expire(self, window_ms: int) -> None:
        await asyncio.sleep(window_ms / 1000)
        n = min(self._count, MAX_TAP_COUNT)
        # Reset before emitting: a tap landing during the send would
        # otherwise join a burst already reported and be lost.
        self._count = 0
        self._timer = None
        if not self._enabled():
            return
        self._emit(TAP_EVENT_NAMES[n])
