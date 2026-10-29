"""
Every request Home Assistant can send the satellite, through the path a real
connection uses.

`_on_packet` is the seam: it decodes the bytes, dispatches, encodes the
response and would write it to the socket. Here the write is replaced, so the
response is captured as the exact (msg_type, payload) that would have gone out
and decoded back with the same registry — the encode/decode half included,
which is the half `esphome/message_registry.py` and the vendored protobufs own.
A wrong field name there is silence on HA's side, not an exception.

Two failure modes this closes, neither visible without a test:

  * a request type missing from `handle_message`'s if-chain falls through to
    its last log line and HA is never answered. A type added to the chain
    without a case here fails `test_every_request_the_satellite_handles_is_covered_here`;
  * `_process_message` catches whatever `handle_message` raises and only logs
    it, so a branch that starts raising is a feature that silently stops
    working. Every case is also exercised through the table.

The satellite is built with a real `DeviceESPhomeServer`, not a stub: the
capabilities, volume, mute and wake-word switch live on it, and they are what
the responses are asserted against.
"""

import ast
import asyncio
from pathlib import Path

import pytest

import em_esphome
import em_player
import em_volume
from em_timers import TIMER_CANCELLED, TIMER_FINISHED, TIMER_STARTED
from esphome import message_registry as registry
from esphome.satellite_server import API_VERSION_MAJOR, API_VERSION_MINOR
from esphome.vendor import api_pb2

CONTROLLER = Path(__file__).resolve().parents[1]
ESPHOME_SRC = CONTROLLER / "em_esphome.py"

DEVICE = "G090LF0000000001"
MAC = "02:EC:00:00:00:01"
MODEL_ID = "hey_jarvis_v0.1"


class ModelInfo:
    """What em_oww_models hands the satellite for the HA picker."""

    name = "Hey Jarvis"
    languages = ("en",)


def satellite(caps=("mic", "speaker", "button_hold", "ambient_light")):
    """A satellite with a real server behind it and the socket writes captured."""
    srv = em_esphome.DeviceESPhomeServer(
        DEVICE, "Test", MAC, MODEL_ID, 16001, ModelInfo())
    srv.set_capabilities(list(caps))
    sat = em_esphome.EchoMuseSatellite(
        device_id=DEVICE,
        label="Test",
        mac_address=MAC,
        oww_model_id=MODEL_ID,
        on_disconnected_cb=lambda *_: None,
        oww_model_info=ModelInfo(),
        owning_server=srv,
    )
    sent: list[tuple[int, bytes]] = []
    sat.send_packet = lambda msg_type, payload: sent.append((msg_type, payload))
    sat.send_packets = lambda packets: sent.extend(packets)
    return sat, srv, sent


def exchange(sat, sent, request) -> list:
    """Send one request the way the connection would, return what came back."""
    sent.clear()
    msg_type, payload = registry.encode(request)
    sat._on_packet(None, msg_type, payload)
    return [registry.decode(t, p) for t, p in sent]


def only(sent) -> object:
    assert len(sent) == 1, f"expected one response, got {[type(m).__name__ for m in sent]}"
    return sent[0]


def kinds(sent) -> list[str]:
    return [type(m).__name__ for m in sent]


# ── The table against the source ────────────────────────────────────────────

