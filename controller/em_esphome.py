"""
em_esphome.py — ESPHome native API integration
================================================

Implements the controller's outward-facing ESPHome satellite interface
(ESPHOME_SPEC.md §2) — the controller's only voice backend:

  - One asyncio TCP listener per device, on ports starting at 16001
    (persisted in the device registry, never reused after deprovisioning).
  - Home Assistant's built-in ESPHome integration dials in to each port
    and drives voice turns through Assist exactly as it would for real
    ESPHome-flashed hardware.

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
                        em_controller.main().

Voice turn flow:
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
import time
import uuid
from typing import TYPE_CHECKING, Optional

import numpy as np

from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo

import em_db as db
import em_api as api

# ── VAD sentinels ──────────────────────────────────────────────────────────────
# Queue items marking end-of-speech in mic_queue/voice_queue, in place of
# audio bytes. Strings, so they can never collide with a bytes payload.
# Defined here (not em_controller) because the import direction is
# em_controller → em_esphome.
#
# B5 fix (2026-07-07): previously a single None sentinel plus a
# device.last_vad_was_timeout side-channel attribute — a second sentinel
# queued before the first was consumed overwrote the flag. The type now
# travels with the queue item itself.
VAD_SENTINEL_END     = "vad_end"
VAD_SENTINEL_TIMEOUT = "vad_no_speech_timeout"

# ── Per-turn trace ─────────────────────────────────────────────────────────────
# Collects timestamps and key metrics through a voice turn and emits a single
# structured [TURN] log line at completion. Makes it possible to see at a glance
# where time is going and whether quality correlates with any specific stage.
#
# All times are offsets in milliseconds from t0 (turn start / wake detected).
# -1 means the stage was not reached in this turn.

import dataclasses

@dataclasses.dataclass
class TurnTrace:
    trigger:          str   = ""      # "wakeword(0.522)" or "button"
    t0:               float = 0.0     # turn start (time.monotonic())
    t_first_frame_ms: int   = -1      # ms from t0 to first real audio frame
    t_vad_end_ms:     int   = -1      # ms from t0 to VAD sentinel received
    audio_frames:     int   = 0       # number of PCM frames sent to HA
    t_stt_ms:         int   = -1      # ms from t0 to STT result received
    stt_text:         str   = ""      # STT transcript
    t_tts_url_ms:     int   = -1      # ms from t0 to TTS URL received
    t_tts_fetched_ms: int   = -1      # ms from t0 to TTS audio fetched+decoded
    tts_bytes:        int   = 0       # decoded PCM bytes
    t_playback_ms:    int   = -1      # ms from t0 to playback started
    t_complete_ms:    int   = -1      # ms from t0 to turn complete
    outcome:          str   = ""      # "ok", "no_speech", "cancelled", "tts_error", "timeout"

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def mark(self, attr: str) -> None:
        """Record current elapsed time into the named timestamp field."""
        setattr(self, attr, self.elapsed_ms())

    def emit(self) -> None:
        """Emit a single structured [TURN] log line."""
        def fmt(v: int) -> str:
            return f"+{v}ms" if v >= 0 else "—"

        audio_ms = self.audio_frames * 80  # each frame is 80ms at 16kHz
        log.info(
            f"[TURN] trigger={self.trigger} outcome={self.outcome} "
            f"total={fmt(self.t_complete_ms)} "
            f"first_frame={fmt(self.t_first_frame_ms)} "
            f"vad_end={fmt(self.t_vad_end_ms)} audio={self.audio_frames}frames/{audio_ms}ms "
            f"stt={fmt(self.t_stt_ms)} text={self.stt_text!r} "
            f"tts_url={fmt(self.t_tts_url_ms)} "
            f"tts_fetch={fmt(self.t_tts_fetched_ms)} tts_bytes={self.tts_bytes} "
            f"playback={fmt(self.t_playback_ms)}"
        )

# Frames to discard from voice_queue at the start of each turn.
# The stream no longer stops at wake, so voice_queue contains the tail of
# "...Jarvis" before the command arrives. N×80ms of bleed removed upfront.
# 3 = 240ms. Lower if first command word gets clipped; raise if wake-word
# tail bleeds into transcripts.
VOICE_PREROLL_DISCARD = 3
from esphome.satellite_server import SatelliteServerProtocol, serve, _HANDLED
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
# Defaults to the real controller version (version.py) so HA's device page
# shows the same thing as the dashboard header; env var kept as an override.
from version import VERSION as _CONTROLLER_VERSION
ESPHOME_PROJECT_VERSION = os.environ.get(
    "ESPHOME_PROJECT_VERSION", _CONTROLLER_VERSION
)

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
        self._trace:            "TurnTrace | None" = None
        # Set on VOICE_ASSISTANT_INTENT_END — the reliable "STT + intent
        # resolution have genuinely completed" marker. Used to distinguish a
        # real terminal RUN_END from a premature/duplicate one that HA can
        # send before the turn has actually progressed (observed in practice —
        # see _handle_voice_event's RUN_END branch).
        self._intent_ended      = False
        # Set on VOICE_ASSISTANT_INTENT_END when HA requests conversation
        # continuation (continue_conversation == "1"). Read by trigger_voice_turn
        # after run_esphome_voice_turn returns to decide whether to re-trigger
        # immediately rather than returning to OWW idle.
        self._continue_conversation = False
        # Set by _stream_mic_audio when the device's local no-speech timeout
        # fired (VAD_NO_SPEECH_TIMEOUT_TYPE) rather than a normal VAD end.
        # Checked by run_esphome_voice_turn to skip the HA round-trip and
        # close the turn quietly instead of waiting on a TTS response that
        # was never going to come.
        self._no_speech_timeout = False
        # C1 fix (2026-07-05 review): set on STT_VAD_END and on ERROR —
        # tells _stream_mic_audio that HA has already ended its side of the
        # turn, so continuing to feed it audio is pointless. Without this,
        # in a noisy room the device's own RMS gate (900ms below threshold)
        # may never close, and _stream_mic_audio sits parked forever after
        # HA has already produced (or errored out on) a result. Cleared at
        # the start of every turn alongside the other per-turn flags.
        self._ha_vad_end = asyncio.Event()

        # Set on STT_VAD_START — HA's VAD heard speech begin. Used by
        # _stream_mic_audio to disarm the no-speech timeout when the
        # controller's own SNR-relative check misses quiet speech. Cleared
        # at the start of every turn alongside _ha_vad_end.
        self._ha_vad_start = asyncio.Event()

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
            yield _HANDLED
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
            yield _HANDLED
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
            yield _HANDLED
            return

        if isinstance(msg, api_pb2.VoiceAssistantEventResponse):
            self._handle_voice_event(msg)
            yield _HANDLED
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

        if event_type == ET.VOICE_ASSISTANT_STT_VAD_START:
            # HA's model-driven VAD heard speech begin — authoritative
            # counterpart to the controller's SNR-relative check in
            # _stream_mic_audio (covers quiet speech in a noisy room that
            # misses the 3×-floor test there).
            self._ha_vad_start.set()

        elif event_type == ET.VOICE_ASSISTANT_STT_VAD_END:
            # Speech ended — HA is now processing (STT → intent → TTS).
            # This is the "thinking" boundary: device detected VAD end,
            # HA now owns the processing pipeline.
            # C1 fix: also tell _stream_mic_audio to stop feeding HA. HA's
            # VAD is the endpointing authority for the turn from here on —
            # the device's own RMS gate becomes advisory (see review §3.1).
            self._ha_vad_end.set()
            if self._on_thinking and not self._turn_cancelled:
                asyncio.create_task(self._on_thinking())

        elif event_type == ET.VOICE_ASSISTANT_STT_END:
            text = data.get("text", "")
            log.info(f"[{self._log_name}] STT result: {text!r}")
            if self._trace:
                self._trace.stt_text = text
                self._trace.t_stt_ms = self._trace.elapsed_ms()
            if self._on_stt_end and not self._turn_cancelled:
                asyncio.create_task(self._on_stt_end(text))

        elif event_type == ET.VOICE_ASSISTANT_INTENT_END:
            # Reliable "STT + intent resolution genuinely finished" marker —
            # always arrives after STT_END, always before TTS_START/RUN_END
            # in a normal turn. Used to tell a real terminal RUN_END apart
            # from a premature/duplicate one (see RUN_END branch below).
            self._intent_ended = True
            if data.get("continue_conversation") == "1":
                self._continue_conversation = True
                log.debug(f"[{self._log_name}] HA requested conversation continuation")

        elif event_type == ET.VOICE_ASSISTANT_TTS_START:
            log.info(f"[{self._log_name}] TTS starting")

        elif event_type == ET.VOICE_ASSISTANT_TTS_END:
            # TTS URL arrives here in some pipeline configurations.
            url = data.get("url", "")
            if url:
                log.info(f"[{self._log_name}] TTS URL: {url}")
            if self._trace:
                self._trace.t_tts_url_ms = self._trace.elapsed_ms()
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
            # C1c fix: if the HA pipeline dies mid-STT, _stream_mic_audio may
            # still be running (waiting on the device's own VAD-end sentinel).
            # Unblock it too, not just the TTS waiter — otherwise the mic
            # stream stays parked until the device's own gate closes.
            self._ha_vad_end.set()
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
        trace: "TurnTrace | None" = None,
        preroll_discard: int = VOICE_PREROLL_DISCARD,
    ) -> None:
        """
        Execute one voice turn over the live HA connection.

        Called by trigger_voice_turn() in the controller, with device.voice_lock
        already held. Streams mic audio to HA, waits for TTS response, and
        hands PCM to post_turn_play() for device-side playback.

        on_thinking() is called when STT_VAD_END arrives — the LED/state
        transition point into the thinking phase.
        post_turn_play(pcm_bytes) wraps _run_post_turn_playback() —
        EQ, resample, stream_speaker, acoustic-feedback drain.

        preroll_discard: number of ~80ms voice_queue frames to drop before
        streaming to HA. Wake-word turns pass VOICE_PREROLL_DISCARD (removes
        the "...Jarvis" tail); button and continuation turns pass 0 — they
        have no wake-word bleed to remove, and discarding real command audio
        just re-introduces the onset-clipping bug P0-1 fixed on the wake path
        (see review C3). Returns nothing; the caller reads
        self._continue_conversation after this returns to decide whether to
        re-trigger — see trigger_voice_turn().
        """
        if not self._transport or self._transport.is_closing():
            log.warning(f"[{self._log_name}] No active HA connection — cannot start voice turn")
            return

        self._turn_active           = True
        self._turn_cancelled        = False
        self._tts_event.clear()
        self._tts_audio_url         = None
        self._tts_audio_data        = None
        self._intent_ended          = False
        self._continue_conversation = False
        self._no_speech_timeout     = False
        self._ha_vad_end.clear()
        self._ha_vad_start.clear()
        self._on_thinking    = on_thinking
        self._on_announce    = None   # set below after announcement path is confirmed
        self._trace          = trace  # may be None — all trace.x calls guard against this

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
            # C1 fix: hard cap on the whole streaming phase as a belt-and-
            # braces guard, on top of the _ha_vad_end early-exit inside the
            # loop itself. If somehow neither HA's VAD-end event nor the
            # device's own sentinel ever arrives, this bounds the damage
            # instead of hanging the turn (and the spinner) indefinitely.
            try:
                await asyncio.wait_for(
                    self._stream_mic_audio(device, preroll_discard=preroll_discard),
                    timeout=20.0,
                )
            except asyncio.TimeoutError:
                log.warning(
                    f"[{self._log_name}] Mic streaming phase hit the 20s hard "
                    f"cap — forcing end=True to HA and falling through to the "
                    f"TTS wait (HA may already have produced a result)"
                )
                if self._trace:
                    self._trace.t_vad_end_ms = self._trace.elapsed_ms()
                if self._transport and not self._transport.is_closing():
                    self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                if trace: trace.outcome = "stream_timeout"

            if self._turn_cancelled:
                log.info(f"[{self._log_name}] Turn cancelled during mic streaming")
                if trace: trace.outcome = "cancelled"
                return

            if self._no_speech_timeout:
                # Device gave up locally before any speech was detected —
                # _stream_mic_audio already skipped sending end=True to HA,
                # so there's no in-flight HA pipeline to wait on. Close the
                # turn immediately rather than sitting on the 30s TTS wait
                # for a response that was never requested.
                if trace: trace.outcome = "no_speech"
                return

            # ── Wait for TTS response (or RUN_END / error / timeout) ──────
            try:
                await asyncio.wait_for(self._tts_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                log.warning(f"[{self._log_name}] Timeout waiting for TTS response from HA")
                if trace: trace.outcome = "timeout"
                return

            if self._turn_cancelled:
                log.info(f"[{self._log_name}] Turn cancelled while waiting for TTS")
                if trace: trace.outcome = "cancelled"
                return

            if self._tts_audio_url:
                # ── Fetch TTS audio and play it ───────────────────────────
                log.info(f"[{self._log_name}] Fetching TTS audio from {self._tts_audio_url}")
                try:
                    pcm_bytes = await _fetch_tts_audio(self._tts_audio_url)
                except Exception as e:
                    log.error(f"[{self._log_name}] TTS audio fetch failed: {e}")
                    if trace: trace.outcome = "tts_error"
                    return

                if trace:
                    trace.t_tts_fetched_ms = trace.elapsed_ms()
                    trace.tts_bytes = len(pcm_bytes) if pcm_bytes else 0

                if pcm_bytes and not self._turn_cancelled:
                    if trace: trace.t_playback_ms = trace.elapsed_ms()
                    await post_turn_play(pcm_bytes)
                    if trace: trace.outcome = "ok"
            else:
                log.info(f"[{self._log_name}] No TTS audio URL received — turn ended without response")
                if trace: trace.outcome = "no_tts"

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
            self._trace          = None
            self._conversation_id = ""
            if trace:
                trace.t_complete_ms = trace.elapsed_ms()
                if not trace.outcome:
                    trace.outcome = "ok"
                trace.emit()
                # Record for the dashboard's Status-tab observability panel
                # and nudge any open dashboards to refresh.
                turn_record = {
                    "ts":           time.time(),
                    "trigger":      trace.trigger,
                    "outcome":      trace.outcome,
                    "total_ms":     trace.t_complete_ms,
                    "vad_end_ms":   trace.t_vad_end_ms,
                    "stt_ms":       trace.t_stt_ms,
                    "tts_url_ms":   trace.t_tts_url_ms,
                    "tts_fetch_ms": trace.t_tts_fetched_ms,
                    "playback_ms":  trace.t_playback_ms,
                    "audio_ms":     trace.audio_frames * 80,
                    "stt_text":     trace.stt_text,
                }
                device.turn_history.append(turn_record)
                try:
                    await api._push_event({
                        "type":      "turn_complete",
                        "device_id": device.device_id,
                        "turn":      turn_record,
                    })
                except Exception:
                    pass  # dashboard notification is best-effort
            log.info(f"[{self._log_name}] ESPHome voice turn complete")

    async def _stream_mic_audio(self, device, preroll_discard: int = VOICE_PREROLL_DISCARD) -> None:
        """
        Pull PCM frames from device.voice_queue and send them to HA as
        VoiceAssistantAudio messages.

        Exits on any of: HA's own VAD end (self._ha_vad_end — see C1 below),
        a device VAD sentinel (None) received via voice_queue, or
        cancel_event firing. Whichever fires first ends the stream; the
        others are then advisory only (see the _ha_vad_end check below) —
        both paths send end=True idempotently, so a race between them is
        harmless.

        The device sentinel comes in two flavours, carried by the queue
        item itself (VAD_SENTINEL_END / VAD_SENTINEL_TIMEOUT, queued by
        em_controller.py's /data frame parser): a normal VAD end (speech
        was detected, then ended — the ordinary case) or a no-speech
        timeout (the device's local grace period elapsed with no speech
        ever detected — see device/internal/client/data.go noSpeechTimeout).

        The no-speech-timeout case sends an empty VoiceAssistantAudio(end=True)
        to close HA's pipeline cleanly (HA already has an open turn from the
        VoiceAssistantRequest sent at turn start), then sets
        self._no_speech_timeout and returns. run_esphome_voice_turn checks
        this flag and skips the TTS wait entirely rather than waiting up to
        30s for a response to an utterance that was never sent — mirroring
        how a real Alexa device gives up quickly and quietly on "wake word,
        then nothing," rather than treating it the same as a real pipeline
        error.

        C1 fix (2026-07-05 review): previously this loop's only exits were
        cancel_event or the device's own RMS-gate sentinel — in a noisy room
        (TV on, kitchen fan) the device gate can stay open indefinitely
        (RMS never drops below threshold for the required 900ms), so this
        loop would sit here forever shovelling room noise at HA long after
        HA's own model-driven VAD had already ended STT and moved on. Now
        self._ha_vad_end (set on STT_VAD_END or ERROR — see
        _handle_voice_event) is checked every iteration and treated as
        authoritative: HA's endpointing is model-driven and pause-tolerant,
        strictly better than a fixed RMS threshold + fixed silence timer.
        The device sentinel is still handled if it arrives first (quiet-room
        case, likely wins the race there) — either path is a valid, clean
        end to the stream.
        """
        pcm_buf = bytearray()

        # Preroll discard — drop wake-word tail from voice_queue before
        # streaming to HA. Wake turns pass VOICE_PREROLL_DISCARD; button and
        # continuation turns pass 0 (see C3 — they have no wake-word tail to
        # remove, and discarding real audio here just clips the first word).
        preroll_remaining = preroll_discard
        if preroll_remaining > 0:
            log.debug(
                f"[{self._log_name}] Discarding {preroll_remaining} preroll frames "
                f"({preroll_remaining * 80}ms) to skip wake-word tail"
            )

        # Controller-side no-speech timeout (replaces device's 0x05 sentinel,
        # which only fires when lock_mic=True — a condition P0-1 eliminates).
        # 5s matches the device's own noSpeechTimeout and Alexa's behaviour.
        #
        # SNR-relative speech detection (2026-07-06): with the ungated wake
        # stream, frames flow continuously — silence included — so "a frame
        # arrived" no longer means "the user spoke". Disarming on the first
        # frame made this timeout dead code, and accidental wakes sat open
        # until HA's own STT timeout (~10s) plus error cleanup. The timeout
        # now disarms only on the first frame whose RMS clears the device's
        # measured noise floor by 3× (with an absolute lower bound matching
        # the old device VAD default, for the floor-not-yet-warmed case).
        # This is the noise floor as *measurement*: the threshold adapts per
        # room, the audio itself is untouched.
        NO_SPEECH_TIMEOUT = 5.0
        speech_seen = False
        first_real_frame_seen = False
        turn_start = time.monotonic()

        def _is_speech(chunk: bytes) -> bool:
            samples = np.frombuffer(chunk, dtype=np.int16)
            if samples.size == 0:
                return False
            rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
            floor = getattr(device, "noise_floor", 0.0)
            return rms >= max(3.0 * floor, 0.004)

        while True:
            if device.cancel_event.is_set():
                self._turn_cancelled = True
                return

            if self._ha_vad_end.is_set():
                # HA has already ended its side of the turn (STT_VAD_END or
                # ERROR) — nothing further sent here can matter. Send end=True
                # defensively in case the device sentinel never arrives (the
                # noisy-room case this fix targets); if the device sentinel
                # already sent it, a second end=True is harmless.
                log.info(
                    f"[{self._log_name}] HA VAD end — stopping mic streaming "
                    f"(device's own RMS gate may still be open; noise-robust "
                    f"HA endpointing wins the race)"
                )
                if self._trace and self._trace.t_vad_end_ms == -1:
                    self._trace.t_vad_end_ms = self._trace.elapsed_ms()
                if self._transport and not self._transport.is_closing():
                    self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                return

            # No-speech timeout: if nothing above the room's noise floor has
            # arrived within NO_SPEECH_TIMEOUT seconds, treat as accidental
            # wake and close quietly. Once speech is detected, HA's VAD owns
            # end-of-turn.
            # HA's VAD hearing speech also disarms the timeout — covers quiet
            # speech in a noisy room that misses the 3×-floor check below.
            if not speech_seen and self._ha_vad_start.is_set():
                speech_seen = True
                log.debug(f"[{self._log_name}] Speech detected (HA VAD start) — no-speech timeout disarmed")

            if not speech_seen and (time.monotonic() - turn_start) > NO_SPEECH_TIMEOUT:
                log.info(
                    f"[{self._log_name}] No speech within {NO_SPEECH_TIMEOUT}s — "
                    f"closing HA pipeline quietly (controller-side no-speech timeout)"
                )
                self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                self._no_speech_timeout = True
                return

            # Race the queue-get against _ha_vad_end directly rather than a
            # flat 1s poll timeout — this notices HA's VAD end within
            # milliseconds instead of up to 1s late, without a busy loop.
            get_task = asyncio.ensure_future(device.voice_queue.get())
            vad_task = asyncio.ensure_future(self._ha_vad_end.wait())
            try:
                done, pending = await asyncio.wait(
                    [get_task, vad_task],
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Belt-and-braces: if this coroutine itself gets cancelled
                # while parked in the wait above (e.g. the 20s hard-cap
                # asyncio.wait_for in run_esphome_voice_turn firing), the
                # CancelledError propagates straight through asyncio.wait()
                # without touching get_task/vad_task — they're independent
                # tasks, not sub-awaits, so nothing cancels them for us.
                # Cancelling both unconditionally here (a no-op if already
                # done) prevents that from leaking a Queue.get() waiter and
                # an Event.wait() waiter on every turn that hits the cap.
                get_task.cancel()
                vad_task.cancel()

            if get_task not in done:
                # Either _ha_vad_end fired (handled at the top of the loop
                # on the next iteration) or the 1s poll elapsed with nothing
                # — loop back to the top where cancel_event/_ha_vad_end/
                # no-speech-timeout are all checked explicitly.
                continue

            payload = get_task.result()

            # VAD sentinel — a string queue item, never audio bytes. (None
            # accepted defensively: it was the pre-B5 sentinel encoding.)
            if payload is None or isinstance(payload, str):
                if payload == VAD_SENTINEL_TIMEOUT:
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
                # Normal VAD end (device sentinel) — signal HA that speech
                # has finished. If self._ha_vad_end is already set, this is
                # just the device catching up on the same conclusion HA
                # already reached; sending end=True twice is harmless.
                log.info(f"[{self._log_name}] VAD end (device sentinel) — sending audio end to HA")
                if self._trace and self._trace.t_vad_end_ms == -1:
                    self._trace.t_vad_end_ms = self._trace.elapsed_ms()
                self._send_one(api_pb2.VoiceAssistantAudio(data=b"", end=True))
                return

            if preroll_remaining > 0:
                preroll_remaining -= 1
                log.debug(f"[{self._log_name}] Preroll discard: skipped frame ({preroll_remaining} remaining)")
                continue

            if not first_real_frame_seen:
                first_real_frame_seen = True
                log.debug(f"[{self._log_name}] First real audio frame received")
                if self._trace:
                    self._trace.t_first_frame_ms = self._trace.elapsed_ms()

            if not speech_seen and _is_speech(payload):
                speech_seen = True
                log.debug(
                    f"[{self._log_name}] Speech detected (above noise floor "
                    f"{getattr(device, 'noise_floor', 0.0):.4f}) — no-speech timeout disarmed"
                )
                # Lock the beamformer onto the speaker now that they're
                # audibly talking. Matters for continuation turns (the wake
                # turn already locked at detection — the device no-ops a
                # second lock) and after any TTS mic restart, which resets
                # the beam to ch6 omni.
                asyncio.ensure_future(device.beam_lock())

            if self._trace:
                self._trace.audio_frames += 1
            pcm_buf.extend(payload)

            # Send in 320-byte chunks (20ms at 16kHz mono S16_LE) —
            # split small for smoother ESPHome API streaming.
            AUDIO_CHUNK = 320
            while len(pcm_buf) >= AUDIO_CHUNK:
                chunk = bytes(pcm_buf[:AUDIO_CHUNK])
                del pcm_buf[:AUDIO_CHUNK]
                self._send_one(api_pb2.VoiceAssistantAudio(data=chunk))

    def disconnect(self) -> None:
        """
        Close this HA connection. HA's reconnect logic redials within
        seconds and re-runs the full handshake — used to force a re-read
        of VoiceAssistantConfiguration after a wake-word model change.
        """
        if self._transport and not self._transport.is_closing():
            self._transport.close()

    def cancel_turn(self) -> None:
        """
        Cancel an in-flight voice turn — local only, no upstream signal.

        Per ESPHOME_SPEC.md §7.4: the ESPHome protocol has no server-side
        abort mechanism. cancel_turn() stops local state only; any in-flight
        HA pipeline generation is left to complete and its result discarded
        on arrival — the cancel is local-only; nothing reaches into HA's
        in-flight pipeline.
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
    # One retry on fetch failure — observed intermittent tts_proxy fetch
    # failures (outcome=tts_error, tts_bytes=0) where the URL was valid and
    # the very next turn fetched fine. A single 0.5s-backoff retry converts
    # those from a silent dead turn into ~1s of extra latency.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    audio_bytes = await resp.read()
            break
        except Exception as e:
            last_exc = e
            if attempt == 0:
                log.warning(f"_fetch_tts_audio: fetch failed ({e}) — retrying once")
                await asyncio.sleep(0.5)
    else:
        raise last_exc

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
    # C1b fix (2026-07-05 review): the aiohttp fetch above is capped at 10s,
    # but proc.communicate() had no timeout at all — if ffmpeg wedges on
    # malformed/truncated input, this could hang the turn indefinitely with
    # no way out. Cheap insurance: 15s cap, kill the process on timeout.
    try:
        pcm, err = await asyncio.wait_for(
            proc.communicate(input=audio_bytes), timeout=15.0
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffmpeg decode timed out after 15s")
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
        # Detach state up front so a device reconnect during the await below
        # sees _server is None and starts a fresh listener, instead of
        # trusting a listener that close() has already shut down.
        server, self._server = self._server, None
        # Clear active satellite so get_satellite() never returns a dead
        # connection's object after a device bounce (B3) — and close its
        # connection: on Python 3.12+ Server.wait_closed() blocks until all
        # accepted connections finish, not just the listener. With HA still
        # connected, stop() otherwise parks here indefinitely with the port
        # already closed — then when HA finally disconnects (e.g. a restart)
        # there is no listener for it to come back to until the controller
        # itself is restarted. Closing the satellite also makes HA mark the
        # device unavailable immediately, which is the intent of tearing the
        # port down on device disconnect in the first place.
        satellite, self._active_satellite = self._active_satellite, None
        if satellite is not None:
            satellite.close()
        if server:
            server.close()
            await server.wait_closed()
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

    Called from em_controller.main().
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
    trigger_label: str = "unknown",  # "wakeword(0.522)" or "button" for trace
    preroll_discard: int = VOICE_PREROLL_DISCARD,
) -> bool:
    """
    Entry point for OWW/button-triggered voice turns in esphome mode.

    Called from em_controller._run_voice_locked(). Finds the active HA
    connection for this device and delegates to
    EchoMuseSatellite.run_esphome_voice_turn().

    preroll_discard: forwarded to run_esphome_voice_turn/_stream_mic_audio.
    Callers should pass VOICE_PREROLL_DISCARD for wake-word turns and 0 for
    button or continuation turns — see review C3. Defaults to
    VOICE_PREROLL_DISCARD for backward compatibility with any caller that
    doesn't specify it, but em_controller.py's call sites should always pass
    it explicitly so the choice is visible at the call site.

    Returns True if HA requested conversation continuation (continue_conversation
    flag set in INTENT_END) — the controller uses this to re-trigger immediately
    rather than returning to OWW idle. Returns False in all other cases including
    no active HA connection.

    If no active HA connection exists, logs and returns False.
    """
    server = get_server(device.device_id)
    if server is None:
        log.warning(
            f"[{device.device_id}] esphome: no server registered for this device "
            f"— was start_esphome_servers() called?"
        )
        return False

    satellite = server.get_satellite()
    if satellite is None:
        log.warning(
            f"[{device.device_id}] esphome: no active HA connection — "
            f"cannot start voice turn (HA not connected to this device's port)"
        )
        return False

    trace = TurnTrace(trigger=trigger_label, t0=time.monotonic())

    await satellite.run_esphome_voice_turn(
        device=device,
        preroll_discard=preroll_discard,
        on_thinking=on_thinking,
        post_turn_play=post_turn_play,
        trace=trace,
    )

    return satellite._continue_conversation


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


