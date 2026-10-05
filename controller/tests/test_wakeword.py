"""
HA's wake word picker (#286) and how it stays out of the mic mute button's
way (#438): what a picker write means, that the button never moves the wake
word, and the one stream re-stop an unmute needs.
"""

import ast
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_wakeword


# ── What a picker write asks for ─────────────────────────────────────────────

MODEL = "hey_mycroft_v0.1"


def test_no_wake_word_is_off():
    assert em_wakeword.requested_on([], MODEL) is False


def test_our_model_is_on():
    assert em_wakeword.requested_on([MODEL], MODEL) is True


def test_our_model_from_both_pickers_is_on():
    """HA creates "Wake word" and "Wake word 2" and sends the union of both,
    whatever max_active_wake_words says — so our model can arrive twice."""
    assert em_wakeword.requested_on([MODEL, MODEL], MODEL) is True


def test_our_model_beside_one_we_lack_is_on():
    """Picker 2 set to our model while picker 1 holds something else still
    asks for our wake word."""
    assert em_wakeword.requested_on(["alexa", MODEL], MODEL) is True


def test_only_wake_words_we_lack_is_declined_not_off():
    """Nobody choosing "Alexa" meant "stop listening". HA re-reads straight
    after writing, so declining snaps the picker back to the truth."""
    assert em_wakeword.requested_on(["alexa"], MODEL) is None
    assert em_wakeword.requested_on(["alexa", "okay_nabu"], MODEL) is None


def test_a_custom_model_is_matched_by_its_id_exactly():
    """Custom models are advertised by prediction key (the filename stem), and
    HA sends back the id it was given — never a path, and never a prefix."""
    custom = "kitchen_helper"
    assert em_wakeword.requested_on([custom], custom) is True
    assert em_wakeword.requested_on(["kitchen"], custom) is None
    assert em_wakeword.requested_on(["/data/oww_models/kitchen_helper.onnx"], custom) is None


# ── The choice from HA ───────────────────────────────────────────────────────

def test_turning_the_wake_word_off_stops_the_stream():
    t = em_wakeword.on_request(want=False, enabled=True, mic_muted=False)
    assert t.enabled is False
    assert t.stop_stream is True
    assert t.start_stream is False
    assert t.changed is True


def test_turning_the_wake_word_on_restarts_the_stream():
    t = em_wakeword.on_request(want=True, enabled=False, mic_muted=False)
    assert t.enabled is True
    assert t.start_stream is True
    assert t.stop_stream is False
    assert t.changed is True


def test_turning_it_on_while_muted_does_not_start_the_stream():
    """The device refuses every mic_start while muted; do not send one we
    know is refused. It restarts the stream itself on unmute."""
    t = em_wakeword.on_request(want=True, enabled=False, mic_muted=True)
    assert t.enabled is True
    assert t.start_stream is False
    assert t.changed is True


def test_turning_it_off_while_muted_does_not_stop_the_stream():
    """The stream is already down — the device stopped it on mute. The state
    is still recorded, so HA reads back the choice it made."""
    t = em_wakeword.on_request(want=False, enabled=True, mic_muted=True)
    assert t.enabled is False
    assert t.stop_stream is False
    assert t.changed is True


def test_setting_the_state_already_held_is_a_no_op():
    for want in (True, False):
        t = em_wakeword.on_request(want=want, enabled=want, mic_muted=False)
        assert t.enabled is want
        assert t.stop_stream is False
        assert t.start_stream is False
        assert t.changed is False


# ── The button does not move HA's choice ─────────────────────────────────────

def test_the_button_never_moves_the_wake_word():
    """on_mic_mute returns a bare bool, so no press can carry a new wake word state."""
    for before, now in ((False, True), (True, False), (False, False), (True, True)):
        for enabled in (True, False):
            out = em_wakeword.on_mic_mute(
                was_muted=before, now_muted=now, enabled=enabled,
            )
            assert out is True or out is False
            assert not isinstance(out, em_wakeword.Transition)


def test_pressing_mute_leaves_the_stream_alone():
    """The device stops its own stream on mute; a controller mic_stop racing
    that is how a stream ends up stuck."""
    for enabled in (True, False):
        assert em_wakeword.on_mic_mute(
            was_muted=False, now_muted=True, enabled=enabled,
        ) is False


