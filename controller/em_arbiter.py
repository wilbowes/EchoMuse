"""
em_arbiter.py — multi-device wake arbitration
==============================================

When one utterance wakes more than one Echo (open-plan rooms, hallways),
every device that scored above threshold would start its own voice turn.
This module elects a single responder, stock-Alexa-style (their "ESP" —
Echo Spatial Perception).

Mechanics: the first detection opens a *round* with a deadline one
arbitration window from now (`wakeArbitrationMs`, per-device config but
sensibly set fleet-wide). Detections landing before the deadline join
the round; at the deadline the best contender wins and every submitter's
await resolves with the winner's device id. Detections that arrive just
*after* a round resolved (a straggler of the same utterance — device
batching means spreads of a few hundred ms are normal) lose to that
round's winner instead of opening a fresh round and double-answering.

The ranking metric is (SNR, score): speech RMS at detection relative to
that device's own tracked noise floor is a proximity proxy — the closer
device hears you louder above *its* room — while raw wake score
saturates near 1.0 for any clean detection and mostly breaks ties.

Latency cost: every wake waits out the window. Callers should skip
arbitration entirely (don't call submit) when only one device is
connected, and the window is configurable down to 0 (= off).

Pure asyncio, no imports from the rest of the controller — unit-tested
in tests/test_arbiter.py.
"""

from __future__ import annotations

import asyncio

# Detections this close behind an already-resolved round are stragglers
# of the same utterance, not a new wake. Beyond it, a detection is a
# genuinely separate wake (someone else, another room) and gets its own
# round even though a turn may already be running elsewhere.
STRAGGLER_WINDOW_S = 1.0


class _Round:
    __slots__ = ("started", "deadline", "entries", "winner")

    def __init__(self, started: float, deadline: float):
        self.started = started
        self.deadline = deadline
        # device_id -> ((snr, score), future)
        self.entries: dict[str, tuple[tuple[float, float], asyncio.Future]] = {}
        self.winner: str | None = None


class WakeArbiter:
    def __init__(self) -> None:
        self._round: _Round | None = None
        self._last: _Round | None = None

    async def submit(self, device_id: str, snr: float, score: float,
                     window_s: float) -> str:
        """
        Register a wake detection; returns the winning device_id once the
        round resolves (== device_id if this device won). The window is
        set by the round's first detection.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        rnd = self._round
        if rnd is None:
            last = self._last
            if (last is not None and last.winner is not None
                    and last.winner != device_id
                    and now - last.started < STRAGGLER_WINDOW_S):
                return last.winner
            rnd = _Round(now, now + window_s)
            self._round = rnd
            asyncio.create_task(self._resolve(rnd))

        fut: asyncio.Future = loop.create_future()
        rnd.entries[device_id] = ((snr, score), fut)
        return await fut

    async def _resolve(self, rnd: _Round) -> None:
        loop = asyncio.get_running_loop()
        delay = rnd.deadline - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._round is rnd:
            self._round = None
        rnd.winner = max(rnd.entries.items(), key=lambda kv: kv[1][0])[0]
        self._last = rnd
        for _metric, fut in rnd.entries.values():
            if not fut.done():
                fut.set_result(rnd.winner)
