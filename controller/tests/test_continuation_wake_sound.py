"""
#1095: a follow-up question plays the wake sound when it reopens the mic.

When HA's agent asks a question, INTENT_END carries continue_conversation and
the turn loop reopens the mic for the answer with no wake word said. The ring
went back to listening and nothing else did, so from across the room there
was no telling whether the Echo was waiting for an answer or done.
"""

import asyncio

import pytest

import _oww_stub

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    yield
    _oww_stub.uninstall()


def _run_turns(monkeypatch, continues, wake_sound=True, capabilities=("wake_cue",),
               private=False):
    """
    Run a wake turn through _run_voice_locked, with HA answering each turn's
    continue_conversation from `continues`, and return the controls the Echo
    was sent, in order (a cue by its name).
    """
    sent = []
    d = em_controller.Device("dev1", "10.0.0.1", list(capabilities), None)
    d.wake_sound = wake_sound
    monkeypatch.setattr(em_controller.Device, "private_listening", property(lambda _d: private))

    async def send_control(msg):
        sent.append(msg.get("cue") or msg["type"])

    answers = iter(continues)

    async def trigger_voice_turn(**_kw):
        return next(answers)

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(d, "send_control", send_control)
    monkeypatch.setattr(em_controller.esphome, "trigger_voice_turn", trigger_voice_turn)
    for name in ("leds_listening", "_leds_turn_end", "_push_device_state"):
        monkeypatch.setattr(em_controller, name, nothing)
    monkeypatch.setattr(em_controller.em_player, "interrupt", nothing)
    monkeypatch.setattr(em_controller.em_player, "resume_interrupted", nothing)

    asyncio.run(em_controller._run_voice_locked(
        d, trigger_label="wakeword(0.990)", is_wakeword=True))
    return sent


# The follow-up reopens the wake stream, or on a private Echo starts a bounded
# turn stream (mic_stop, then mic_start with lock_mic); the cue follows either.
@pytest.mark.parametrize("private", [False, True])
def test_a_follow_up_plays_the_wake_sound_once_the_mic_is_open(monkeypatch, private):
    sent = _run_turns(monkeypatch, [True, False], private=private)
    assert sent.count("wake") == 1
    assert sent.index("mic_start") < sent.index("wake")


def test_each_follow_up_plays_it_again(monkeypatch):
    assert _run_turns(monkeypatch, [True, True, False]).count("wake") == 2


@pytest.mark.parametrize("wake_sound, capabilities", [
    (False, ("wake_cue",)),  # the sound is off on this Echo
    (True, ()),              # its firmware cannot make the sound
])
def test_no_sound_unless_it_is_on_and_the_echo_can_play_it(monkeypatch, wake_sound, capabilities):
    sent = _run_turns(monkeypatch, [True, False], wake_sound, capabilities)
    assert "mic_start" in sent  # the follow-up still reopened the mic
    assert "wake" not in sent


def test_a_turn_ha_does_not_continue_plays_nothing(monkeypatch):
    assert "wake" not in _run_turns(monkeypatch, [False])
