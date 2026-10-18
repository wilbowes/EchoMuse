"""Controller-side playback start reporting (#203)."""

import asyncio

import em_controller
import em_playstart


def run(coro):
    return asyncio.run(coro)


def test_startreport_begin_returns_event():
    r = em_playstart.StartReport()
    ev = r.begin()
    assert isinstance(ev, asyncio.Event)
    assert not ev.is_set()


def test_startreport_signal_resolves_oldest_waiter_fifo():
    r = em_playstart.StartReport()
    a, b = r.begin(), r.begin()
    r.signal()
    assert a.is_set() and not b.is_set()
    r.signal()
    assert b.is_set()


def test_startreport_signal_with_no_waiter_is_dropped():
    r = em_playstart.StartReport()
    r.signal()
    ev = r.begin()
    assert not ev.is_set()


def test_startreport_end_retires_waiter():
    r = em_playstart.StartReport()
    ev = r.begin()
    r.end(ev)
    r.signal()
    assert not ev.is_set()


def test_startreport_end_accepts_none_and_missing():
    r = em_playstart.StartReport()
    r.end(None)
    ev = r.begin()
    r.end(ev)
    r.end(ev)


def test_begin_playback_registers_both_ends_and_end_retires_both():
    """
    Every voice stream registers a start and an end waiter in the same place:
    a timer burst or an announcement is a playback too, and registering starts
    only where the meter is wanted let its report resolve the turn's waiter.
    """
    d = em_controller.Device("dev1", "10.0.0.1", [], None)
    p = d.begin_playback()
    assert not p.started.is_set() and not p.ended.is_set()

    d.playback_start.signal()
    assert p.started.is_set()

    d.signal_playback_done()
    assert p.ended.is_set()

    d.end_playback(p)
    assert not d.playback_start._waiters
    assert not d._playback_waiters


def test_two_playbacks_pair_their_reports_in_start_order():
    d = em_controller.Device("dev1", "10.0.0.1", [], None)
    first = d.begin_playback()
    second = d.begin_playback()

    d.playback_start.signal()
    assert first.started.is_set() and not second.started.is_set()
    d.signal_playback_done()
    assert first.ended.is_set() and not second.ended.is_set()

    d.playback_start.signal()
    d.signal_playback_done()
    assert second.started.is_set() and second.ended.is_set()

    d.end_playback(first)
    d.end_playback(second)
    assert not d.playback_start._waiters
    assert not d._playback_waiters


async def _collect(gen):
    out = []
    async for c in gen:
        out.append(c)
    return out


def test_meter_at_playback_start_without_report_estimates():
    async def gen():
        for i in range(5):
            yield bytes([i]) * 100

    fired = []

    async def on_start():
        fired.append(True)

    chunks, task = em_playstart.meter_at_playback_start(gen(), on_start, 200)
    assert task is None
    collected = run(_collect(chunks))
    assert len(collected) == 5
    assert fired


def test_meter_at_playback_start_waits_for_this_playbacks_waiter():
    async def gen():
        for i in range(3):
            yield bytes([i])

    fired = []

    async def on_start():
        fired.append(True)

    async def main():
        started = asyncio.Event()
        chunks, task = em_playstart.meter_at_playback_start(
            gen(), on_start, 1000, started=started)
        assert task is not None
        started.set()  # device says it started
        collected = []
        async for c in chunks:
            collected.append(c)
        await task
        return collected

    collected = run(main())
    assert len(collected) == 3
    assert fired == [True]


def test_meter_watcher_cancel_does_not_touch_the_queue():
    """
    The waiter belongs to Device.begin_playback now; cancelling the watcher
    (a playback that ended before the device reported) must leave the queue
    to end_playback, which retires it in the caller's finally.
    """
    async def gen():
        for i in range(3):
            yield bytes([i])

    async def on_start():
        pass

    async def main():
        d = em_controller.Device("dev1", "10.0.0.1", [], None)
        p = d.begin_playback()
        chunks, task = em_playstart.meter_at_playback_start(
            gen(), on_start, 1000, started=p.started)
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        d.end_playback(p)
        assert not d.playback_start._waiters

    run(main())


def test_meter_at_playback_start_fires_when_the_audio_runs_out():
    # A reply shorter than the prime window never reaches the byte count, so
    # exhaustion is the trigger; without it a two-word answer has no meter.
    async def gen():
        yield b"x" * 100

    fired = []

    async def on_start():
        fired.append(True)

    chunks, task = em_playstart.meter_at_playback_start(gen(), on_start, 1000)
    assert task is None
    assert len(run(_collect(chunks))) == 1
    assert fired == [True]
