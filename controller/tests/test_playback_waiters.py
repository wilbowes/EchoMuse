"""
Playback completion belongs to a playback, not to a device.

`Device.playback_done` was a single `asyncio.Event` with two waiters —
`_run_post_turn_playback` and the chunked voice path — and one setter, the
`playback_stats` handler. Two concurrent playbacks therefore both woke on
whichever report arrived first.

Observed on the EA controller, 2026-08-28 20:07 (#373): two `Playback
complete (device-confirmed)` lines in the same millisecond, and an
announcement whose wait ended after a chime's duration rather than its own.

The class under test is defined in em_controller, which the suite cannot
import — it pulls in aiohttp, zeroconf and openwakeword. So the four methods
are lifted from source and exercised on a bare object, the same approach
tests/test_deploy.py uses for call-site shape. If they are renamed or moved,
the lift fails loudly rather than silently testing nothing.
"""
import ast
import asyncio
import collections
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[1]


def _playback_api():
    """Build a throwaway class carrying only the playback-waiter methods."""
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    device = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "Device"
    )
    wanted = {"begin_playback", "end_playback", "signal_playback_done"}
    methods = [n for n in device.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]
    missing = wanted - {m.name for m in methods}
    assert not missing, (
        f"em_controller.Device no longer defines {sorted(missing)} — if they "
        "were renamed, update this test to match rather than deleting it"
    )
    ns: dict = {"asyncio": asyncio, "collections": collections}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "<lift>", "exec"), ns)

    class Dev:
        def __init__(self):
            self._playback_waiters = collections.deque()
    for name in wanted:
        setattr(Dev, name, ns[name])
    return Dev


Dev = _playback_api()


def test_a_report_resolves_only_its_own_playback():
    """
    The bug, directly: two playbacks in flight, one report.

    Under the old single Event both woke. The first report belongs to the
    first playback — the device plays one stream at a time and reports in the
    order it finishes them.
    """
    d = Dev()
    first, second = d.begin_playback(), d.begin_playback()

    d.signal_playback_done()
    assert first.is_set(), "the first playback's waiter was not resolved"
    assert not second.is_set(), (
        "the second playback woke on a report belonging to the first — this "
        "is the two-Playback-complete-lines-in-one-millisecond bug (#373)"
    )

    d.signal_playback_done()
    assert second.is_set()


def test_a_cancelled_playback_does_not_steal_the_next_report():
    """
    A barge-in, a mute or a dropped device ends a playback with no report
    coming. Its waiter must leave the queue, or every later playback resolves
    one report early and for ever — a silent desync that would present as
    playback completing before the audio does.
    """
    d = Dev()
    abandoned = d.begin_playback()
    d.end_playback(abandoned)

    live = d.begin_playback()
    d.signal_playback_done()

    assert live.is_set(), "the live playback did not get its own report"
    assert not abandoned.is_set()


def test_end_playback_is_idempotent():
    """Teardown paths in em_controller are reached more than once by design."""
    d = Dev()
    ev = d.begin_playback()
    d.end_playback(ev)
    d.end_playback(ev)  # must not raise
    assert not d._playback_waiters


def test_a_stray_report_is_dropped_not_saved():
    """
    A report with nothing waiting must not be remembered for a playback that
    has not started yet. That is the stale-set hazard the old `clear()` calls
    existed to work around, and a fresh Event per playback removes it by
    construction — but only if a spare report is discarded rather than queued.
    """
    d = Dev()
    d.signal_playback_done()          # nothing waiting
    later = d.begin_playback()
    assert not later.is_set(), (
        "a playback resolved before its audio was sent — a stray report was "
        "saved up instead of dropped"
    )


def test_waiters_do_not_accumulate_across_playbacks():
    """A leak here would grow unbounded on a device that plays a lot."""
    d = Dev()
    for _ in range(50):
        ev = d.begin_playback()
        d.signal_playback_done()
        d.end_playback(ev)
    assert not d._playback_waiters


@pytest.mark.parametrize("n", [2, 5])
def test_reports_resolve_in_order(n):
    """FIFO: the device finishes streams in the order it received them."""
    d = Dev()
    evs = [d.begin_playback() for _ in range(n)]
    for i in range(n):
        d.signal_playback_done()
        assert evs[i].is_set()
        assert not any(e.is_set() for e in evs[i + 1:]), (
            "a later playback resolved out of order"
        )