CASES = {
    "DeviceInfoRequest": lambda: api_pb2.DeviceInfoRequest(),
    "ListEntitiesRequest": lambda: api_pb2.ListEntitiesRequest(),
    "SubscribeStatesRequest": lambda: api_pb2.SubscribeStatesRequest(),
    "SubscribeHomeAssistantStatesRequest":
        lambda: api_pb2.SubscribeHomeAssistantStatesRequest(),
    "SubscribeVoiceAssistantRequest":
        lambda: api_pb2.SubscribeVoiceAssistantRequest(subscribe=True),
    "VoiceAssistantConfigurationRequest":
        lambda: api_pb2.VoiceAssistantConfigurationRequest(),
    "VoiceAssistantSetConfiguration":
        lambda: api_pb2.VoiceAssistantSetConfiguration(active_wake_words=[MODEL_ID]),
    "MediaPlayerCommandRequest":
        lambda: api_pb2.MediaPlayerCommandRequest(has_volume=True, volume=0.5),
    "SubscribeHomeassistantServicesRequest":
        lambda: api_pb2.SubscribeHomeassistantServicesRequest(),
    "VoiceAssistantResponse": lambda: api_pb2.VoiceAssistantResponse(port=0),
    "VoiceAssistantEventResponse":
        lambda: api_pb2.VoiceAssistantEventResponse(
            event_type=api_pb2.VoiceAssistantEvent.VOICE_ASSISTANT_RUN_START),
    "VoiceAssistantTimerEventResponse":
        lambda: api_pb2.VoiceAssistantTimerEventResponse(
            event_type=TIMER_STARTED, timer_id="t1", name="Tea",
            total_seconds=300, seconds_left=300),
    "VoiceAssistantAnnounceRequest":
        lambda: api_pb2.VoiceAssistantAnnounceRequest(media_id="conn-test"),
}


def handled_request_types() -> set[str]:
    """The api_pb2 classes `EchoMuseSatellite.handle_message` tests for."""
    tree = ast.parse(ESPHOME_SRC.read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "EchoMuseSatellite")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_message")
    found = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"):
            continue
        subject = node.args[1]
        for elt in (subject.elts if isinstance(subject, ast.Tuple) else [subject]):
            if (isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name)
                    and elt.value.id == "api_pb2"):
                found.add(elt.attr)
    return found


