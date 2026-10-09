"""
#506: a question asked from inside a voice turn replaces that turn, or the
answer window never opens.

The deadlock, measured across three devices: 6 of 8 turns in one window, 5 of
21 in another, every failing cycle identical.

Pure decision tests. The call site is in em_esphome, which the CI suite cannot
import, so this is the only part with coverage; the structural guard that the
supersede is actually reached lives in test_supersede_wiring.py below.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_supersede


def _v(**kw):
    base = dict(turn_active=True, run_started=True, run_finished=False,
                intent_ended=False, already_cancelled=False)
    base.update(kw)
    return em_supersede.decide(**base)


def test_a_question_inside_a_live_turn_supersedes_it():
    v = _v()
    assert v.supersede is True
    assert v.reason == em_supersede.SUPERSEDED


def test_the_reason_is_not_a_barge():
    """Nobody spoke over the assistant, and the dashboard groups wake
    statistics by trigger prefix — a barge label would file these turns under
    #250's barge-detection query, which is the one thing that query is for."""
    assert _v().reason not in ("barged", "cancelled")
    assert not em_supersede.SUPERSEDED.startswith("wakeword")
    assert not em_supersede.SUPERSEDED.startswith("button")


def test_a_live_run_is_aborted_not_just_our_end_of_it():
    """The protocol carries no run id and HA does not serialise runs, so a
    local-only cancel leaves two runs on one connection and the old one's tail
    events kill the question's turn."""
    assert _v(run_started=True).abort_ha is True


def test_a_run_that_never_started_is_not_aborted():
    """Nothing to abort, and VoiceAssistantRequest(start=False) against a run
    that does not exist is a message HA has to interpret."""
    assert _v(run_started=False).abort_ha is False


def test_a_finished_run_is_left_alone():
    """The response was delivered and only our speaker is still busy — the
    case abort_ha_run was split out for. A second start=False here would race
    the question's own pipeline, which is the failure the whole mechanism
    exists to prevent."""
    assert _v(run_finished=True).supersede is False


def test_a_turn_past_intent_end_is_left_alone():
    """#506 deadlocks before INTENT_END. Past it the turn is progressing, and
    superseding would replace a turn that was about to answer."""
    assert _v(intent_ended=True).supersede is False


def test_an_already_cancelled_turn_is_left_alone():
    """A barge, a button or a mute got here first. cancel_turn keeps the FIRST
    reason, so re-deciding would relabel someone else's event."""
    assert _v(already_cancelled=True).supersede is False


def test_an_announcement_outside_a_turn_supersedes_nothing():
    """The ordinary case: the setup wizard, a standalone push. No turn holds
    the voice lock."""
    v = _v(turn_active=False)
    assert v.supersede is False
    assert v.reason == "" and v.abort_ha is False


def test_a_settled_turn_supersedes_nothing_whatever_else_is_true():
    """One settled signal is enough. Guards against a future field being added
    to the call and quietly re-opening this."""
    for settled in ({"run_finished": True}, {"intent_ended": True},
                    {"already_cancelled": True}, {"turn_active": False}):
        assert _v(**settled).supersede is False, settled
