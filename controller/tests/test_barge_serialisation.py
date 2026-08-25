"""
The ESPHome voice protocol serialises pipeline runs at the SATELLITE or not
at all.

`VoiceAssistantEventResponse` carries `event_type` and a `data` list of
name/value pairs — there is no run identifier anywhere in the protocol, so a
client structurally cannot attribute an event to a particular run. Home
Assistant does not enforce one-run-at-a-time either: `handle_pipeline_start`
clears the audio queue and cancels the TTS streaming task, then overwrites
`_pipeline_task` WITHOUT cancelling the previous one. Starting a second run
therefore orphans the first, which keeps emitting events onto the same
connection.

Barge-in is the only place two runs can overlap, and it did. Measured on
2026-08-17: five barge-ins, five interrupting turns dead in 4-17ms with zero
audio captured, because the aborted run's RUN_END arrived ~4ms after the new
turn started and the "HA ended a run it never started" branch read it as
terminal.

The state machine lives in `em_runbarrier` rather than in `em_esphome` for the
reason `em_linkauth.decide` does: this suite cannot import `em_esphome` (it
pulls in zeroconf, aiohttp and the database), so logic that lives there has no
coverage. Both of this machine's failure modes are silent — swallow too little
and the interrupting turn dies, swallow too much and the satellite goes deaf —
which is exactly the shape that should not be sitting untested inside a big
async method.

The wiring in `em_esphome` is pinned separately, against the source.
"""

import re
from pathlib import Path

from em_runbarrier import RunBarrier

ESPHOME_SRC = (Path(__file__).resolve().parents[1] / "em_esphome.py").read_text()
CONTROLLER_SRC = (Path(__file__).resolve().parents[1] / "em_controller.py").read_text()


# ── The barrier ──────────────────────────────────────────────────────────────


def test_an_untouched_barrier_discards_nothing():
    """
    The overwhelmingly common case: no barge, no abort, every event delivered.
    """
    b = RunBarrier()
    b.begin_turn()
    assert b.discards(is_run_start=False) is False
    assert b.discards(is_run_start=True) is False


def test_an_abort_arms_the_NEXT_turn_not_the_current_one():
    """
    The abort happens during the turn being abandoned, and that turn still has
    tearing-down of its own to do. It is the turn AFTER it that must not see
    the old run's tail.
    """
    b = RunBarrier()
    b.begin_turn()
    b.abort()
    assert b.discards(is_run_start=False) is False, "armed the turn doing the aborting"
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is True


def test_the_stale_tail_is_discarded_until_run_start():
    """
    The measured failure. RUN_END, STT_VAD_END and the orphan's eventual ERROR
    all arrived on the new turn; each one alone is enough to kill it.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True   # stale RUN_END
    assert b.discards(is_run_start=False) is True   # stale STT_VAD_END
    assert b.discards(is_run_start=False) is True   # stale ERROR


def test_run_start_releases_the_barrier_and_is_itself_delivered():
    """
    RUN_START is the release AND a real event. Swallowing it would leave
    `_run_started` False and re-arm the very bug this fixes, for the turn's
    own terminal RUN_END.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=True) is False
    assert b.active is False


def test_everything_after_run_start_is_delivered():
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    b.discards(is_run_start=False)                  # stale, dropped
    b.discards(is_run_start=True)                   # ours, releases
    assert b.discards(is_run_start=False) is False
    assert b.discards(is_run_start=False) is False


def test_the_barrier_does_not_outlive_its_turn():
    """
    If HA never sends the RUN_START we are waiting for — connection dropped,
    pipeline failed to start — the barrier must come down when the turn ends.
    Events are dispatched whether or not a turn is in progress, so a barrier
    left standing discards them indefinitely.

    `begin_turn` would also clear it, but only once another turn starts; that
    is not a bound, because the turn that would clear it is one whose events
    are being discarded.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()                                    # RUN_START never came
    assert b.active is False
    assert b.discards(is_run_start=False) is False


def test_the_barrier_is_bounded_to_one_turn():
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is False


def test_an_arm_is_consumed_not_merely_read():
    """
    One abort protects exactly one turn. Leaving `armed` set would re-arm
    every subsequent turn off a single barge.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.armed is False
    b.discards(is_run_start=True)
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is False


def test_two_aborts_before_a_turn_still_protect_one_turn():
    b = RunBarrier()
    b.abort()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()
    b.begin_turn()
    assert b.discards(is_run_start=False) is False


