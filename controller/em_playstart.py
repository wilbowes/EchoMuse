"""
When playback starts: the device says so, or we estimate it (#203).

The device sent exactly one playback message — `playback_stats`, once its audio
channel drained after EOS — so the controller has always known precisely when
audio STOPPED and has had to guess when it STARTED, in two places: the
Speaking tile was set on the first period put on the wire, and the meter ring
was raised once `SPEAKER_PRIME_SECONDS` of audio had been handed to the
streamer. Both lead the speaker by up to the prime window, because the device
deliberately holds audio until roughly a second is queued (`primePeriods`,
pcm_speaker.go) or EOS arrives, and a socket write completes near-instantly
however slow the link is — the same property that makes `send_ms` useless as a
delivery measure.

Firmware announcing `playback_start` sends `playback_started` when its ALSA
write begins. The device knows, and there is no better witness: it is the
instant the barge-in threshold keys off and the level tap the meter renders is
read. This module holds the controller's half: the per-playback rendezvous the
control handler resolves (`StartReport`), and the trigger the meter ring is
raised from (`meter_at_playback_start`). Without the report both fall back to
the estimate, which is the behaviour of every device in the field today.
"""

from __future__ import annotations

import asyncio
import collections


class StartReport:
    """
    Per-playback waiters for the device's `playback_started` (#203).

    A QUEUE of waiters, one per playback, rather than a single Event on the
    device — the shape the playback-END waiters arrived at the hard way
    (#373): two playbacks can be in flight (a turn and an announcement), and
    with one Event both wake on whichever report arrives first. FIFO because
    the device plays one stream at a time and reports them in the order it
    starts them, so the oldest outstanding playback owns a report.

    A report with nothing waiting is dropped rather than remembered: a start
    cannot be "saved up" for a playback whose audio has not been sent yet. A
    fresh event per playback retires the `clear()` discipline a single Event
    would need, but only if a cancelled playback takes its waiter out — which
    is why `meter_at_playback_start` owns both halves and retires the waiter in
    its own finally.
    """

    def __init__(self) -> None:
        self._waiters: collections.deque = collections.deque()

    def begin(self) -> asyncio.Event:
        """Register a waiter for this playback's start and return it."""
        ev = asyncio.Event()
        self._waiters.append(ev)
        return ev

    def end(self, ev: asyncio.Event | None) -> None:
        """
        Retire a waiter, whether or not the device ever reported. Idempotent,
        and None is accepted so a caller that never registered one (firmware
        without the report) can call it unconditionally in a finally.

        Must be called: a playback cancelled before the device began playing
        (barge-in during the prime window) never gets a report, and an
        abandoned waiter would take the NEXT playback's start.
        """
        if ev is None:
            return
        try:
            self._waiters.remove(ev)
        except ValueError:
            pass

    def signal(self) -> None:
        """Resolve the oldest outstanding playback: the device has started."""
        if self._waiters:
            self._waiters.popleft().set()


def meter_at_playback_start(pcm_chunks, on_start, prime_bytes: int,
                            report: StartReport | None = None):
    """
    Pass PCM through untouched, firing `on_start` when playback begins.

    `report`, when given, is the device's own report of that moment on firmware
    announcing `playback_start`, and it is then the ONLY trigger — the byte
    count below is not a fallback here, because a second trigger firing up to a
    prime window early is the error being removed. The wait happens in a task
    of its own and never inside the generator: pcm_chunks is what FEEDS the
    device, so a controller waiting in it for the device to start would be
    waiting for audio it is not sending.

    Without one, the trigger is the estimate the controller has always used:
    the device holds audio until roughly `prime_bytes` is queued, and counting
    what has been handed to the streamer tracks that closely enough. A stream
    that is cancelled or yields nothing never fires, and the spinner simply
    stays up until the turn's ring cleanup — the failure direction that looks
    like "still working" rather than "dead".

    Returns (chunks, task). The task is the report's watcher and must be
    cancelled by the caller when the playback ends; it is None when the byte
    count is the trigger.
    """
    if report is not None:
        ev = report.begin()

        async def _on_device_report() -> None:
            try:
                await ev.wait()
                await on_start()
            finally:
                # In the watcher's own finally, so a playback that ends (or is
                # cancelled) before the device ever reported cannot leave a
                # waiter behind to take the next playback's start.
                report.end(ev)

        return pcm_chunks, asyncio.create_task(_on_device_report()), ev

    return _estimate_at_playback_start(pcm_chunks, on_start, prime_bytes), None, None


async def _estimate_at_playback_start(pcm_chunks, on_start, prime_bytes: int):
    """The byte-count estimate — `meter_at_playback_start` without a report.

    Was `_meter_at_playback_start` in em_controller; moved here with the
    device-report trigger so the choice between them is one readable function
    and can be tested without a device (#203).
    """
    sent = 0
    fired = False
    async for chunk in pcm_chunks:
        yield chunk
        if not fired:
            sent += len(chunk)
            if sent >= prime_bytes:
                fired = True
                await on_start()
    if not fired:
        # Exhaustion fires it too, so a two-word answer still gets a meter.
        await on_start()
