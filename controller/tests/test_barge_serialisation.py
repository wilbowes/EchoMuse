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


def _turn_body() -> str:
    """
    run_esphome_voice_turn with comments stripped.

    The comments in the teardown necessarily name the calls they explain, so
    a source search including them matches the explanation and not the code.
    """
    start = ESPHOME_SRC.index("async def run_esphome_voice_turn")
    body = ESPHOME_SRC[start:]
    body = body[:re.search(r"\n    async def ", body[1:]).start() + 1]
    return "\n".join(
        l for l in body.splitlines() if not l.lstrip().startswith("#")
    )


def test_every_abandoned_turn_serialises_at_teardown():
    """
    #329, generalised. A run we started that HA never finished is still
    emitting onto this connection, and its RUN_END lands on whatever turn is
    running when it arrives — which reads there as "HA ended a run it never
    started" and kills that turn in milliseconds.

    Measured on the 2026-08-25 soak: all four pipeline_refused turns in 15
    hours immediately followed a timeout, none followed a good turn.

    The guard belongs at teardown, not on each early return. Fixing the
    `timeout` path alone left `no_speech` — the same fault, reached whenever
    a wake fires and nobody speaks — one branch away, with a comment
    asserting there was no in-flight pipeline three lines after saying HA
    was listening.
    """
    body = _turn_body()
    assert "if self._run_started and not self._run_finished:" in body, \
        "teardown must serialise a run HA never told us finished"
    guard = body.index("if self._run_started and not self._run_finished:")
    end_turn = body.index("self._barrier.end_turn()")
    assert guard < end_turn, \
        "the abort must arm the barrier before the turn drops it"


def test_teardown_is_reached_by_every_return():
    """
    The guard above is only an invariant because every path runs it. If the
    teardown ever stops being a `finally`, a return added later silently
    opts out and the fault comes back on that path alone — which is exactly
    how it survived the first time.
    """
    lines = _turn_body().splitlines()
    tear = next(i for i, l in enumerate(lines) if "self._turn_active    = False" in l)
    enclosing = next(
        lines[i].strip() for i in range(tear, 0, -1)
        if lines[i].strip() in ("try:", "finally:")
    )
    assert enclosing == "finally:", \
        f"teardown sits under {enclosing!r}; an early return would skip it"


def test_the_serialisation_lives_in_exactly_one_place():
    """
    Two call sites: the barge (via abort_ha_run) and teardown. A third would
    mean a path deciding for itself, which is the shape that produced the
    bug.
    """
    calls = ESPHOME_SRC.count("self.end_ha_run()")
    assert calls == 2, (
        f"end_ha_run has {calls} call sites; it should be abort_ha_run and "
        "teardown only — a per-path abort is what left no_speech uncovered"
    )


def test_ending_the_run_is_idempotent():
    """
    A barge ends the run and then teardown runs anyway. A second
    `start=False` at that moment races the INTERRUPTING turn's own pipeline
    — the precise failure this whole mechanism exists to prevent — so the
    flag must be set before anything is sent, not after.
    """
    fn = ESPHOME_SRC[ESPHOME_SRC.index("def end_ha_run"):][:4000]
    # Past the docstring: it quotes `start=False` while explaining why the
    # ordering matters, and matching that instead of the send is how the
    # first version of this test failed. Third time today.
    code = fn[fn.index('"""', fn.index('"""') + 3) + 3:]
    code = "\n".join(l for l in code.splitlines() if not l.lstrip().startswith("#"))
    early_out = code.index("return")
    set_flag = code.index("self._run_finished = True")
    send = code.index("start=False")
    assert early_out < set_flag < send, \
        "end_ha_run must bail on an already-finished run, and claim the run "\
        "before it sends anything"


def test_a_genuinely_finished_run_is_not_aborted():
    """
    The common case. Both branches where HA really ends a run must say so,
    or every ordinary turn ends by aborting a pipeline that already
    completed and arming a barrier the next turn does not need.
    """
    assert ESPHOME_SRC.count("self._run_finished = True") >= 3, (
        "expected the flag set by end_ha_run plus both terminal RUN_END "
        "branches"
    )
    never_started = ESPHOME_SRC[ESPHOME_SRC.index("self._ha_never_started = True"):][:200]
    assert "self._run_finished = True" in never_started, \
        "a run HA ended without starting is finished, not live"


def test_the_flag_resets_per_turn():
    """
    Left set, it disables the serialisation for the life of the connection —
    silently, and only for the multi-run case that is hard to reproduce.
    """
    body = _turn_body()
    assert "self._run_finished          = False" in body, \
        "the flag must be cleared at turn start beside _run_started"