# ── The wiring, pinned against the source ────────────────────────────────────


def test_the_abort_actually_reaches_ha():
    """
    `VoiceAssistantRequest(start=False)` is the one message that reaches into
    HA's in-flight pipeline: aioesphomeapi maps it to `handle_stop(True)` ->
    `_abort_pipeline()`, which queues the audio sentinel AND cancels
    `_pipeline_task`. cancel_turn's old docstring claimed no such mechanism
    existed, citing an ESPHOME_SPEC.md §7.4 that is not in the tree.
    """
    assert "VoiceAssistantRequest(start=False)" in ESPHOME_SRC, (
        "nothing aborts HA's pipeline — the old run keeps emitting onto the "
        "connection the interrupting turn is using"
    )


def test_the_satellite_hands_the_arm_over_at_turn_start_and_drops_it_at_end():
    """
    Both calls are load-bearing and neither is obviously necessary at the call
    site, which is how one of them would get tidied away.
    """
    assert "self._barrier.begin_turn()" in ESPHOME_SRC
    assert "self._barrier.end_turn()" in ESPHOME_SRC


def test_the_event_handler_consults_the_barrier_before_anything_else():
    """
    The gate has to sit above the dispatch, not inside a branch — a stale
    STT_VAD_END is as fatal to the interrupting turn as a stale RUN_END, and
    listing event types to guard would go stale the next time one is added.
    """
    handler = ESPHOME_SRC[ESPHOME_SRC.index("def _handle_voice_event"):]
    gate = handler.index("self._barrier")
    first_dispatch = handler.index("if event_type ==")
    assert gate < first_dispatch, (
        "the barrier must gate every event, not selected ones"
    )


def test_barge_serialises_in_both_phases():
    """
    Thinking starts another turn on this connection, so HA's run must be
    aborted first. Playback must serialise too: RUN_END follows TTS_END, so a
    barge in the first milliseconds of audio can beat it.
    """
    watcher = CONTROLLER_SRC[CONTROLLER_SRC.index("async def _barge_watcher"):]
    watcher = watcher[: watcher.index("\nasync def ", 10)]
    assert "abort_ha=True" in watcher, (
        "barge during thinking must abort HA's pipeline before the "
        "interrupting turn starts"
    )
    assert "abort_ha_run" in watcher, (
        "barge during playback must serialise against the interrupting turn"
    )


def test_the_unarmed_run_end_path_is_still_there():
    """
    HA's wake-word interception emits RUN_END having never started a pipeline.
    That is genuinely terminal, and missing it stalled the voice satellite
    setup dialog for the life of the feature. The barrier is armed only by an
    abort, so this path must survive untouched.
    """
    assert re.search(r"if not self\._run_started:", ESPHOME_SRC), (
        "the RUN_END-without-RUN_START discriminator is gone"
    )
    assert "self._ha_never_started = True" in ESPHOME_SRC


def test_a_timeout_serialises_the_same_way_a_barge_does():
    """
    A timeout orphans HA's run exactly as an abort does — we stop waiting
    while the pipeline is still live — so it needs the same barrier.

    Without it the stale run's RUN_END lands on the NEXT turn, which has not
    seen its own RUN_START, and _ha_never_started reads it as terminal.
    Measured on the 2026-08-25 soak: all four pipeline_refused turns in 15
    hours immediately followed a timeout and none followed a good turn, so
    the user re-asking after a slow intent was refused in ~3ms, twice.
    """
    # Anchored on the message, not on `except asyncio.TimeoutError:` — there
    # are two, and the earlier one is the mic-streaming hard cap, which
    # deliberately falls through to this wait rather than abandoning the run.
    body = ESPHOME_SRC[ESPHOME_SRC.index("Timeout waiting for TTS response from HA"):]
    body = body[:body.index("\n            if self._turn_cancelled:")]
    assert "abort_ha_run()" in body, \
        "a timed-out turn must abort HA's run and arm the barrier"
    # abort_ha_run defaults the reason to "barged". Nobody spoke over
    # anything here, and _turn_end_reason is the CAUSE.
    assert '_turn_end_reason = "timeout"' in body, \
        "the reason must be set before abort_ha_run defaults it to barged"
    assert body.index('_turn_end_reason = "timeout"') < body.index("abort_ha_run()"), \
        "setting it after the call is too late — the default has already won"
