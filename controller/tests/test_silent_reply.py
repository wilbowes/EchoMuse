"""
A run HA ends with nothing to say is not a failed turn.

"Hey Jarvis, never mind" reaches HA's HassNevermind intent, which returns an
empty reply, and HA's pipeline skips TTS when the reply is empty
(assist_pipeline: "Skip TTS"). The run goes INTENT_END then RUN_END with no
TTS_START. The turn recorded that as `no_tts` and ended on the error cue, so
the advice for stopping a reply after a false wake (#688) worked and then
told the user it had failed.

These drive the real event handler with the messages HA sends.
"""

import _oww_stub

_oww_stub.install()
try:
    import em_controller
    import em_esphome
finally:
    _oww_stub.uninstall()

from _sources import code_only, def_source  # noqa: E402
from esphome.vendor import api_pb2  # noqa: E402
from esphome.vendor.api_pb2 import VoiceAssistantEvent as ET  # noqa: E402


def satellite() -> "em_esphome.EchoMuseSatellite":
    s = em_esphome.EchoMuseSatellite(
        device_id="G090LF0000000001", label="Test", mac_address="02:EC:00:00:00:01",
        oww_model_id="hey_jarvis_v0.1", on_disconnected_cb=lambda *_: None,
        oww_model_info=None)
    s._turn_active = True
    return s


def send(s, event_type, **data) -> None:
    msg = api_pb2.VoiceAssistantEventResponse(event_type=event_type)
    for name, value in data.items():
        msg.data.add(name=name, value=value)
    s._handle_voice_event(msg)


def up_to_intent_end(s) -> None:
    send(s, ET.VOICE_ASSISTANT_RUN_START)
    send(s, ET.VOICE_ASSISTANT_STT_START)
    send(s, ET.VOICE_ASSISTANT_STT_END, text="never mind")
    send(s, ET.VOICE_ASSISTANT_INTENT_START)
    send(s, ET.VOICE_ASSISTANT_INTENT_END)


def test_never_mind_ends_the_turn_without_a_reply():
    s = satellite()
    up_to_intent_end(s)
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert s._tts_event.is_set(), "the turn would wait out the reply timeout"
    assert s._ended_without_reply()


def test_a_reply_that_started_and_never_arrived_is_still_a_failure():
    s = satellite()
    up_to_intent_end(s)
    send(s, ET.VOICE_ASSISTANT_TTS_START, text="Okay")
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._ended_without_reply()


def test_a_pipeline_error_is_still_a_failure():
    s = satellite()
    up_to_intent_end(s)
    send(s, ET.VOICE_ASSISTANT_ERROR, code="tts-failed", message="boom")
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._ended_without_reply()


def test_a_run_ended_before_the_intent_is_not_a_silent_reply():
    s = satellite()
    send(s, ET.VOICE_ASSISTANT_RUN_START)
    send(s, ET.VOICE_ASSISTANT_STT_START)
    send(s, ET.VOICE_ASSISTANT_STT_END, text="ask me something")
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._ended_without_reply()


def test_an_ordinary_reply_is_not_a_silent_reply():
    s = satellite()
    up_to_intent_end(s)
    send(s, ET.VOICE_ASSISTANT_TTS_START, text="It is 9pm")
    send(s, ET.VOICE_ASSISTANT_TTS_END, url="http://ha/tts.wav")
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._ended_without_reply()


def test_the_outcome_is_decided_before_the_no_tts_fallback():
    body = code_only(def_source("em_esphome.run_esphome_voice_turn"))
    silent = body.index("self._ended_without_reply()")
    assert silent < body.index("trace.outcome = 'no_tts'"), \
        "no_tts would claim the turn first"
    assert "trace.outcome = 'silent_reply'" in body


def test_a_silent_reply_ends_on_the_done_cue_not_the_error_cue():
    assert em_controller._OUTCOME_ANIM["silent_reply"] == "done_anim"
    assert em_controller._OUTCOME_ANIM["no_tts"] == "error_anim"


def test_the_done_cue_is_a_brief_solid_hold_in_the_scenes_own_colour():
    import em_scenes
    configs = [{"ledScene": n} for n in ("standard", "airy", "malevolent", "pride")]
    configs.append({"ledScene": "custom", "ledListenColor": "#123456",
                    "ledThinkColor": "#abcdef"})
    for cfg in configs:
        scene = em_scenes.resolve(cfg)
        cue = scene["done_anim"]
        assert cue["pattern"] == "solid" and cue["ttlSec"] == 1
        assert cue["colors"] == scene["ack_anim"]["colors"], (
            f"{cfg['ledScene']}: the done cue must take the scene's colour")
    custom = em_scenes.resolve(configs[-1])["done_anim"]["colors"]
    assert custom == [[0xab, 0xcd, 0xef]]
