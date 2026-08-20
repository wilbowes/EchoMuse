"""
When a wake-word score during a response counts as a barge-in.

This decision shipped untested and wrong, and the failure was invisible until
responses got long: the playback branch fired on ONE 80ms frame at a bar ten
times below the wake threshold, while scoring the device's own speech. Asking
for a story cut it off mid-sentence twice in a row (2026-08-20).
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import em_barge

BARGE = 0.25
WAKE = 0.5


def playback(score, prev=0.0, barge=BARGE):
    return em_barge.decide(score=score, prev_score=prev, in_playback=True,
                           barge_threshold=barge, wake_threshold=WAKE)


def thinking(score, prev=0.0):
    return em_barge.decide(score=score, prev_score=prev, in_playback=False,
                           barge_threshold=BARGE, wake_threshold=WAKE)


# ── Playback: the bug ─────────────────────────────────────────────────────────

def test_one_loud_frame_during_playback_does_not_fire():
    """
    The whole bug. An isolated transient in the assistant's own narration
    scored 0.091 and 0.184 against a 0.05 bar and cancelled the response.
    """
    assert playback(0.9, prev=0.0).fired is False
    assert playback(0.184, prev=0.02, barge=0.05).fired is False


def test_two_consecutive_frames_fire():
    d = playback(0.4, prev=0.3)
    assert d.fired is True
    assert "two consecutive" in d.note


def test_a_frame_below_the_bar_breaks_the_pair():
    """Consecutive means adjacent — a dip resets the evidence."""
    assert playback(0.4, prev=BARGE - 0.01).fired is False


def test_exactly_at_the_threshold_counts():
    assert playback(BARGE, prev=BARGE).fired is True


def test_the_first_frame_of_a_run_cannot_fire():
    """
    The caller seeds prev_score at 0.0 for a fresh watcher and again after a
    stream discontinuity. A carried-over score would let one frame of a new
    turn fire on evidence from the previous one.
    """
    assert playback(0.99, prev=0.0).fired is False


def test_a_genuine_barge_still_fires_at_realistic_scores():
    """
    Speech over TTS is depressed to ~0.3-0.5 by the echo, which is what the
    0.25 default is set against. Raising the bar must not stop barge-in.
    """
    for s in (0.30, 0.42, 0.55, 0.9):
        assert playback(s, prev=s).fired is True


# ── Thinking: unchanged behaviour ─────────────────────────────────────────────

def test_one_frame_at_the_full_wake_threshold_fires_while_thinking():
    """Nothing is playing, so there is no self-echo to guard against."""
    d = thinking(WAKE)
    assert d.fired is True
    assert "two consecutive" not in d.note


def test_two_low_tier_frames_fire_while_thinking():
    d = thinking(0.25, prev=0.25)
    assert d.fired is True
    assert "low tier" in d.note


def test_one_low_tier_frame_does_not_fire_while_thinking():
    assert thinking(0.25, prev=0.0).fired is False


def test_the_low_tier_has_a_floor():
    """
    A sensitivity setting must not quietly become a self-trigger setting:
    0.4 x a low wake threshold would otherwise reach down into noise.
    """
    assert em_barge.low_tier_for(0.1) == 0.2
    assert em_barge.low_tier_for(0.5) == 0.2
    assert em_barge.low_tier_for(0.9) == pytest.approx(0.36)


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_a_decision_that_did_not_fire_carries_no_reason():
    """So a caller cannot log a justification for something that never fired."""
    assert playback(0.9, prev=0.0).note == ""
    assert thinking(0.0).note == ""
