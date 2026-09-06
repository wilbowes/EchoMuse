"""
RTT excursion log coalescing.

Measured on the EA controller 2026-08-28: 2880 excursion lines in 13 hours
from two devices, 48% of the whole log ring, all of it duplicating counters
already persisted to device_metrics. 23 of the 2880 were busy — the ones that
actually delayed audio — and they were indistinguishable in the flood.
"""

import em_rttlog as R


def log(device="dev1", summary_sec=600.0):
    return R.ExcursionLog(device, summary_sec=summary_sec)


# ─── Busy excursions are never coalesced ──────────────────────────────────────

def test_busy_excursion_logs_immediately():
    line = log().record(1011, busy=True, now=0.0)
    assert line == "[dev1] RTT excursion: 1011ms (busy)"


def test_every_busy_excursion_logs():
    L = log()
    assert L.record(900, busy=True, now=0.0)
    assert L.record(950, busy=True, now=1.0)


def test_busy_excursions_do_not_disturb_an_open_idle_window():
    L = log()
    L.record(600, busy=False, now=0.0)
    L.record(900, busy=True, now=10.0)
    assert L.idle_count == 1
    # The idle window still closes on its own schedule, and the busy sample
    # is not in its count or its worst.
    line = L.record(700, busy=False, now=600.0)
    assert "2 idle excursions" in line
    assert "worst 700ms" in line


# ─── Idle excursions coalesce ─────────────────────────────────────────────────

def test_idle_excursion_is_silent_inside_the_window():
    L = log()
    assert L.record(600, busy=False, now=0.0) is None
    assert L.record(700, busy=False, now=59.0) is None


def test_window_closes_with_count_and_worst():
    L = log()
    for i in range(46):
        L.record(500 + i, busy=False, now=float(i))
    line = L.record(2140, busy=False, now=600.0)
    assert line == "[dev1] RTT: 47 idle excursions in 10m, worst 2140ms"


def test_window_resets_after_emitting():
    L = log()
    L.record(600, busy=False, now=0.0)
    L.record(2000, busy=False, now=600.0)
    assert L.idle_count == 0
    assert L.idle_worst_ms == 0
    assert L.idle_since is None
    # The next window starts at the next excursion, not at the last emit.
    assert L.record(700, busy=False, now=1200.0) is None
    assert L.record(800, busy=False, now=1801.0) == \
        "[dev1] RTT: 2 idle excursions in 10m, worst 800ms"


def test_elapsed_is_reported_not_assumed():
    """
    A window is closed by the next excursion, which can arrive hours late.
    Reporting the nominal 10m there would misstate the rate the line exists
    to convey.
    """
    L = log()
    L.record(600, busy=False, now=0.0)
    line = L.record(700, busy=False, now=3600.0)
    assert "in 60m" in line


def test_a_quiet_link_emits_nothing():
    """No timer, by design: no excursions means nothing to report."""
    L = log()
    assert L.idle_since is None
    assert L.idle_count == 0


# ─── The volume claim, as arithmetic ──────────────────────────────────────────

def test_coalescing_is_worth_doing():
    """
    The measured EA rate — 1443 idle excursions from one device over 13h —
    must come out as tens of lines, not thousands.
    """
    L = log()
    emitted = 0
    interval = (13 * 3600) / 1443
    for i in range(1443):
        if L.record(600, busy=False, now=i * interval):
            emitted += 1
    assert emitted <= 80, f"{emitted} lines is not a reduction"