def test_unmuting_with_the_wake_word_off_stops_the_stream_again():
    """The device restarts its own wake stream on unmute (cmd/server.go).
    With the wake word off those frames are only discarded, so the controller
    takes the stream back down — which re-asserts HA's choice rather than
    changing it."""
    assert em_wakeword.on_mic_mute(
        was_muted=True, now_muted=False, enabled=False,
    ) is True


def test_unmuting_with_the_wake_word_on_leaves_the_stream_up():
    assert em_wakeword.on_mic_mute(
        was_muted=True, now_muted=False, enabled=True,
    ) is False


def test_a_reconnect_re_report_changes_nothing():
    """The device sends mute_state on every (re)connect. A Dot that dropped
    off Wi-Fi must not read as a button press in either direction."""
    for muted in (False, True):
        for enabled in (True, False):
            assert em_wakeword.on_mic_mute(
                was_muted=muted, now_muted=muted, enabled=enabled,
            ) is False


# ── Whether the wake word may act ────────────────────────────────────────────

def test_wake_is_allowed_only_when_listening_and_unmuted():
    assert em_wakeword.wake_allowed(mic_muted=False, enabled=True) is True
    assert em_wakeword.wake_allowed(mic_muted=True, enabled=True) is False
    assert em_wakeword.wake_allowed(mic_muted=False, enabled=False) is False
    assert em_wakeword.wake_allowed(mic_muted=True, enabled=False) is False


# ── Off, for an Echo that listens privately ──────────────────────────────────

def test_off_is_declined_on_firmware_that_would_still_send():
    """An older privately listening Echo opens a session and streams before
    the controller can close it, so "No wake word" would be a false claim."""
    assert em_wakeword.decline_off(want=False, listening_locally=True, device_can=False)


def test_off_is_accepted_where_the_device_stops_at_the_crossing():
    assert not em_wakeword.decline_off(want=False, listening_locally=True, device_can=True)


def test_off_is_accepted_for_an_echo_streaming_to_the_controller():
    """mic_stop is enough when the controller does the listening."""
    assert not em_wakeword.decline_off(want=False, listening_locally=False, device_can=False)


def test_on_is_never_declined():
    assert not em_wakeword.decline_off(want=True, listening_locally=True, device_can=False)


# ── The same on either side of the wake word split ──────────────────────────
#
# "On this Echo" and "On the controller" must behave the same with the wake
# word off (Wil, 2026-10-05). #552 shipped with one gate on the Echo and none
# where the controller scores a barge, and a follow-up path that only worked
# for an Echo listening privately.

CONTROLLER = Path(__file__).resolve().parent.parent


def _func(name: str) -> str:
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    return ast.unparse(next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))


def test_the_rule_does_not_know_where_the_wake_word_is_detected():
    """No parameter to branch on is how the two modes cannot drift."""
    params = set(inspect.signature(em_wakeword.wake_allowed).parameters)
    assert params == {"mic_muted", "enabled"}


def test_the_controller_asks_the_rule_for_wakes_and_for_barges():
    assert "em_wakeword.wake_allowed(" in _func("_stream_listen")
    assert "em_wakeword.wake_allowed(" in _func("_barge_watcher")


def test_the_echo_stops_at_the_crossing_before_anything_can_act_on_it():
    """A session opened or a wake reported is what the controller would turn
    into a wake or a barge, so the Echo's check has to come first."""
    go = (CONTROLLER.parent / "device/cmd/server.go").read_text()
    body = go.split("func onWakeCrossing(")[1].split("\nfunc ")[0]
    off = body.index("WakeWordOn()")
    assert off < body.index("OpenListen(")
    assert off < body.index("SendOwwWake(")


def test_a_follow_up_gets_a_microphone_in_every_combination():
    """Reusing the wake stream is right only when there is one: scored here,
    with the wake word on."""
    needs = em_wakeword.follow_up_needs_turn_stream
    assert needs(private=False, enabled=True) is False
    assert needs(private=False, enabled=False) is True
    assert needs(private=True, enabled=True) is True
    assert needs(private=True, enabled=False) is True


def test_the_follow_up_path_uses_it():
    assert "em_wakeword.follow_up_needs_turn_stream(" in _func("_run_voice_locked")
