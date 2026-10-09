"""
em_supersede.py — whether a question asked inside a turn replaces that turn.

`assist_satellite.ask_question` called from within a voice turn's own intent
deadlocks against it (#506, reported in #423). The question is spoken, the
answer window opens about 30 seconds late, and Home Assistant reports "No
answer from question".

The circle: `_run_announce` awaits `_start_conversation_turn`, which reaches
`_run_voice_locked` and blocks on `device.voice_lock` — held by the wake turn
that is still waiting for a TTS response. The wake turn cannot finish because
HA's own script is blocked: `async_internal_ask_question` awaits its answer
future with no timeout, so the intent that called it never completes and
INTENT_END never arrives. Only our 30s timeout breaks it, by which point the
user answered 30 seconds ago. Measured across three devices: 6 of 8 turns in
one window, 5 of 21 in another, every failing cycle identical.

The turn being superseded is by construction one that cannot progress until
the question is answered, so queueing behind it waits for something that will
never arrive. Queueing is the wrong answer and standing down is worse — the
user did speak.

Pure, for the reason em_barge and em_runbarrier are pure: the suite cannot
import em_esphome, so this is the only part of the decision with coverage.
"""

from __future__ import annotations

from typing import NamedTuple


class Verdict(NamedTuple):
    # Cancel the live turn so its voice_lock is released for the question.
    supersede: bool
    # The recorded outcome for the turn being replaced.
    reason: str
    # Abort HA's pipeline, not just our end of it. The protocol carries no run
    # id and HA does not serialise runs, so a local-only cancel leaves two
    # runs on one connection and the old one's tail events kill the new turn.
    abort_ha: bool


#: The outcome recorded on the turn that was replaced. Its own label, not
#: "barged" and not "cancelled": nobody spoke over the assistant, and the
#: dashboard groups wake statistics by trigger prefix, so borrowing a barge
#: label would file this under #250's barge-detection query.
SUPERSEDED = "superseded"


def decide(*, turn_active: bool, run_started: bool, run_finished: bool,
           intent_ended: bool, already_cancelled: bool) -> Verdict:
    """
    Whether the `start_conversation` half of an announcement replaces the turn
    in progress.

    `turn_active` — a turn holds the device's voice lock right now.
    `run_started` / `run_finished` — HA's pipeline for THAT turn, read off the
    same fields the turn's own teardown guard uses. A run that never started
    has nothing to abort, and one that already finished needs no abort: the
    response is delivered and only our speaker is still busy.
    `intent_ended` — HA reached INTENT_END, so the turn is past the stage
    #506 deadlocks at. Superseding past it is harmless but pointless, and
    leaving it alone keeps a normal turn's outcome intact.
    `already_cancelled` — something else got here first (a barge, a button, a
    mute). First reason wins in `cancel_turn`, and re-deciding would relabel
    it.

    Returns supersede=False and an empty reason when there is nothing live to
    replace, which is the ordinary case: an announcement outside a turn.
    """
    if not turn_active or run_finished or intent_ended or already_cancelled:
        return Verdict(supersede=False, reason="", abort_ha=False)
    return Verdict(supersede=True, reason=SUPERSEDED, abort_ha=run_started)