def test_every_request_the_satellite_handles_is_covered_here():
    """
    A new branch in the if-chain is a message HA sends and nobody answered
    yet. Without this the test table quietly covers twelve of thirteen.
    """
    missing = handled_request_types() - set(CASES)
    assert not missing, (
        f"handle_message handles {sorted(missing)} but no case here sends it — "
        f"add one to CASES and assert the response"
    )
    stale = set(CASES) - handled_request_types()
    assert not stale, (
        f"CASES sends {sorted(stale)}, which handle_message no longer handles"
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_handled_request_is_answered_without_raising(name, monkeypatch):
    """
    The error path is a log line: `_process_message` swallows whatever the
    branch raises, so a refactor that breaks one type leaves every test green
    and the feature dead.
    """
    monkeypatch.setattr(em_esphome, "_fetch_tts_audio", _bytes_recorder(b""))

    async def scenario():
        sat, srv, sent = satellite()
        srv._set_wake_word = lambda on: None
        srv._standalone_play = _recorder([])
        return exchange(sat, sent, CASES[name]())

    asyncio.run(scenario())


# ── The base class: handshake, keepalive, teardown ──────────────────────────

def test_hello_is_answered_with_the_protocol_version():
    sat, _, sent = satellite()
    reply = only(exchange(sat, sent, api_pb2.HelloRequest(client_info="HA")))
    assert isinstance(reply, api_pb2.HelloResponse)
    assert (reply.api_version_major, reply.api_version_minor) == (
        API_VERSION_MAJOR, API_VERSION_MINOR)
    assert reply.name == sat.server_name


def test_ping_is_answered():
    sat, _, sent = satellite()
    assert isinstance(only(exchange(sat, sent, api_pb2.PingRequest())),
                      api_pb2.PingResponse)


def test_disconnect_is_acknowledged():
    sat, _, sent = satellite()
    assert isinstance(only(exchange(sat, sent, api_pb2.DisconnectRequest())),
                      api_pb2.DisconnectResponse)


def test_an_unexpected_authentication_request_is_acknowledged_not_dropped():
    """We never ask for a password; a client that sends one anyway must not hang."""
    sat, _, sent = satellite()
    reply = only(exchange(sat, sent, api_pb2.AuthenticationRequest(password="")))
    assert isinstance(reply, api_pb2.AuthenticationResponse)
    assert reply.invalid_password is False


def test_an_unknown_message_type_is_ignored():
    """A newer HA may send a type we have no case for; it must not kill the link."""
    sat, _, sent = satellite()
    sat._on_packet(None, 4242, b"")
    assert sent == []


def test_a_payload_that_does_not_decode_closes_instead_of_answering():
    """Half a message is a broken stream: close, so HA reconnects cleanly."""
    sat, _, sent = satellite()
    msg_type, payload = registry.encode(api_pb2.DeviceInfoRequest())
    sat._on_packet(None, msg_type, payload + b"\xff\xff")
    assert sent == []


# ── What HA is told about the device ────────────────────────────────────────

def test_device_info_names_a_project_with_a_dot():
    """
    HA's manager.py does `project_name.split(".")` unguarded and raises
    IndexError on a name without one, which presents as a device that never
    appears in Devices & Services.
    """
    sat, _, sent = satellite()
    info = only(exchange(sat, sent, api_pb2.DeviceInfoRequest()))
    assert "." in info.project_name
    assert info.project_name.startswith("EchoMuse.")
    assert info.friendly_name == "Test Voice Assistant"
    assert info.mac_address == MAC
    assert info.uses_password is False


def test_device_info_offers_announce_then_listen_only_with_a_mic():
    """
    START_CONVERSATION is the bit HA filters targets on for
    assist_satellite.start_conversation. On a device without a mic it must be
    absent, or HA offers an action that listens to nothing.
    """
    with_mic, _, sent_mic = satellite(("mic", "speaker"))
    flags = only(exchange(with_mic, sent_mic,
                          api_pb2.DeviceInfoRequest())).voice_assistant_feature_flags
    assert flags & em_esphome.VOICE_ASSISTANT_CONVERSATION_FLAG

    without, _, sent_without = satellite(("speaker",))
    flags = only(exchange(without, sent_without,
                          api_pb2.DeviceInfoRequest())).voice_assistant_feature_flags
    assert not flags & em_esphome.VOICE_ASSISTANT_CONVERSATION_FLAG


def test_the_entity_list_advertises_only_what_the_firmware_reports():
    """
    An entity that can never fire is worse than a missing one: someone writes
    an automation against it and it silently never runs.
    """
    sat, _, sent = satellite(("mic", "button_hold"))
    entities = exchange(sat, sent, api_pb2.ListEntitiesRequest())
    assert kinds(entities) == [
        "ListEntitiesMediaPlayerResponse",
        "ListEntitiesEventResponse",
        "ListEntitiesBinarySensorResponse",
        "ListEntitiesDoneResponse",
    ]
    assert entities[1].device_class == "button"
    assert entities[2].object_id == "mic_muted"

    bare, _, sent_bare = satellite(())
    assert kinds(exchange(bare, sent_bare, api_pb2.ListEntitiesRequest())) == [
        "ListEntitiesMediaPlayerResponse", "ListEntitiesDoneResponse"]


def test_the_media_player_asks_for_wav_at_the_wire_rate():
    """
    supported_formats is what makes HA run TTS and announcements through its
    ffmpeg proxy already at 48kHz mono S16 — as WAV, so it passes straight
    through with no decoder holding audio back between sentences. It was FLAC,
    copied from Voice PE, which decodes on the device; here that would need a
    decoder we do not have.
    """
    sat, _, sent = satellite()
    player = exchange(sat, sent, api_pb2.ListEntitiesRequest())[0]
    formats = {(f.purpose, f.format, f.sample_rate, f.num_channels, f.sample_bytes)
               for f in player.supported_formats}
    assert formats == {
        (api_pb2.MEDIA_PLAYER_FORMAT_PURPOSE_DEFAULT, "wav",
         em_esphome.WIRE_RATE, 1, 2),
        (api_pb2.MEDIA_PLAYER_FORMAT_PURPOSE_ANNOUNCEMENT, "wav",
         em_esphome.WIRE_RATE, 1, 2),
    }
    assert player.key == em_esphome.MEDIA_PLAYER_KEY


def test_subscribing_reports_the_current_state_at_once():
    """Otherwise HA shows "unknown" until the next thing the device says."""
    sat, srv, sent = satellite(("mic",))
    srv.set_volume(0.5)
    srv.mic_muted = True
    replies = exchange(sat, sent, api_pb2.SubscribeStatesRequest())
    assert kinds(replies) == ["MediaPlayerStateResponse", "BinarySensorStateResponse"]
    assert replies[0].volume == 0.5
    assert replies[1].state is True


# ── The wake word picker ────────────────────────────────────────────────────

def test_the_picker_is_told_our_model_and_what_is_active():
    sat, srv, sent = satellite()
    cfg = only(exchange(sat, sent, api_pb2.VoiceAssistantConfigurationRequest()))
    assert [w.id for w in cfg.available_wake_words] == [MODEL_ID]
    assert cfg.available_wake_words[0].wake_word == "Hey Jarvis"
    assert cfg.active_wake_words == [MODEL_ID]
    assert cfg.max_active_wake_words == 1
    assert srv.wake_word_told is True

    srv.wake_word_enabled = False
    off = only(exchange(sat, sent, api_pb2.VoiceAssistantConfigurationRequest()))
    assert off.active_wake_words == []


def test_choosing_our_model_turns_listening_on_and_choosing_none_turns_it_off():
    sat, srv, sent = satellite()
    applied = []
    srv._set_wake_word = applied.append

    exchange(sat, sent, api_pb2.VoiceAssistantSetConfiguration(
        active_wake_words=[MODEL_ID]))
    exchange(sat, sent, api_pb2.VoiceAssistantSetConfiguration(
        active_wake_words=[]))
    assert applied == [True, False]


def test_another_device_wake_word_is_refused_rather_than_applied():
    """
    Our model is chosen in the EchoMuse dashboard, not in HA's picker. A
    request naming a different id must leave detection alone instead of
    stopping a listener nobody meant to stop.
    """
    sat, srv, sent = satellite()
    applied = []
    srv._set_wake_word = applied.append
    exchange(sat, sent, api_pb2.VoiceAssistantSetConfiguration(
        active_wake_words=["okay_nabu"]))
    assert applied == []


# ── The media player commands ──────────────────────────────────────────────

def test_ha_volume_reaches_the_device_in_its_native_level():
    """
    HA sends 0.0–1.0; the device takes 0–127 with the codec's unity gain as
    the ceiling. A missing conversion is a slider that is a fifth of the way
    up and silent.
    """
    sat, srv, sent = satellite()
    srv.set_volume(0.4)
    levels = []
    srv._send_volume_set = _recorder(levels)

    async def scenario():
        reply = only(exchange(sat, sent, api_pb2.MediaPlayerCommandRequest(
            has_volume=True, volume=0.4)))
        await asyncio.sleep(0)
        return reply

    reply = asyncio.run(scenario())
    assert levels == [(em_volume.ha_volume_to_device(0.4),)]
    assert reply.volume == pytest.approx(0.4, abs=1e-6)


def test_mute_is_a_volume_of_zero_and_unmute_puts_the_level_back():
    sat, srv, sent = satellite()
    srv.set_volume(0.5)
    levels = []
    srv._send_volume_set = _recorder(levels)

    async def scenario():
        exchange(sat, sent, api_pb2.MediaPlayerCommandRequest(
            has_command=True, command=api_pb2.MEDIA_PLAYER_COMMAND_MUTE))
        exchange(sat, sent, api_pb2.MediaPlayerCommandRequest(
            has_command=True, command=api_pb2.MEDIA_PLAYER_COMMAND_UNMUTE))
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert srv.output_mute.muted is False
    assert levels == [(0,), (em_volume.ha_volume_to_device(0.5),)]


def test_pause_play_and_stop_reach_the_player(monkeypatch):
    """Logged as "command ... (unhandled)" once, so nothing paused anything."""
    sat, _, sent = satellite()
    calls = []
    for name in ("pause", "resume", "stop"):
        monkeypatch.setattr(em_player, name, _recorder(calls, name))

    async def scenario():
        for cmd in (api_pb2.MEDIA_PLAYER_COMMAND_PAUSE,
                    api_pb2.MEDIA_PLAYER_COMMAND_PLAY,
                    api_pb2.MEDIA_PLAYER_COMMAND_STOP):
            exchange(sat, sent, api_pb2.MediaPlayerCommandRequest(
                has_command=True, command=cmd))
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls == ["pause", "resume", "stop"]


def test_play_media_is_answered_playing_before_the_decoder_is_up(monkeypatch):
    """
    The optimistic state is the contract: HA's media player would otherwise
    sit on IDLE until the feed pushes, and the UI flickers for every play.
    """
    sat, _, sent = satellite()
    played = []
    monkeypatch.setattr(em_player, "play", _recorder(played))

    async def scenario():
        reply = only(exchange(sat, sent, api_pb2.MediaPlayerCommandRequest(
            has_media_url=True, media_url="http://ha/audio.wav")))
        await asyncio.sleep(0)
        return reply

    reply = asyncio.run(scenario())
    assert reply.state == api_pb2.MediaPlayerState.MEDIA_PLAYER_STATE_PLAYING
    assert played == [(DEVICE, "http://ha/audio.wav")]


# ── Timers and announcements ───────────────────────────────────────────────

def test_only_a_finished_timer_rings():
    """
    STARTED/UPDATED/CANCELLED move the registry; FINISHED is the one that
    starts the alarm. Ringing on a start is an alarm that goes off when the
    timer is set.
    """
    async def scenario(event_type):
        sat, srv, sent = satellite()
        rung = []
        srv._ring_alarm = _recorder(rung, "ring")
        srv._stop_alarm = _recorder(rung, "stop")
        exchange(sat, sent, api_pb2.VoiceAssistantTimerEventResponse(
            event_type=event_type, timer_id="t1", name="Tea",
            total_seconds=300, seconds_left=300))
        await asyncio.sleep(0)
        return rung, srv.timer_ringing

    assert asyncio.run(scenario(TIMER_STARTED)) == ([], False)
    assert asyncio.run(scenario(TIMER_CANCELLED)) == ([], False)
    assert asyncio.run(scenario(TIMER_FINISHED)) == (["ring"], True)


def test_an_announcement_reports_playing_now_and_finished_only_after_audio(monkeypatch):
    """
    AnnounceFinished is HA's completion signal and HA blocks on it, holding
    the satellite in RESPONDING. Answering it when the message arrives returns
    the service call while audio still plays, and two chained announcements
    overlap on the device. The state goes out now (it describes the state we
    are entering); the completion, at the end of playback.
    """
    sat, srv, sent = satellite()
    played = []
    monkeypatch.setattr(em_esphome, "_fetch_tts_audio", _bytes_recorder(b"pcm"))
    srv._standalone_play = _recorder(played)

    async def scenario():
        reply = only(exchange(sat, sent, api_pb2.VoiceAssistantAnnounceRequest(
            media_id="conn-test")))
        assert isinstance(reply, api_pb2.MediaPlayerStateResponse)
        assert reply.state == api_pb2.MediaPlayerState.MEDIA_PLAYER_STATE_PLAYING
        assert sent and not any(isinstance(registry.decode(t, p),
                                            api_pb2.VoiceAssistantAnnounceFinished)
                                for t, p in sent)
        for _ in range(20):
            await asyncio.sleep(0)
        return played

    played = asyncio.run(scenario())
    assert played == [(b"pcm",)]


# ── Helpers ────────────────────────────────────────────────────────────────

def _recorder(sink: list, tag: str | None = None):
    """An async callback that records its arguments, or `tag` when named."""
    async def record(*args):
        sink.append(tag if tag is not None else args)
    return record


def _bytes_recorder(payload: bytes):
    async def fetch(_media_id: str) -> bytes:
        return payload
    return fetch
