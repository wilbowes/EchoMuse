"""
#299: the wake-stream watchdog treated a dropped link as a broken mic.

On a lossy link, no mic frames arrive because the transport keeps
dropping — but the watchdog's escalation ladder (defensive mic_start,
then mic_stop+mic_start) assumed a zombie stream device-side and acted:
extra control-plane traffic on an already failing link, and a working
mic pipeline torn down and rebuilt (bedroom, 2026-08-23: AEC resyncs
3, 4, 5, 6).

The distinction needs state AND time (review on PR #311): a boolean
"fresh connection" guard made the zombie case unrecoverable, because a
device streaming to a superseded socket never delivers a frame on the
fresh one — so the flag never flipped and the escalation that repairs
the Office-style 4.7-hour deafness could never run. The guard is
therefore time-bounded: fresh connections get FRESH_CONN_GRACE_S; past
that, silence IS the zombie signature and the ladder runs.

em_controller is deliberately not importable here (see conftest); these
are shape guards on the shipped source.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _listener_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("async def wake_word_listener")
    return src[start:start + 30_000]


def test_link_down_stands_down_before_the_ladder():
    src = _listener_src()
    guard = src.index("if device.data_ws is None:")
    ladder = src.index("dead_streak += 1", guard)
    assert guard < ladder, \
        "a fully down data plane must stand the watchdog down first"
    branch_end = src.find("continue", guard)
    between = src[guard:branch_end]
    assert "dead_streak" not in between and "mic_start" not in between, \
        "a down link must neither count nor repair"


def test_fresh_connection_gets_a_bounded_grace():
    src = _listener_src()
    fresh = src.index("not device.frames_seen_this_connection")
    grace = src.index("FRESH_CONN_GRACE_S", fresh)
    branch_end = src.find("continue", grace)
    between = src[grace:branch_end]
    assert "dead_streak" not in between and "mic_start" not in between, \
        "within the grace window the watchdog must stand down"
    assert src[branch_end:].startswith("continue")


def test_past_the_grace_the_ladder_runs_zombie_repair():
    """
    The reason the guard is time-bounded: a zombie stream never delivers a
    frame on the fresh connection, so an unbounded grace would be the
    Office incident (deaf 4.7h) again. Past the window, silence must fall
    through to the escalation ladder.
    """
    src = _listener_src()
    grace_check = src.index("< FRESH_CONN_GRACE_S")
    after = src[grace_check:src.index("dead_streak = 0", grace_check)]
    assert "zombie" in after, "past-grace silence must be named as the zombie case"
    ladder = after.find("dead_streak += 1")
    assert ladder != -1, \
        "past the grace window the escalation ladder must be reachable"


def test_both_time_sources_are_updated():
    src = (CONTROLLER / "em_controller.py").read_text()
    hd = src[src.index("device.data_ws = ws"):]
    hd = hd[:hd.index("async def", 10)]
    assert "data_connected_at" in hd, \
        "handle_data must timestamp each new connection"
    # Both ends anchored from the SAME start, or the slice silently inverts:
    # the end marker is searched from 0, so any `except asyncio.TimeoutError`
    # appearing earlier in the file (voice-stream pacing added one) makes
    # this src[later:earlier] — an empty string that fails with no clue why.
    _wl_start = src.index("payload = await asyncio.wait_for(")
    wl = src[_wl_start:src.index("except asyncio.TimeoutError", _wl_start)]
    assert "frames_seen_this_connection = True" in wl, \
        "a delivered frame must set the flag"
