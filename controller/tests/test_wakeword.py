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


# ── A stored "No wake word" on firmware that cannot honour it (#776) ─────────
#
# decline_off covers a request arriving NOW. A choice already in the database
# was never revisited, so a rollback or a downgrade left the Echo opening a
# session on every wake while Home Assistant read "No wake word" throughout.
# These must be the same rule, and these cases are the only real guard: the
# source-reading tests elsewhere in this file cannot see whether the stored
# value and the reported value are moved together.

def test_a_stored_off_is_given_up_on_firmware_that_would_still_send():
    assert em_wakeword.stored_off_unsupported(
        stored=True, listening_locally=True, device_can=False)


def test_a_stored_off_is_kept_where_the_device_stops_at_the_crossing():
    assert not em_wakeword.stored_off_unsupported(
        stored=True, listening_locally=True, device_can=True)


def test_a_stored_off_is_kept_for_an_echo_streaming_to_the_controller():
    """The controller stops that stream itself, so this Echo IS honouring off.
    Reverting it would be a regression, not a fix."""
    assert not em_wakeword.stored_off_unsupported(
        stored=True, listening_locally=False, device_can=False)


def test_nothing_to_give_up_when_the_wake_word_is_already_on():
    assert not em_wakeword.stored_off_unsupported(
        stored=False, listening_locally=True, device_can=False)


def test_the_stored_rule_is_the_decline_rule_mirrored():
    """One rule, two callers: if these ever disagree, one path leaves an Echo
    claiming silence it is not keeping — which is the bug.

    They are NOT the same predicate and the test should not pretend they are.
    `decline_off` asks whether a request to go off was refused; it says True
    whatever is currently stored, because nothing is stored yet at that point.
    `stored_off_unsupported` asks whether a stored off has to be given up, so
    it needs the stored value too. What must hold is the part they share: for
    an Echo that is privately listening on firmware that cannot stop at the
    crossing, the answer is yes in both, and turning the wake word back on is
    never refused by either."""
    for local, can, would_decline in [
        (True, False, True),    # privately listening, cannot stop at the crossing
        (True, True, False),    # can honour it
        (False, False, False),  # controller-scores, so mic_stop is enough
        (False, True, False),
    ]:
        assert em_wakeword.decline_off(
            want=False, listening_locally=local, device_can=can) is would_decline
        # An existing stored off is given up exactly when a fresh one would be
        # declined — that is the mirror, and it is the whole of it.
        assert em_wakeword.stored_off_unsupported(
            stored=True, listening_locally=local, device_can=can) is would_decline
        # Nothing stored, nothing to give up.
        assert em_wakeword.stored_off_unsupported(
            stored=False, listening_locally=local, device_can=can) is False
        # Turning it back on is never refused.
        assert em_wakeword.decline_off(
            want=True, listening_locally=local, device_can=can) is False


def test_the_stored_rule_runs_where_the_device_reports_listening():
    """On the listen_state report, not at registration: syncListenState is sent
    after the ack, so at connect time listen_reported is still None and a check
    there reads "not private" for a private Echo."""
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_control")

    # Find the branch that handles listen_state, and ask whether the rule is
    # reached inside it. Walking the tree rather than searching text: this
    # function is 1000+ lines and its formatting is not a contract.
    branch = None
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "listen_state" in test and "msg_type" in test:
            branch = node
            break
    assert branch is not None, "no listen_state branch in handle_control"

    assert any("stored_off_unsupported" in ast.unparse(n)
               for n in ast.walk(branch)), (
        "the stored-off rule must be reached from the listen_state branch, "
        "which is where the device says what it is doing with its microphone"
    )
    # The live state and Home Assistant go to on; the database keeps off.
    # The temporary flag is what makes a later "on" request stick.
    dumped = ast.unparse(branch)
    for needed in ("wake_word_temporarily_on", "update_wake_word"):
        assert needed in dumped, (
            f"{needed} is missing — the live state must go on while the stored "
            "value stays off, and the temporary flag is what makes a later 'on' "
            "request stick after an upgrade"
        )
    assert "set_wake_word_enabled" not in dumped, (
        "the stored value must NOT be written here — keeping it off is what "
        "lets the person's choice come back after an upgrade"
    )


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


def test_audio_arriving_with_the_wake_word_off_is_a_stream_to_stop():
    """Off means nothing leaves the Echo. Muted, it sends nothing and owns
    its stream; on, the stream is the wake stream."""
    stray = em_wakeword.stray_stream
    assert stray(mic_muted=False, enabled=False) is True
    assert stray(mic_muted=True, enabled=False) is False
    assert stray(mic_muted=False, enabled=True) is False
    assert stray(mic_muted=True, enabled=True) is False


def test_the_wake_listener_stops_a_stray_stream():
    src = _func("_stream_listen")
    at = src.index("em_wakeword.stray_stream(")
    assert "mic_stop()" in src[at:at + 600]


def test_a_request_for_on_while_temporarily_on_is_stored():
    """
    #776: when the live value was turned on for a firmware that cannot honour
    off, the stored value is still off. A request for "on" here must still be
    stored, or the person's choice comes back as off after the upgrade — they
    would have to set it twice. The live value is already on, so on_request
    reports no change; this writes the stored value and clears the flag.
    """
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    # _set_wake_word is nested inside another function, so search the whole tree.
    set_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_set_wake_word":
            set_fn = node
            break
    assert set_fn is not None, "_set_wake_word not found"

    body = ast.unparse(set_fn)
    assert "wake_word_temporarily_on" in body, (
        "_set_wake_word must check the temporary flag — a request for 'on' "
        "while temporarily on must still be stored, or the person's choice "
        "comes back as off after the upgrade"
    )
    assert "set_wake_word_enabled" in body, (
        "the stored value must be written when the temporary flag is set"
    )
