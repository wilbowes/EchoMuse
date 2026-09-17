"""
Soft-mute policy (#286) and how it yields to the button mute (#438).

The rules this file pins, in the order they matter:

- The button always wins. A hard-mute TRANSITION in either direction clears
  the soft mute; a re-report of the same hard state (the device sends
  `mute_state` on every reconnect) leaves it alone.
- A soft mute stops the wake stream; clearing it restarts the stream — unless
  the device is hard muted, where the device would refuse `mic_start` anyway.
- Setting the state it is already in is a no-op, so HA re-asserting a switch
  does not bounce the mic stream.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_softmute


# ── The switch from HA ───────────────────────────────────────────────────────

def test_switching_on_stops_the_wake_stream():
    t = em_softmute.on_switch(want=True, soft=False, hard=False)
    assert t.soft is True
    assert t.stop_stream is True
    assert t.start_stream is False
    assert t.changed is True


def test_switching_off_restarts_the_wake_stream():
    t = em_softmute.on_switch(want=False, soft=True, hard=False)
    assert t.soft is False
    assert t.start_stream is True
    assert t.stop_stream is False
    assert t.changed is True


def test_switching_off_while_hard_muted_does_not_start_the_stream():
    """The device refuses every mic_start while muted; do not send one we
    know is refused. The device restarts the stream itself on unmute."""
    t = em_softmute.on_switch(want=False, soft=True, hard=True)
    assert t.soft is False
    assert t.start_stream is False
    assert t.changed is True


def test_switching_on_while_hard_muted_still_records_the_soft_mute():
    """The stream is already down (the device stopped it on mute), so there
    is nothing to stop. The soft mute is still recorded so HA reads back the
    switch it set — the button unmute that follows clears it, as any hard
    transition does."""
    t = em_softmute.on_switch(want=True, soft=False, hard=True)
    assert t.soft is True
    assert t.stop_stream is False
    assert t.changed is True


def test_setting_the_state_already_held_is_a_no_op():
    for want in (True, False):
        t = em_softmute.on_switch(want=want, soft=want, hard=False)
        assert t.soft is want
        assert t.stop_stream is False
        assert t.start_stream is False
        assert t.changed is False


# ── The button wins ──────────────────────────────────────────────────────────

def test_pressing_mute_clears_the_soft_mute():
    t = em_softmute.on_hard_mute(hard_before=False, hard_now=True, soft=True)
    assert t.soft is False
    assert t.changed is True


def test_pressing_unmute_clears_the_soft_mute():
    """A soft mute does not survive the button in either direction: someone
    who pressed unmute expects the wake word to work."""
    t = em_softmute.on_hard_mute(hard_before=True, hard_now=False, soft=True)
    assert t.soft is False
    assert t.changed is True


def test_the_button_never_touches_the_stream():
    """The device stops and restarts the mic stream itself on mute/unmute
    (cmd/server.go); the controller sending its own would race it."""
    for before, now in ((False, True), (True, False)):
        t = em_softmute.on_hard_mute(hard_before=before, hard_now=now, soft=True)
        assert t.stop_stream is False
        assert t.start_stream is False


def test_a_reconnect_re_report_leaves_the_soft_mute_alone():
    """The device sends mute_state on every (re)connect. A Dot that dropped
    off Wi-Fi mid-film must come back still soft muted."""
    for hard in (False, True):
        t = em_softmute.on_hard_mute(hard_before=hard, hard_now=hard, soft=True)
        assert t.soft is True
        assert t.changed is False


def test_a_hard_transition_with_no_soft_mute_changes_nothing():
    t = em_softmute.on_hard_mute(hard_before=False, hard_now=True, soft=False)
    assert t.soft is False
    assert t.changed is False


# ── Whether the wake word may act ────────────────────────────────────────────

def test_wake_is_allowed_only_when_neither_mute_holds():
    assert em_softmute.wake_allowed(hard=False, soft=False) is True
    assert em_softmute.wake_allowed(hard=True, soft=False) is False
    assert em_softmute.wake_allowed(hard=False, soft=True) is False
    assert em_softmute.wake_allowed(hard=True, soft=True) is False
