"""
em_esphome.py — ESPHome native API integration
================================================

Implements the controller's outward-facing ESPHome satellite interface
(ESPHOME_SPEC.md §2). When VOICE_MODE=esphome:

  - One asyncio TCP listener per device, on ports starting at 16001
    (persisted in the device registry, never reused after deprovisioning).
  - Home Assistant's built-in ESPHome integration dials in to each port
    and drives voice turns through Assist exactly as it would for real
    ESPHome-flashed hardware.
  - ClaraCore is not involved. run_voice_turn() is not called.

Architecture:

  EchoMuseSatellite   — SatelliteServerProtocol subclass, one instance per
                        active HA connection. Handles the full message sequence
                        confirmed against real HA Core 2026.6.4 (see session
                        handoff). Sends VoiceAssistantRequest to HA and streams
                        mic audio when the device's wake word fires or a button
                        triggers a turn.

  DeviceESPhomeServer — Owns the asyncio TCP server for one device port.
                        Enforces single-claimant: a second inbound connection
                        gets DisconnectResponse + close immediately. Holds a
                        reference to the current active EchoMuseSatellite
                        instance so the controller can reach it for OWW/button
                        triggers.

  start_esphome_servers() / stop_esphome_servers() — lifecycle, called from
                        em_controller.main() when VOICE_MODE=esphome.

Voice turn flow (esphome mode):
  1. OWW fires (or button press) → em_controller calls
     trigger_voice_turn(device) in this module
  2. trigger_voice_turn() finds the active EchoMuseSatellite for that device
  3. EchoMuseSatellite sends VoiceAssistantRequest(start=True, flags=0) to HA
     (flags=0 = device already detected wake word, skip HA-side wake word detection)
  4. Streams VoiceAssistantAudio chunks from device.voice_queue
  5. Sends VoiceAssistantAudio(end=True) on VAD sentinel
  6. Receives VoiceAssistantEventResponse stream from HA (RUN_START,
     STT_END, TTS_START, etc — see ESPHOME_SPEC.md §4)
  7. VoiceAssistantAnnounceRequest carries TTS audio URL → fetch + play
     via existing device speaker pipeline (_run_post_turn_playback)
  8. Sends VoiceAssistantAnnounceFinished when playback completes

Cancel (esphome mode):
  Local-only per ESPHOME_SPEC.md §7.4 — stop local playback, clear state,
  do NOT signal HA. Any in-flight server-side generation is left to complete
  and its result discarded on arrival (HA connection stays up).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import TYPE_CHECKING, Optional

from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo

import em_db as db
from esphome.satellite_server import SatelliteServerProtocol, serve
from esphome.feature_flags import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    VoiceAssistantFeature,
)
from esphome.vendor import api_pb2

if TYPE_CHECKING:
    pass

log = logging.getLogger("echomuse.esphome")

# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_IP    = os.environ.get("SERVER_IP", "10.10.1.236")
SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")

# ESPHome controller firmware version string reported to HA.
# Matches the EchoMuse controller version — read from the same source
# as the rest of the controller uses.
ESPHOME_PROJECT_VERSION = os.environ.get("ESPHOME_PROJECT_VERSION", "2.0.0")

# ─── Media player entity ─────────────────────────────────────────────────────

# Single media_player entity key — stable per connection.
# Required: HA's ESPHome integration silently ignores devices with zero
# entities (confirmed empirically, see session handoff finding #2).
MEDIA_PLAYER_KEY = 1

MEDIA_PLAYER_FEATURES = int(
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

# Voice assistant feature flags advertised to HA.
# ANNOUNCE is required to trigger VoiceAssistantConfigurationRequest
# (see session handoff finding #3).
VOICE_ASSISTANT_FLAGS = int(
    VoiceAssistantFeature.VOICE_ASSISTANT
    | VoiceAssistantFeature.API_AUDIO
    | VoiceAssistantFeature.ANNOUNCE
)

# VoiceAssistantRequest flags=0 means "device already detected wake word,
# run Assist pipeline from STT onward — do not run HA-side wake word
# detection on the audio stream."
VOICE_REQUEST_FLAGS_WAKE_WORD_DONE = 0


# ─── EchoMuseSatellite ───────────────────────────────────────────────────────

class EchoMuseSatellite(SatelliteServerProtocol):
    """
    One instance per active HA connection for one device.

    Handles the full ESPHome native API message sequence confirmed against
    real HA Core 2026.6.4. Voice turn lifecycle is driven by the controller
    (OWW / button → trigger_voice_turn()) rather than initiated by HA.
    """

    def __init__(
        self,
        device_id: str,
        label: str,
        mac_address: str,
        oww_model_id: str,
        on_disconnected_cb,
        owning_server=None,   # DeviceESPhomeServer — back-reference so the
                              # standalone-announce path can read the live
                              # _standalone_play callback rather than a
                              # point-in-time copy taken at connect (see
                              # _fetch_and_play_announce). Optional/typed
                              # loosely to avoid a circular import; may be
                              # None in tests or the reject-connection path.
    ) -> None:
        super().__init__(
            server_name=f"echomuse-{device_id[-12:].lower()}",
            log_name=f"esphome.{device_id[-8:]}",
        )
        self.device_id      = device_id
        self.label          = label
        self.mac_address    = mac_address
        self.oww_model_id   = oww_model_id
        self._owning_server  = owning_server

        # Set on the base class so connection_lost dispatches back to
        # DeviceESPhomeServer for claimant cleanup.
        self._disconnected_hook = on_disconnected_cb

        # Voice turn state — guarded by the device's voice_lock in the
        # controller; only one turn active at a time.
        self._turn_active       = False
        self._turn_cancelled    = False
        self._tts_audio_url:    Optional[str] = None
        self._tts_audio_data:   Optional[bytes] = None
        self._tts_event         = asyncio.Event()
        self._conversation_id:  str = ""
        # Set on VOICE_ASSISTANT_INTENT_END — the reliable "STT + intent
        # resolution have genuinely completed" marker. Used to distinguish a
        # real terminal RUN_END from a premature/duplicate one that HA can
        # send before the turn has actually progressed (observed in practice —
        # see _handle_voice_event's RUN_END branch).
        self._intent_ended      = False
        # Set by _stream_mic_audio when the device's local no-speech timeout
        # fired (VAD_NO_SPEECH_TIMEOUT_TYPE) rather than a normal VAD end.
        # Checked by run_esphome_voice_turn to skip the HA round-trip and
        # close the turn quietly instead of waiting on a TTS response that
        # was never going to come.
        self._no_speech_timeout = False

        # Callbacks injected by trigger_voice_turn() for each turn —
        # cleared at turn end.
        self._on_tts_received   = None   # async callable(pcm_url_or_bytes)
        self._on_thinking       = None   # async callable()
        self._on_stt_end        = None   # async callable(text: str)
        self._on_announce       = None   # async callable(pcm_bytes) — set only during an active voice turn (run_esphome_voice_turn); None otherwise. The standalone-announce path (setup wizard, push TTS) does NOT use this — it reads self._owning_server._standalone_play live at call time instead, since that value can legitimately change after this satellite was constructed (see _fetch_and_play_announce).

    # ── Message handling ─────────────────────────────────────────────────

    @property
    def _current_volume(self) -> float:
        """Current volume as HA float (0.0–1.0), read from owning server."""
        if self._owning_server is not None:
            return self._owning_server.volume
        return 1.0

    def handle_message(self, msg):
        """
        Handle all messages beyond Hello/Ping/Disconnect/Authentication
        (those are covered by the base class).

        Yields zero or more protobuf response messages.
        """
        if isinstance(msg, api_pb2.DeviceInfoRequest):
            log.debug(f"[{self._log_name}] DeviceInfoRequest from {self.peer}")
            yield api_pb2.DeviceInfoResponse(
                uses_password=False,
                name=self.server_name,
                friendly_name=self.label,
                mac_address=self.mac_address,
                manufacturer="EchoMuse",
                model="Echo Dot Gen 2 (biscuit)",
                # CRITICAL: dot notation required — HA's manager.py does
                # project_name.split(".") unconditionally. No dot → IndexError
                # → device silently never appears in Devices & Services.
                # (session handoff finding #1)
                project_name=f"EchoMuse.{self.label}",
                project_version=ESPHOME_PROJECT_VERSION,
                voice_assistant_feature_flags=VOICE_ASSISTANT_FLAGS,
            )
            return

        if isinstance(msg, api_pb2.ListEntitiesRequest):
            log.debug(f"[{self._log_name}] ListEntitiesRequest from {self.peer}")
            yield api_pb2.ListEntitiesMediaPlayerResponse(
                object_id="media_player",
                key=MEDIA_PLAYER_KEY,
                name=self.label,
                supports_pause=True,
                feature_flags=MEDIA_PLAYER_FEATURES,
            )
            yield api_pb2.ListEntitiesDoneResponse()
            return

        if isinstance(msg, (api_pb2.SubscribeStatesRequest,
                             api_pb2.SubscribeHomeAssistantStatesRequest)):
            log.debug(f"[{self._log_name}] {type(msg).__name__} from {self.peer}")
            yield api_pb2.MediaPlayerStateResponse(
                key=MEDIA_PLAYER_KEY,
                state=MediaPlayerState.IDLE,
                volume=self._current_volume,
                muted=False,
            )
            return

        if isinstance(msg, api_pb2.SubscribeVoiceAssistantRequest):
            # One-way — no response. Confirmed present in real HA 2026.6.4
            # traffic (session handoff finding #4).
            log.debug(
                f"[{self._log_name}] SubscribeVoiceAssistantRequest "
                f"subscribe={msg.subscribe} flags={msg.flags}"
            )
            return

        if isinstance(msg, api_pb2.VoiceAssistantConfigurationRequest):
            log.debug(f"[{self._log_name}] VoiceAssistantConfigurationRequest")
            yield api_pb2.VoiceAssistantConfigurationResponse(
                available_wake_words=[
                    api_pb2.VoiceAssistantWakeWord(
                        id=self.oww_model_id,
                        wake_word=self.oww_model_id.replace("_", " "),
                        trained_languages=["en"],
                    )
                ],
                active_wake_words=[self.oww_model_id],
                max_active_wake_words=1,
            )
            return

        if isinstance(msg, api_pb2.MediaPlayerCommandRequest):
            if msg.has_volume and self._owning_server is not None:
                # HA set an explicit volume — convert float (0.0–1.0) to device
                # integer (0–175) and forward to the physical device.
                level = max(0, min(175, round(msg.volume * 175)))
                log.debug(
                    f"[{self._log_name}] MediaPlayerCommandRequest: "
                    f"volume={msg.volume:.3f} → level={level}"
                )
                send_fn = self._owning_server._send_volume_set
                if send_fn is not None:
                    asyncio.create_task(send_fn(level))
                else:
                    log.warning(f"[{self._log_name}] volume set requested but device not connected")
            elif msg.has_command:
                log.debug(
                    f"[{self._log_name}] MediaPlayerCommandRequest: "
                    f"command={msg.command} (unhandled)"
                )
            yield api_pb2.MediaPlayerStateResponse(
                key=MEDIA_PLAYER_KEY,
                state=MediaPlayerState.IDLE,
                volume=self._current_volume,
                muted=False,
            )
            return

        if isinstance(msg, api_pb2.SubscribeHomeassistantServicesRequest):
            # No response defined; HA proceeds fine without one.
            log.debug(f"[{self._log_name}] SubscribeHomeassistantServicesRequest (no response)")
            return

        if isinstance(msg, api_pb2.VoiceAssistantResponse):
            # HA's ack to our VoiceAssistantRequest. `port` is only meaningful
            # for the UDP audio-return path used by real ESP32 firmware in
            # non-API_AUDIO mode; linux-voice-assistant (confirmed by reading
            # satellite.py directly) never handles this message at all — audio
            # continues over the same TCP connection via VoiceAssistantAudio.
            # Intentional no-op, not a gap. `error` is also unused here: a
            # true HA-side failure surfaces via VOICE_ASSISTANT_ERROR on the
            # event stream instead, which _handle_voice_event already covers.
            log.debug(f"[{self._log_name}] VoiceAssistantResponse port={msg.port} error={msg.error}")
            return

        if isinstance(msg, api_pb2.VoiceAssistantEventResponse):
            self._handle_voice_event(msg)
            return

        if isinstance(msg, api_pb2.VoiceAssistantAnnounceRequest):
            # HA-initiated announcement (setup wizard audio test, or TTS push).
            # HA expects the satellite to transition MediaPlayerState to
            # ANNOUNCING then back to IDLE around the announce, then send
            # AnnounceFinished — the setup wizard checks this state machine
            # to confirm the device can reach HA's audio endpoint.
            # Audio fetch+play runs as a background task; state transitions
            # are sent synchronously so the wizard doesn't time out.
            log.info(f"[{self._log_name}] AnnounceRequest: media_id={msg.media_id!r} text={msg.text!r}")
            asyncio.create_task(self._fetch_and_play_announce(msg.media_id))
            yield api_pb2.MediaPlayerStateResponse(
                key=MEDIA_PLAYER_KEY,
                state=MediaPlayerState.ANNOUNCING,
                volume=self._current_volume,
                muted=False,
            )
            yield api_pb2.VoiceAssistantAnnounceFinished(success=True)
            yield api_pb2.MediaPlayerStateResponse(
                key=MEDIA_PLAYER_KEY,
                state=MediaPlayerState.IDLE,
                volume=self._current_volume,
                muted=False,
            )
            return

        log.debug(
            f"[{self._log_name}] {self.peer}: unhandled {type(msg).__name__}"
        )

    # ── Voice event stream ───────────────────────────────────────────────

    def _handle_voice_event(self, msg: api_pb2.VoiceAssistantEventResponse) -> None:
        """
        Dispatch incoming VoiceAssistantEventResponse messages during a
        voice turn. Event types confirmed from linux-voice-assistant/satellite.py
        and ESPHOME_SPEC.md §4.
        """
        from esphome.vendor.api_pb2 import VoiceAssistantEvent as ET

        event_type = msg.event_type
        data = {item.name: item.value for item in msg.data}

        log.debug(f"[{self._log_name}] VoiceAssistantEvent type={event_type} data={data}")

        if event_type == ET.VOICE_ASSISTANT_STT_VAD_END:
            # Speech ended — HA is now processing (STT → intent → TTS).
            # This is the "thinking" boundary: device detected VAD end,
            # HA now owns the processing pipeline.
            if self._on_thinking and not self._turn_cancelled:
                asyncio.create_task(self._on_thinking())

        elif event_type == ET.VOICE_ASSISTANT_STT_END:
            text = data.get("text", "")
            log.info(f"[{self._log_name}] STT result: {text!r}")
            if self._on_stt_end and not self._turn_cancelled:
                asyncio.create_task(self._on_stt_end(text))

        elif event_type == ET.VOICE_ASSISTANT_INTENT_END:
            # Reliable "STT + intent resolution genuinely finished" marker —
            # always arrives after STT_END, always before TTS_START/RUN_END
            # in a normal turn. Used to tell a real terminal RUN_END apart
            # from a premature/duplicate one (see RUN_END branch below).
            self._intent_ended = True

        elif event_type == ET.VOICE_ASSISTANT_TTS_START:
            log.info(f"[{self._log_name}] TTS starting")

        elif event_type == ET.VOICE_ASSISTANT_TTS_END:
            # TTS URL arrives here in some pipeline configurations.
            url = data.get("url", "")
            if url:
                log.info(f"[{self._log_name}] TTS URL: {url}")
                self._tts_audio_url = url
                self._tts_event.set()

        elif event_type == ET.VOICE_ASSISTANT_RUN_END:
            log.info(f"[{self._log_name}] Pipeline run ended")
            # HA can emit a RUN_END that isn't the turn's real terminal event —
            # confirmed in practice: a RUN_END arriving before STT_START, with
            # the genuine terminal RUN_END following ~5s later after TTS_END.
            # Only treat RUN_END as "nothing more is coming" once the turn has
            # reached INTENT_END (STT + intent resolution genuinely done) or
            # was cancelled locally — otherwise a premature/duplicate RUN_END
            # wins the race against _stream_mic_audio and ends the turn before
            # STT/intent/TTS ever ran. A turn that legitimately ends with no
            # spoken response (e.g. a silent light-toggle intent) still passes
            # through INTENT_END first, so this doesn't add a 30s stall for
            # that case — only a premature RUN_END before INTENT_END is held.
            if self._intent_ended or self._turn_cancelled:
                self._tts_event.set()
            else:
                log.debug(
                    f"[{self._log_name}] Ignoring RUN_END — INTENT_END not yet "
                    f"seen, treating as premature/duplicate rather than turn end"
                )

        elif event_type == ET.VOICE_ASSISTANT_ERROR:
            code = data.get("code", "")
            message = data.get("message", "")
            log.warning(f"[{self._log_name}] Pipeline error: {code} — {message}")
            self._tts_event.set()  # unblock turn waiter

    # ── Announcement handling ────────────────────────────────────────────

    async def _fetch_and_play_announce(self, media_id: str) -> None:
        """
        Background task: fetch TTS audio from HA and play it on the device.

        Fired after VoiceAssistantAnnounceFinished is already sent, so HA's
        setup wizard doesn't time out waiting for the response. Audio playback
        happens asynchronously — if it fails, the wizard has already passed.

        During a voice turn, _on_announce is set by run_esphome_voice_turn()
        and takes priority — it routes audio to the device's speaker pipeline
        as part of the in-progress turn. Outside a turn (setup wizard,
        standalone push TTS), _on_announce is always None by design (it's
        turn-scoped — see the attribute's docstring in __init__), so we read
        self._owning_server._standalone_play directly instead. This has to be
        a live read, not a copy taken at connect time: _standalone_play is
        set by em_controller.py's device_connected() on the physical Echo
        Dot's own connect event, which is independent of and not ordered
        relative to HA's ESPHome TCP connect — copying it once into
        self._on_announce at construction (the original approach) meant the
        callback was frequently still None at that point even though the
        device was really connected, since device_connected() just hadn't
        run yet for this session. Confirmed in practice: this fired as
        "no playback callback set" on freshly-established connections, not
        just stale reconnects, which ruled out a staleness-only explanation.
        """
        if not media_id:
            return
        try:
            pcm_bytes = await _fetch_tts_audio(media_id)
            if not pcm_bytes:
                return

            play_cb = self._on_announce
            if play_cb is None and self._owning_server is not None:
                play_cb = self._owning_server._standalone_play

            if play_cb:
                await play_cb(pcm_bytes)
            else:
                log.info(f"[{self._log_name}] Announce audio fetched ({len(pcm_bytes)}b) — no playback callback set (standalone announce)")
        except Exception as e:
            log.error(f"[{self._log_name}] Announce fetch/play error: {e}")

    # ── Voice turn (outbound) ────────────────────────────────────────────

    async def run_esphome_voice_turn(
        self,
        device,            # em_controller.Device — avoids circular import
        on_thinking,       # async callable()
        post_turn_play,    # async callable(voice_response: bytes)
    ) -> None:
        """
        Execute one voice turn over the live HA connection.

        Called by trigger_voice_turn() in the controller, with device.voice_lock
        already held. Streams mic audio to HA, waits for TTS response, and
        hands PCM to post_turn_play() for device-side playback.

        on_thinking() is called when STT_VAD_END arrives — same LED/state
        transition point as the ClaraCore "THINKING" sentinel.
        post_turn_play(pcm_bytes) mirrors _run_post_turn_playback() from the
        claracore path — EQ, resample, stream_speaker, acoustic-feedback drain.
        """
        if not self._transport or self._transport.is_closing():
            log.warning(f"[{self._log_name}] No active HA connection — cannot start voice turn")
            return

        self._turn_active    = True
        self._turn_cancelled = False
        self._tts_event.clear()
        self._tts_audio_url  = None
        self._tts_audio_data = None
        self._intent_ended   = False
        self._no_speech_timeout = False
        self._on_thinking    = on_thinking
        self._on_announce    = None   # set below after announcement path is confirmed

        self._conversation_id = str(uuid.uuid4())

        log.info(
            f"[{self._log_name}] Starting ESPHome voice turn "
            f"conversation_id={self._conversation_id}"
        )

        try:
            # ── Tell HA to start the Assist pipeline ──────────────────────
            # flags=0 → device detected wake word, skip HA-side wake word step.
            self._send_one(api_pb2.VoiceAssistantRequest(
                start=True,
                conversation_id=self._conversation_id,
                flags=VOICE_REQUEST_FLAGS_WAKE_WORD_DONE,
            ))

            # ── Stream mic audio from device.voice_queue ──────────────────
            await self._stream_mic_audio(device)

            if self._turn_cancelled:
                log.info(f"[{self._log_name}] Turn cancelled during mic streaming")
                return

            if self._no_speech_timeout:
                # Device gave up locally before any speech was detected —
                # _stream_mic_audio already skipped sending end=True to HA,
                # so there's no in-flight HA pipeline to wait on. Close the
                # turn immediately rather than sitting on the 30s TTS wait
                # for a response that was never requested.
                return

            # ── Wait for TTS response (or RUN_END / error / timeout) ──────
            try:
                await asyncio.wait_for(self._tts_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                log.warning(f"[{self._log_name}] Timeout waiting for TTS response from HA")
                return

            if self._turn_cancelled:
                log.info(f"[{self._log_name}] Turn cancelled while waiting for TTS")
                return

            if self._tts_audio_url:
                # ── Fetch TTS audio and play it ───────────────────────────
                log.info(f"[{self._log_name}] Fetching TTS audio from {self._tts_audio_url}")
                try:
                    pcm_bytes = await _fetch_tts_audio(self._tts_audio_url)
                except Exception as e:
                    log.error(f"[{self._log_name}] TTS audio fetch failed: {e}")
                    return

                if pcm_bytes and not self._turn_cancelled:
                    await post_turn_play(pcm_bytes)
            else:
                log.info(f"[{self._log_name}] No TTS audio URL received — turn ended without response")

        finally:
            # Signal HA that the satellite has finished playing TTS and is
            # back at idle. The reference implementation (linux-voice-assistant
            # _tts_finished callback) sends VoiceAssistantAnnounceFinished —
            # not VoiceAssistantRequest(start=False), not MediaPlayerStateResponse.
            # This is what actually transitions HA's Assist satellite panel out
            # of "Responding." RUN_END only signals server-side pipeline completion;
            # the satellite may still be fetching and playing audio at that point.
            if self._transport and not self._transport.is_closing():
                self._send_one(api_pb2.VoiceAssistantAnnounceFinished(success=True))
                self._send_one(api_pb2.MediaPlayerStateResponse(
                    key=MEDIA_PLAYER_KEY,
                    state=MediaPlayerState.IDLE,
                    volume=self._current_volume,
                    muted=False,
                ))
            self._turn_active    = False
            self._on_thinking    = None
            self._on_announce    = None
            self._conversation_id = ""
            log.info(f"[{self._log_name}] ESPHome voice turn complete")

    async def _stream_mic_audio(self, device) -> None:
        """
        Pull PCM frames from device.voice_queue and send them to HA as
        VoiceAssistantAudio messages.

        Exits when a VAD sentinel (None) is received or cancel_event fires.
        The sentinel comes in two flavours, distinguished via
        device.last_vad_was_timeout (set by em_controller.py's /data frame
        parser immediately before queueing): a normal VAD end (speech was
        detected, then ended — the ordinary case) or a no-speech timeout
        (the device's local grace period elapsed with no speech ever
        detected — see device/internal/client/data.go noSpeechTimeout).

        The no-speech-timeout case sends an empty VoiceAssistantAudio(end=True)
        to close HA's pipeline cleanly (HA already has an open turn from the
        VoiceAssistantRequest sent at turn start), then sets
        self._no_speech_timeout and returns. run_esphome_voice_turn checks
        this flag and skips the TTS wait entirely rather than waiting up to
        30s for a response to an utterance that was never sent — mirroring
        how a real Alexa device gives up quickly and quietly on "wake word,
        then nothing," rather than treating it the same as a real pipeline
        error.
        """
        pcm_buf = bytearray()

        while True:
            if device.cancel_event.is_set():
                self._turn_cancelled = True
                return

            try:
                payload = await asyncio.wait_for(device.voice_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            if payload is None:
                if device.last_vad_was_timeout:
                    # No speech was ever detected, but VoiceAssistantRequest
                    # (start=True) was already sent before this fired — HA
                    # has an open pipeline expecting an audio stream. Close
                    # it cleanly with an empty end=True (a real, valid
                    # protocol message — same call used below for a normal
                    # VAD end, just with nothing buffered) rather than
                    # abandoning the stream and leaving HA to notice via its
                    # own inactivity timeout. run_esphome_voice_turn still
                    # skips the TTS wait afterward — see _no_speech_timeout.
                    log.info(f"[{self._log_name}] No speech detected — closing HA pipeline with empty end=True, not waiting for a response")
                    self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                    self._no_speech_timeout = True
                    return
                # Normal VAD end — signal HA that speech has finished
                log.info(f"[{self._log_name}] VAD end — sending audio end to HA")
                self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                return

            pcm_buf.extend(payload)

            # Send in 320-byte chunks (20ms at 16kHz mono S16_LE) —
            # same granularity as the ClaraCore backend's CHUNK_BYTES
            # but split further for smoother ESPHome API streaming.
            AUDIO_CHUNK = 320
            while len(pcm_buf) >= AUDIO_CHUNK:
                chunk = bytes(pcm_buf[:AUDIO_CHUNK])
                del pcm_buf[:AUDIO_CHUNK]
                self._send_one(api_pb2.VoiceAssistantAudio(data=chunk))

    def cancel_turn(self) -> None:
        """
        Cancel an in-flight voice turn — local only, no upstream signal.

        Per ESPHOME_SPEC.md §7.4: the ESPHome protocol has no server-side
        abort mechanism. cancel_turn() stops local state only; any in-flight
        HA pipeline generation is left to complete and its result discarded
        on arrival. This is a real, documented behavioural difference from
        claracore mode (where cancel_event reaches into the live WS exchange).
        """
        if self._turn_active:
            log.info(f"[{self._log_name}] Turn cancelled (local only)")
            self._turn_cancelled = True
            self._tts_event.set()  # unblock any waiting coroutine


# ─── TTS audio fetch ─────────────────────────────────────────────────────────

async def _fetch_tts_audio(url: str) -> bytes:
    """
    Fetch TTS audio from HA and decode to 22050Hz mono S16_LE PCM.

    Uses ffmpeg subprocess — handles MP3, WAV, FLAC, OGG transparently
    regardless of which TTS provider HA is configured with. Output is raw
    PCM at Piper rate (22050Hz mono S16_LE), matching what the existing
    _run_post_turn_playback() / resample_to_stereo_48k() pipeline expects.

    Requires ffmpeg in PATH (installed via Dockerfile apt-get).
    """
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            audio_bytes = await resp.read()

    log.debug(f"_fetch_tts_audio: fetched {len(audio_bytes)}b, decoding via ffmpeg")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "s16le", "-ar", "22050", "-ac", "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm, err = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {err.decode()[:200]}")

    log.debug(f"_fetch_tts_audio: decoded to {len(pcm)}b PCM (22050Hz mono S16_LE)")
    return pcm


# ─── DeviceESPhomeServer ─────────────────────────────────────────────────────

class DeviceESPhomeServer:
    """
    Manages the ESPHome API TCP listener for one device.

    Single-claimant: only one active HA connection at a time. A second
    inbound connection is rejected with DisconnectResponse + close.
    """

    def __init__(self, device_id: str, label: str, mac_address: str, oww_model_id: str, port: int) -> None:
        self.device_id    = device_id
        self.label        = label
        self.mac_address  = mac_address
        self.oww_model_id = oww_model_id
        self.port         = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_satellite: Optional[EchoMuseSatellite] = None
        self._mdns_info: Optional[ServiceInfo] = None
        # Current volume as HA float (0.0–1.0). Seeded from stored config
        # by update_device_volume() when the device connects; updated on
        # every volume_state message from the device. Read by the satellite
        # for MediaPlayerStateResponse rather than hardcoding 1.0.
        self.volume: float = 1.0
        # Injected by device_connected() — async callable(pcm_bytes) for
        # standalone announce playback (setup wizard, push TTS) when no
        # voice turn is active.
        self._standalone_play = None
        # Injected by device_connected() — async callable(level: int) that
        # sends a volume_set control-plane message to the physical device.
        # None when no device is connected.
        self._send_volume_set = None

    def get_satellite(self) -> Optional[EchoMuseSatellite]:
        """Return the active HA connection's satellite instance, or None."""
        return self._active_satellite

    def set_volume(self, volume: float) -> None:
        """Update stored volume (0.0–1.0) from a device volume_state report."""
        self.volume = max(0.0, min(1.0, volume))

    def _protocol_factory(self):
        """
        Called by asyncio for each new inbound TCP connection.

        If a connection is already active, returns a satellite that will
        immediately send DisconnectResponse and close (single-claimant enforcement).
        """
        if self._active_satellite is not None:
            log.warning(
                f"[esphome.{self.device_id[-8:]}] Second connection attempt — "
                f"rejecting (single-claimant)"
            )
            return _RejectProtocol()

        satellite = EchoMuseSatellite(
            device_id=self.device_id,
            label=self.label,
            mac_address=self.mac_address,
            oww_model_id=self.oww_model_id,
            on_disconnected_cb=self._on_satellite_disconnected,
            owning_server=self,
        )
        self._active_satellite = satellite
        log.info(f"[esphome.{self.device_id[-8:]}] HA connected on port {self.port}")
        return satellite

    def _on_satellite_disconnected(self, satellite: EchoMuseSatellite) -> None:
        if self._active_satellite is satellite:
            self._active_satellite = None
            log.info(f"[esphome.{self.device_id[-8:]}] HA disconnected")

    async def start(self, host: str) -> None:
        self._server = await serve(self._protocol_factory, host, self.port)
        log.info(f"[esphome.{self.device_id[-8:]}] Listening on {host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        log.info(f"[esphome.{self.device_id[-8:]}] Server stopped")

    def set_mdns_info(self, info: ServiceInfo) -> None:
        self._mdns_info = info


class _RejectProtocol(SatelliteServerProtocol):
    """
    Minimal protocol that immediately rejects a second inbound connection.

    Sends DisconnectResponse on first message received, then closes.
    This keeps the TCP handshake but refuses to proceed with the ESPHome
    protocol — HA's ESPHome integration will retry later.
    """

    def __init__(self) -> None:
        super().__init__(server_name="reject", log_name="esphome.reject")
        self._rejected = False

    def handle_message(self, msg):
        if not self._rejected:
            self._rejected = True
            log.debug("[esphome.reject] Sending DisconnectResponse to second claimant")
            yield api_pb2.DisconnectResponse()
            self.close()


# ─── Fleet management ─────────────────────────────────────────────────────────

# device_id → DeviceESPhomeServer
_servers: dict[str, DeviceESPhomeServer] = {}
_azc: Optional[AsyncZeroconf] = None


async def start_esphome_servers(
    devices: dict,   # device_id → em_controller.Device
    host: str = "0.0.0.0",
) -> None:
    """
    Start one ESPHome API TCP server per approved device.

    Allocates a port from the DB if not already assigned. Registers each
    device via mDNS (_esphomelib._tcp) for HA auto-discovery.

    Called from em_controller.main() when VOICE_MODE=esphome.
    """
    global _azc

    _azc = AsyncZeroconf()
    loop = asyncio.get_event_loop()

    all_devices = await loop.run_in_executor(None, db.get_all_devices)
    approved = [row for row in all_devices if row["approved"]]

    log.info(f"Starting ESPHome servers for {len(approved)} approved device(s)")

    for row in approved:
        device_id = row["device_id"]
        label     = row["label"] or f"EchoMuse {device_id[-8:]}"
        # Use MAC from device_id (ro.serialno) as a stable identifier.
        # ro.serialno on the biscuit is a 12-char hex string — format it
        # as a MAC-style address for ESPHome's mac_address field.
        mac = _serialno_to_mac(device_id)

        # Get or allocate ESPHome port
        port = await loop.run_in_executor(None, db.get_esphome_port, device_id)
        if port is None:
            port = await loop.run_in_executor(None, db.assign_esphome_port, device_id)
            log.info(f"[{device_id}] Allocated ESPHome port {port}")

        # Get OWW model from device config
        config       = await loop.run_in_executor(None, db.get_device_config, device_id)
        oww_model_id = config.get("owwModel", "hey_jarvis_v0.1")

        server = DeviceESPhomeServer(
            device_id=device_id,
            label=label,
            mac_address=mac,
            oww_model_id=oww_model_id,
            port=port,
        )
        # Don't start the TCP listener yet — port comes up when the
        # physical device connects to the controller (device_connected()).
        # This prevents the setup wizard from running against an offline device.
        _servers[device_id] = server

        # mDNS registration — _esphomelib._tcp, one per device port,
        # same pattern as the controller's own _emcontroller._tcp service.
        mdns_info = _make_device_mdns_info(device_id, label, port)
        try:
            await _azc.async_register_service(mdns_info, allow_name_change=True)
            server.set_mdns_info(mdns_info)
            log.info(
                f"[{device_id}] mDNS registered: "
                f"{_mdns_service_name(device_id)}._esphomelib._tcp → {SERVER_IP}:{port}"
            )
        except Exception as e:
            log.warning(f"[{device_id}] mDNS registration failed: {e}")

    log.info(f"ESPHome servers ready ({len(_servers)} device(s))")


async def stop_esphome_servers() -> None:
    """Stop all ESPHome API servers and deregister mDNS services."""
    global _azc

    for device_id, server in list(_servers.items()):
        if server._mdns_info and _azc:
            try:
                await _azc.async_unregister_service(server._mdns_info)
            except Exception:
                pass
        await server.stop()
    _servers.clear()

    if _azc:
        await _azc.async_close()
        _azc = None

    log.info("ESPHome servers stopped")


def get_server(device_id: str) -> Optional[DeviceESPhomeServer]:
    """Return the DeviceESPhomeServer for a device, or None."""
    return _servers.get(device_id)


# ─── Voice turn trigger ───────────────────────────────────────────────────────

async def trigger_voice_turn(
    device,           # em_controller.Device
    on_thinking,      # async callable() — LED/state transition
    post_turn_play,   # async callable(pcm_bytes: bytes) — playback + drain
) -> None:
    """
    Entry point for OWW/button-triggered voice turns in esphome mode.

    Called from em_controller._run_voice_locked() instead of run_voice_turn()
    when VOICE_MODE=esphome. Finds the active HA connection for this device
    and delegates to EchoMuseSatellite.run_esphome_voice_turn().

    If no active HA connection exists, logs and returns — same behaviour as
    the claracore path when voice_server.py is unreachable.
    """
    server = get_server(device.device_id)
    if server is None:
        log.warning(
            f"[{device.device_id}] esphome: no server registered for this device "
            f"— was start_esphome_servers() called?"
        )
        return

    satellite = server.get_satellite()
    if satellite is None:
        log.warning(
            f"[{device.device_id}] esphome: no active HA connection — "
            f"cannot start voice turn (HA not connected to this device's port)"
        )
        return

    await satellite.run_esphome_voice_turn(
        device=device,
        on_thinking=on_thinking,
        post_turn_play=post_turn_play,
    )


def cancel_voice_turn(device_id: str) -> None:
    """
    Cancel an in-flight ESPHome voice turn for a device.

    Local-only per ESPHOME_SPEC.md §7.4 — does not signal HA.
    Called from em_controller.handle_button_event() when voice_lock is held.
    """
    server = get_server(device_id)
    if server is None:
        return
    satellite = server.get_satellite()
    if satellite is None:
        return
    satellite.cancel_turn()



# ─── Device lifecycle hooks ───────────────────────────────────────────────────

async def device_connected(
    device_id: str,
    host: str = "0.0.0.0",
    standalone_play=None,
    send_volume_set=None,
) -> None:
    """
    Called by em_controller.handle_control() when an Echo Dot connects.

    Brings the ESPHome API TCP listener up for this device so HA can connect
    and the setup wizard can run against a device that's actually present.
    No-op if VOICE_MODE != esphome or the device has no registered server.

    standalone_play: async callable(pcm_bytes) — routed to the device's
    speaker pipeline for standalone announces (setup wizard audio test,
    push TTS) when no voice turn is active. Provided by the controller
    as a closure over the Device object.

    send_volume_set: async callable(level: int) — sends a volume_set
    control-plane message to the physical device. Provided by the controller
    as a closure over the Device object. Used by the satellite to forward
    HA MediaPlayerCommandRequest volume changes down to the device.
    """
    server = _servers.get(device_id)
    if server is None:
        return
    server._standalone_play = standalone_play
    server._send_volume_set = send_volume_set
    if server._server is not None:
        log.debug(f"[esphome.{device_id[-8:]}] device_connected: port {server.port} already listening")
        return
    await server.start(host)
    log.info(f"[esphome.{device_id[-8:]}] ESPHome port {server.port} up (device connected)")


async def device_disconnected(device_id: str) -> None:
    """
    Called by em_controller.handle_control() when an Echo Dot disconnects.

    Tears down the ESPHome API TCP listener so HA sees the device as
    unavailable and won't attempt to run the setup wizard against an
    offline device.
    """
    server = _servers.get(device_id)
    if server is None:
        return
    if server._server is None:
        log.debug(f"[esphome.{device_id[-8:]}] device_disconnected: port already down")
        return
    server._send_volume_set = None
    await server.stop()
    log.info(f"[esphome.{device_id[-8:]}] ESPHome port {server.port} down (device disconnected)")


def update_device_volume(device_id: str, volume: float) -> None:
    """
    Called by em_controller when a volume_state message arrives from the device.

    Updates the server's stored volume and pushes an unsolicited
    MediaPlayerStateResponse to HA so the entity reflects the new value
    immediately — without waiting for HA to poll on its next refresh cycle.
    No-op if no server is registered for this device.
    """
    server = _servers.get(device_id)
    if server is None:
        return
    server.set_volume(volume)
    log.debug(f"[esphome.{device_id[-8:]}] volume updated → {volume:.3f}")
    satellite = server.get_satellite()
    if satellite is not None:
        satellite._send_one(api_pb2.MediaPlayerStateResponse(
            key=MEDIA_PLAYER_KEY,
            state=MediaPlayerState.IDLE,
            volume=volume,
            muted=False,
        ))


# ─── mDNS helpers ────────────────────────────────────────────────────────────

def _mdns_service_name(device_id: str) -> str:
    """Short stable service name for mDNS registration."""
    return f"echomuse-{device_id[-12:].lower()}"


def _make_device_mdns_info(device_id: str, label: str, port: int) -> ServiceInfo:
    svc_name = _mdns_service_name(device_id)
    return ServiceInfo(
        "_esphomelib._tcp.local.",
        f"{svc_name}._esphomelib._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=port,
        properties={
            "version": ESPHOME_PROJECT_VERSION,
            "friendly_name": label,
            "network": "ethwifi",
            "project_name": f"EchoMuse.{label}",
            "project_version": ESPHOME_PROJECT_VERSION,
        },
        server=f"{svc_name}.local.",
    )


def _serialno_to_mac(device_id: str) -> str:
    """
    Derive a stable MAC-format string from a device's ro.serialno.

    ro.serialno on the biscuit is a 12-char uppercase hex string
    (e.g. G0K0XXXXXXXX). We take the last 12 hex chars (or pad/truncate)
    to build a MAC-style address for ESPHome's mac_address field.
    This is cosmetic only — ESPHome's protocol uses it as a stable
    device identifier in HA's device registry.
    """
    # Extract hex chars only, take last 12, pad with zeros if short
    hex_chars = "".join(c for c in device_id if c in "0123456789ABCDEFabcdef")
    hex_chars = hex_chars[-12:].upper().zfill(12)
    return ":".join(hex_chars[i:i+2] for i in range(0, 12, 2))
