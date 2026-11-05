import asyncio

from em_tap_burst import TapCoalescer

WINDOW_MS = 60


def run(coro):
    return asyncio.run(coro)


def _collector():
    sent: list[str] = []
    return sent, sent.append


def test_single_tap_emits_single():
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["single"]


def test_three_taps_inside_window_emit_one_triple():
    """The point of the feature: a burst reports once, not N times."""
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        for _ in range(3):
            c.tap(WINDOW_MS)
            await asyncio.sleep(WINDOW_MS / 1000 / 3)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["triple"]


def test_fourth_tap_still_reports_triple():
    """HA only knows the event types advertised at connect — there is no
    "quadruple" to send, so extra taps clamp rather than drop the burst."""
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        for _ in range(6):
            c.tap(WINDOW_MS)
            await asyncio.sleep(WINDOW_MS / 1000 / 6)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["triple"]


def test_taps_outside_window_emit_separately():
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)
        c.tap(WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["single", "single"]


def test_cancel_drops_the_pending_burst():
    """The disconnect path: send_button_event resolves by device_id, so an
    orphaned timer would fire a phantom tap at the replacement connection."""
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        c.tap(WINDOW_MS)
        c.cancel()
        await asyncio.sleep(WINDOW_MS / 1000 * 2)
        return c.count

    assert run(main()) == 0
    assert sent == []


def test_disabled_mid_burst_emits_nothing():
    sent, emit = _collector()
    on = True

    async def main():
        nonlocal on
        c = TapCoalescer(emit, enabled=lambda: on)
        c.tap(WINDOW_MS)
        on = False
        await asyncio.sleep(WINDOW_MS / 1000 * 2)
        return c.count

    assert run(main()) == 0
    assert sent == []


def test_burst_state_resets_between_bursts():
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        c.tap(WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)
        c.tap(WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["double", "single"]


# ── The device's own gap (#115) ──────────────────────────────────────────────

def test_a_device_gap_past_the_window_ends_the_burst_without_waiting():
    """
    Two taps the link delivered together but the user made half a second
    apart are two singles. The first must fire as soon as the second arrives:
    the device has already said the burst is over, and waiting for the
    arrival clock is the jitter the device measured it out of.
    """
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        c.tap(WINDOW_MS, gap_ms=WINDOW_MS + 500)
        assert sent == ["single"], "the first tap waited for a burst the device ended"
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["single", "single"]


def test_a_device_gap_inside_the_window_joins_the_burst():
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        c.tap(WINDOW_MS, gap_ms=WINDOW_MS)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["double"]


def test_without_a_device_gap_arrival_time_still_decides():
    """
    Firmware predating the capability sends no gap, so the same taps are
    judged on arrival as they always were: delivered together is a double.
    """
    sent, emit = _collector()

    async def main():
        c = TapCoalescer(emit)
        c.tap(WINDOW_MS)
        c.tap(WINDOW_MS, gap_ms=None)
        await asyncio.sleep(WINDOW_MS / 1000 * 2)

    run(main())
    assert sent == ["double"]
