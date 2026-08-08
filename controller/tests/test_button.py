"""
Action-button gesture policy.

The case this file exists for is `test_hold_fires_while_muted`: a hold bound
to an HA automation stopped working whenever the mic was muted, from the day
holds shipped (v2.10.0) until this fix, because the device dropped every dot
press while muted. Nothing tested it, so nothing said.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_button

HOLD_MS = 750


def decide(held_ms=0, muted=False, turn_active=False, tap_event=False):
    return em_button.decide(
        held_ms=held_ms,
        hold_ms=HOLD_MS,
        muted=muted,
        turn_active=turn_active,
        tap_event=tap_event,
    )


# ── The mute rule ────────────────────────────────────────────────────────────

def test_hold_fires_while_muted():
    """A hold is not speech, so the mute has no opinion about it."""
    assert decide(held_ms=800, muted=True) == em_button.HOLD


def test_hold_fires_while_muted_during_a_turn():
    """Mute cancels the turn on its own; the hold is still a hold."""
    assert decide(held_ms=800, muted=True, turn_active=True) == em_button.HOLD


def test_tap_starts_no_turn_while_muted():
    assert decide(held_ms=10, muted=True) == em_button.BLOCKED


def test_muted_tap_does_not_cancel_either():
    """Nothing about a muted press reaches the voice path."""
    assert decide(held_ms=10, muted=True, turn_active=True) == em_button.BLOCKED


# ── Unmuted behaviour is unchanged ───────────────────────────────────────────

def test_tap_starts_a_turn():
    assert decide(held_ms=10) == em_button.TURN


def test_tap_during_a_turn_cancels_it():
    assert decide(held_ms=10, turn_active=True) == em_button.CANCEL


def test_hold_fires_long():
    assert decide(held_ms=800) == em_button.HOLD


# ── The hold threshold ───────────────────────────────────────────────────────

def test_exactly_the_threshold_is_a_hold():
    assert decide(held_ms=HOLD_MS) == em_button.HOLD


def test_one_ms_under_is_a_tap():
    assert decide(held_ms=HOLD_MS - 1) == em_button.TURN


def test_absent_hold_time_reads_as_a_tap():
    """
    Firmware predating heldMs reports 0. It must degrade to the old
    behaviour — a turn — never be promoted to a gesture the user did not
    make.
    """
    assert decide(held_ms=0) == em_button.TURN
    assert decide(held_ms=0, muted=True) == em_button.BLOCKED


# ── buttonSingleTapEvent ─────────────────────────────────────────────────────

def test_tap_event_replaces_the_turn():
    assert decide(held_ms=10, tap_event=True) == em_button.TAP_EVENT


def test_tap_event_replaces_the_cancel():
    """The trade the setting makes: the button stops cancelling responses."""
    assert decide(held_ms=10, turn_active=True, tap_event=True) == em_button.TAP_EVENT


def test_tap_event_fires_while_muted():
    """Same reason a hold does — an event is not speech."""
    assert decide(held_ms=10, muted=True, tap_event=True) == em_button.TAP_EVENT


def test_hold_still_wins_over_tap_event():
    assert decide(held_ms=800, tap_event=True) == em_button.HOLD


def test_off_by_default_is_the_old_behaviour():
    """The caller ANDs the setting with button_hold capability, so an
    incapable device arrives here as tap_event=False and keeps its turn —
    rather than emitting to an entity HA was never offered."""
    assert decide(held_ms=10, tap_event=False) == em_button.TURN
    assert decide(held_ms=10, turn_active=True, tap_event=False) == em_button.CANCEL