def update_oww_model(device_id: str, model_id: str) -> None:
    """
    Keep HA's wake-word dropdown honest.

    The satellite advertises the device's OWW model in
    VoiceAssistantConfigurationResponse, which HA requests only at connect
    time — so a wake-word change in the dashboard left HA showing the old
    model until a controller restart. On a change: store the new id (used
    by the next satellite instance) and bounce the active HA connection so
    HA redials (within seconds) and re-requests the configuration.
    """
    server = get_server(device_id)
    if server is None or server.oww_model_id == model_id:
        return
    server.oww_model_id = model_id
    satellite = server.get_satellite()
    if satellite is not None:
        log.info(f"[{device_id}] OWW model → {model_id} — bouncing HA "
                 f"connection to refresh the wake word configuration")
        satellite.disconnect()



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
    No-op if the device has no registered server.

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
            # mac is MANDATORY for HA discovery: the ESPHome config flow's
            # zeroconf step aborts (reason "mdns_missing_mac") when the TXT
            # record lacks it — devices advertised without it never produce
            # a discovery card, silently. Real ESPHome firmware advertises
            # bare lowercase hex; HA normalises it (format_mac) and matches
            # it against the mac the satellite reports in DeviceInfo (same
            # _serialno_to_mac derivation, so they agree).
            "mac": _serialno_to_mac(device_id).replace(":", "").lower(),
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
