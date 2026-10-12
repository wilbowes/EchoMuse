"""
A RUN_END between STT_END and INTENT_END ends the turn (#506).

Home Assistant opens ask_question, announce and start_conversation with
`_cancel_running_pipeline()`. When the turn's own intent is what asked, the
run it cancels is that turn's: RUN_END arrives after STT and before the intent
finished, and the question follows 2ms later. The handler read that RUN_END
as premature and ignored it, so the turn held the voice lock for the 30s
reply wait while the question's listening turn queued behind it.

Measured on a dev Echo, 2026-10-12. Main: wake turn `timeout` at 33.1s,
listening turn `no_speech`, no answer reached the script. With this: wake
turn `superseded` at 3.3s, listening 2ms after the question, `answered`.

These drive the real event handler with the messages HA sends.
"""

import _oww_stub

_oww_stub.install()
try:
    import em_esphome
finally:
    _oww_stub.uninstall()

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


def up_to_stt_end(s) -> None:
    send(s, ET.VOICE_ASSISTANT_RUN_START)
    send(s, ET.VOICE_ASSISTANT_STT_START)
    send(s, ET.VOICE_ASSISTANT_STT_END, text="run the question test")


def test_a_run_ended_after_stt_and_before_the_intent_ends_the_turn():
    s = satellite()
    up_to_stt_end(s)
    send(s, ET.VOICE_ASSISTANT_INTENT_START)
    assert not s._tts_event.is_set()
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert s._tts_event.is_set(), "the turn would wait out the reply timeout"
    assert s._run_finished, "teardown would abort a run HA already ended"
    assert s._ha_ended_early


def test_it_does_not_need_intent_start():
    s = satellite()
    up_to_stt_end(s)
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert s._tts_event.is_set() and s._ha_ended_early


def test_a_run_end_before_stt_is_still_premature():
    """The RUN_END the guard was written for arrives before STT_START, with
    the real one following after TTS_END. It must stay non-terminal."""
    s = satellite()
    send(s, ET.VOICE_ASSISTANT_RUN_START)
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._tts_event.is_set()
    assert not s._run_finished and not s._ha_ended_early
    send(s, ET.VOICE_ASSISTANT_STT_START)
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert not s._tts_event.is_set()


def test_an_ordinary_turn_is_not_marked():
    s = satellite()
    up_to_stt_end(s)
    send(s, ET.VOICE_ASSISTANT_INTENT_START)
    send(s, ET.VOICE_ASSISTANT_INTENT_END)
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert s._run_finished and s._tts_event.is_set()
    assert not s._ha_ended_early


def test_a_run_ha_never_started_is_still_refused_not_superseded():
    s = satellite()
    send(s, ET.VOICE_ASSISTANT_RUN_END)
    assert s._ha_never_started and not s._ha_ended_early


def test_the_turn_records_it_and_does_not_answer_an_announcement_it_did_not_play():
    """Source shape for the two things the turn body does with the flag: its
    own outcome, and no AnnounceFinished, which HA would take as the reply to
    the question it has just sent."""
    from pathlib import Path
    src = Path(em_esphome.__file__).read_text()
    assert 'elif self._ha_ended_early:' in src
    assert 'trace.outcome = "superseded"' in src
    guard = src.index("if not self._ha_ended_early:")
    sent = src.index("VoiceAssistantAnnounceFinished(success=True)", guard)
    assert sent - guard < 120


def test_the_flag_resets_per_turn():
    from pathlib import Path
    src = Path(em_esphome.__file__).read_text()
    assert src.count("self._ha_ended_early") >= 5
    assert len([l for l in src.splitlines()
                if l.strip().startswith("self._ha_ended_early") and l.rstrip().endswith("= False")]) == 2
