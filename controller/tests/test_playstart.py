"""Controller-side playback start reporting (#203)."""

import asyncio

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

    prime = 200
    chunks, task, ev = em_playstart.meter_at_playback_start(gen(), on_start, prime)
    assert task is None and ev is None
    collected = run(_collect(chunks))
    assert len(collected) == 5
    assert fired


def test_meter_at_playback_start_with_report_waits_for_signal():
    async def gen():
        for i in range(3):
            yield bytes([i])

    fired = []

    async def on_start():
        fired.append(True)

    r = em_playstart.StartReport()

    async def drive():
        chunks, task, ev = em_playstart.meter_at_playback_start(
            gen(), on_start, 1000, report=r
        )
        assert task is not None and ev is not None
        collected = []
        async for c in chunks:
            collected.append(c)
        # signal only after collection starts
        return collected, task

    # We need the report to arrive mid-stream; do it inside the generator
    # by signalling before awaiting collection.
    async def main():
        chunks, task, ev = em_playstart.meter_at_playback_start(
            gen(), on_start, 1000, report=r
        )
        assert task is not None
        r.signal()  # device says it started
        collected = []
        async for c in chunks:
            collected.append(c)
        await task
        return collected

    collected = run(main())
    assert len(collected) == 3
    assert fired == [True]
    assert not r._waiters


def test_meter_at_playback_start_report_cancel_retires_waiter():
    async def gen():
        for i in range(3):
            yield bytes([i])

    async def on_start():
        pass

    async def main():
        r = em_playstart.StartReport()
        chunks, task, ev = em_playstart.meter_at_playback_start(
            gen(), on_start, 1000, report=r
        )
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        r.end(ev)  # controller's finally retires it (cancel may land pre-start)
        assert not r._waiters

    run(main())