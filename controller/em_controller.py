"""
EchoMuse Controller
===================

WebSocket server. Echo Dot devices connect via mDNS discovery.

mDNS advertisement is handled internally — no separate container required.

Architecture:
- Advertise _emcontroller._tcp on SERVER_PORT (zeroconf, host network)
- Devices open THREE connections:
    /control — JSON control plane (buttons, LEDs, mic_start/stop, ping,
                                   register, config, log, pending)
    /data    — binary data plane (mic PCM frames in, speaker PCM frames out)
    /shell   — raw binary stdin/stdout (demand-opened for shell sessions
                                        and OTA binary transfer)
- HTTP API and dashboard SPA served by aiohttp on API_PORT

Device WebSocket protocol:
  /control — Device → Server:
    {"type": "register", "device_id": "G0K0XXXXXXXX", "ip": "...",
     "version": "v2.0.1", "capabilities": [...]}
    {"type": "button", "clickType": 138, "down": false}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "pong"}

  /control — Server → Device:
    {"type": "ack",     "device_id": "..."}
    {"type": "pending"}
    {"type": "config",  "adcDigitalGain": 100, ...}
    {"type": "leds",    "leds": [...]}
    {"type": "mic_start"}
    {"type": "mic_stop"}
    {"type": "ping"}

  /data — Device → Server:
    <binary> [0x01][seq_hi][seq_lo][PCM mono S16_LE 2560 bytes]

  /data — Server → Device:
    <binary> [0x02][PCM stereo S16_LE 48kHz — 8192 bytes per period]
    <binary> [0x03] end of audio stream

  /shell — bidirectional raw binary (demand-opened by device on
           receipt of shell_open control message — not yet implemented
           in this revision; shell connections come inbound from the
           Go binary to the controller's /shell/{device_id} path)
"""

import asyncio
import collections
import json
import logging
import os
import socket
import struct

import numpy as np
from aiohttp import web
from openwakeword.model import Model as OWWModel
from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_db as db
import em_auth as auth
import em_api as api
import em_eq
import em_scenes
import em_esphome as esphome

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("echomuse")

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


def _log_task_exception(task: asyncio.Task) -> None:
    """
    Standard done-callback for fire-and-forget asyncio.create_task() calls.

    Without this, an exception raised inside a task nobody awaits vanishes
    silently — asyncio only surfaces it via a "Task exception was never
    retrieved" warning at garbage-collection time, easy to miss in normal
    logs. Attach via task.add_done_callback(_log_task_exception) at every
    fire-and-forget create_task() call site (see M1 in the 2026-07-05
    review — currently applied to the button-triggered voice turn task).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"Unhandled exception in background task {task.get_name()}: {exc}", exc_info=exc)


# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
API_PORT     = int(os.environ.get("API_PORT", "8768"))
SERVER_IP    = os.environ.get("SERVER_IP", "10.10.1.236")
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")
DB_PATH      = os.environ.get("DB_PATH", "echomuse.db")

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")

# Mic
CHUNK_BYTES          = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
# NOTE: VOICE_PREROLL_DISCARD lives in em_esphome.py (esphome.VOICE_PREROLL_DISCARD)
# — it's used there in _stream_mic_audio, and _run_voice_locked below reads it
# via that single source of truth rather than keeping a second copy here that
# could drift out of sync (a duplicate here was previously dead code — see
# v2.6.3 changelog — resist the temptation to reintroduce it).


# Speaker — must match PcmSpeaker constants in Go
SPEAKER_RATE   = 48000
SPEAKER_PERIOD = 2048
SPEAKER_BYTES  = SPEAKER_PERIOD * 2 * 2   # 8192 bytes/period
PIPER_RATE     = 22050

# LEDs
NUM_LEDS = 12

# Wake word
OWW_MODEL     = os.environ.get("OWW_MODEL", "hey_jarvis")
OWW_THRESHOLD = float(os.environ.get("OWW_THRESHOLD", "0.5"))

# mDNS re-registration interval — keeps IGMP membership alive on the LAN
MDNS_REFRESH_INTERVAL = 120

# Binary frame types
MIC_FRAME_TYPE     = 0x01
VAD_END_TYPE       = 0x04
# Distinct from VAD_END_TYPE — device never detected speech at all within its
# local no-speech grace period (see device/internal/client/data.go
# noSpeechTimeout), as opposed to VAD_END_TYPE which means speech was
# detected and then ended normally. Each frame type queues its matching
# string sentinel (esphome.VAD_SENTINEL_END / VAD_SENTINEL_TIMEOUT) so the
# type travels with the queue item — B5 fix, 2026-07-07; the old None +
# device.last_vad_was_timeout side-channel let a second sentinel overwrite
# the first's flag before it was consumed. OWW/barge-watcher consumers treat
# both flavours identically; esphome's _stream_mic_audio differentiates.
VAD_NO_SPEECH_TIMEOUT_TYPE = 0x05
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3   # [type][seq_hi][seq_lo]

# Volume conversion — device uses integer 0–175, HA expects float 0.0–1.0.
VOLUME_MAX_DEVICE = 175

def _device_level_to_ha(level: int) -> float:
    """Convert device volume (0–175) to HA float (0.0–1.0)."""
    return max(0.0, min(1.0, level / VOLUME_MAX_DEVICE))

def _ha_volume_to_device(volume: float) -> int:
    """Convert HA volume float (0.0–1.0) to device integer (0–175)."""
    return max(0, min(VOLUME_MAX_DEVICE, round(volume * VOLUME_MAX_DEVICE)))

# ─── Device registry ──────────────────────────────────────────────────────────

class Device:
    def __init__(
        self,
        device_id: str,
        ip: str,
        capabilities: list,
        control_ws: WebSocketServerProtocol,
    ):
        self.device_id    = device_id
        self.ip           = ip
        self.capabilities = capabilities
        self.control_ws   = control_ws

        self.data_ws: WebSocketServerProtocol | None = None
        self.voice_lock   = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self.mic_queue:   asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.voice_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.oww_paused   = asyncio.Event()  # set during voice turn

        # Transient state — read by em_api._merge_device()
        self.speaking  = False
        self.muted     = False
        self.listening = False
        self.thinking  = False

        # Volume as HA float (0.0–1.0). Initialised from stored config in
        # handle_control() after config is read; updated on volume_state
        # messages from the device and persisted back to config.
        # Default matches DEFAULT_DEVICE_CONFIG startupVolume=85.
        self.volume: float = _device_level_to_ha(85)

        self.data_ready = asyncio.Event()

        # Tunable at runtime — updated when a config push arrives.
        # wake_word_listener reads this each detection cycle rather than
        # caching a snapshot at startup, so config changes take effect
        # without requiring a device reconnect.
        self.oww_threshold: float = OWW_THRESHOLD
        self.oww_model:     str   = f"{OWW_MODEL}_v0.1"
        # Q1 fix (2026-07-05 review): openwakeword's built-in speexdsp noise
        # suppressor — 16kHz-native, applied controller-side, only to the
        # wake path (cannot affect STT audio since STT never sees it). Like
        # oww_model, a change here requires reconstructing the OWWModel
        # instance — wake_word_listener's reload loop checks this alongside
        # oww_model. Config key: owwSpeexNs. Defaults False (opt-in — needs
        # the speexdsp-ns pip package confirmed installable in the Docker
        # build before enabling fleet-wide; see review Q1 fix sequence).
        self.oww_speex_ns:  bool  = False
        self.eq_bands:      list  = [0.0] * 8
        self.eq_loudness:   bool  = False
        # LED ring scene — render-ready palette/spinner from em_scenes,
        # refreshed on connect and on any config push carrying led* keys.
        self.led_scene:     dict  = em_scenes.resolve({})
        self.stats:         dict | None = None
        # In-flight wifi_scan awaiter (set by the API handler). Change
        # pending/result state lives in api._wifi_states instead — this
        # Device object dies with the connection when the network switches.
        self.wifi_scan_future: asyncio.Future | None = None
        # Q4 fix (2026-07-05 review): dashboard-visible near-miss counter —
        # incremented in wake_word_listener whenever a score exceeds 0.05
        # but doesn't clear device.oww_threshold. Separate field from
        # self.stats deliberately: self.stats is entirely overwritten every
        # ~30s by the device's own hardware-stats report (msg_type=="stats"
        # in handle_control), so anything stashed inside it would get wiped
        # on the next report. This field is controller-owned and persists
        # independently, reset only on device reconnect (see Device.__init__
        # semantics generally — a fresh Device is created per connection).
        self.oww_near_misses: int = 0

        # Per-room noise floor estimate (normalized RMS, 0..1), tracked from
        # the continuous wake stream in wake_word_listener. Measurement only —
        # never applied to the audio (see 2026-07-06 architecture discussion:
        # adaptation as measurement, not signal modification). Consumers:
        # em_esphome._stream_mic_audio's SNR-relative no-speech detection,
        # and diagnostics (near-miss logs). Asymmetric tracker: follows drops
        # quickly, rises slowly, so speech doesn't drag the floor up.
        self.noise_floor: float = 0.0

        # Barge-in (§3.2): wake word interrupts the thinking phase or TTS
        # playback. Controller-side feature — with it enabled the mic keeps
        # streaming through the turn (device AEC subtracts the speaker
        # output during playback) and _barge_watcher scores the stream from
        # STT_VAD_END onward (barge_threshold during playback, the normal
        # wake threshold during thinking); on detection it sets
        # barge_detected + cancel_event (plus speaker_flush or HA pipeline
        # cancel, phase-dependent) and the turn loop re-enters a fresh
        # turn. _barge_model is a dedicated OWW instance (the main wake
        # listener task is blocked awaiting the turn).
        self.barge_in_enabled = False
        self.barge_threshold  = 0.6
        self.barge_detected   = False
        self._barge_model     = None
        self._barge_model_key = None

        # Recent voice-turn traces (dicts derived from TurnTrace at emit
        # time in em_esphome) — powers the Status tab's observability panel.
        # In-memory only; bounded.
        self.turn_history: collections.deque = collections.deque(maxlen=50)

    async def send_control(self, msg: dict):
        try:
            await self.control_ws.send(json.dumps(msg))
        except Exception as e:
            log.warning(f"[{self.device_id}] Control send failed: {e}")

    async def send_data(self, data: bytes):
        if self.data_ws is None:
            log.warning(f"[{self.device_id}] No data connection")
            return
        try:
            await self.data_ws.send(data)
        except Exception as e:
            log.warning(f"[{self.device_id}] Data send failed: {e}")

    async def set_leds(self, leds: list, listening: bool | None = None):
        # The optional listening flag tells the device explicitly that this
        # frame is the listening ring (enables its direction overlay).
        # Pre-scene firmware inferred it from the ring being all-green —
        # that heuristic breaks for every non-green scene, so newer
        # firmware trusts this flag when present and old firmware just
        # ignores the extra key.
        msg = {"type": "leds", "leds": leds}
        if listening is not None:
            msg["listening"] = listening
        await self.send_control(msg)

    async def ping(self):
        await self.send_control({"type": "ping"})

    async def mic_start(self):
        await self.send_control({"type": "mic_start"})

    async def mic_start_turn(self):
        """Start mic for a voice turn — signals device to lock the best directional mic."""
        await self.send_control({"type": "mic_start", "lock_mic": True})

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

    async def beam_lock(self):
        # Lock the beamformer onto the speaker's perimeter mic mid-stream —
        # no stream restart. Device no-ops if already locked or if
        # beamformingEnabled is false in its config.
        await self.send_control({"type": "beam_lock"})

    async def beam_unlock(self):
        await self.send_control({"type": "beam_unlock"})

    async def push_config(self, **kwargs):
        await self.send_control({"type": "config", **kwargs})

    async def stream_speaker(self, pcm: bytes):
        """Stream resampled stereo 48kHz PCM as 0x02 frames, then 0x03 EOS."""
        self.speaking = True
        try:
            offset = 0
            while offset < len(pcm):
                if self.cancel_event.is_set():
                    break
                chunk = pcm[offset:offset + SPEAKER_BYTES]
                if len(chunk) < SPEAKER_BYTES:
                    # Pad the final partial period with silence — without this,
                    # up to one full period (~42ms at 48kHz) of the last word is
                    # silently dropped because the old loop required a full period.
                    chunk = chunk + bytes(SPEAKER_BYTES - len(chunk))
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                offset += SPEAKER_BYTES
        finally:
            self.speaking = False
            # EOS must go out on EVERY exit, including task cancellation
            # (barge-in cancels this task mid-send): the device's barge-in
            # flush discards 0x02 frames until it sees this stream's 0x03 —
            # a stream that ends without one would leave the discard armed
            # and swallow the next turn's audio. shield() lets the send
            # complete even though this task is mid-cancellation; the
            # original CancelledError still propagates after the finally.
            try:
                await asyncio.shield(self.send_data(bytes([SPEAKER_EOS_TYPE])))
            except BaseException:
                pass  # WS gone / re-cancelled — device flush self-heals on reconnect


# The live device registry — keyed by device_id (ro.serialno).
# em_api receives a reference to this dict at startup.
_devices: dict[str, Device] = {}

# Shell session coordination — keyed by device_id.
#
# _shell_pending:   Future resolved with the device ws when handle_shell receives it.
# _shell_dashboard: dashboard WebSocket set by em_api for interactive sessions.
_shell_pending:    dict[str, asyncio.Future] = {}
_shell_dashboard:  dict[str, object]         = {}


def get_device(device_id: str) -> Device | None:
    return _devices.get(device_id)


async def _push_device_state(device: Device) -> None:
    """Push current transient device state to dashboard clients."""
    await api._push_event({
        "type":      "device_update",
        "device_id": device.device_id,
        "state": {
            "connected": True,
            "speaking":  device.speaking,
            "muted":     device.muted,
            "listening": device.listening,
            "thinking":  device.thinking,
        },
    })


# ─── LED helpers ──────────────────────────────────────────────────────────────

def _make_leds(r, g, b):
    return [{"id": i, "r": r, "g": g, "b": b} for i in range(NUM_LEDS)]


async def leds_off(device: Device):
    await device.set_leds(_make_leds(0, 0, 0))


async def leds_listening(device: Device):
    await device.set_leds(device.led_scene["listening"], listening=True)


async def leds_spin_green(device: Device, stop_event: asyncio.Event):
    # Name is historical — the spinner renders whatever the device's scene
    # says (head+trail dot for solid scenes, rotating palette for pride).
    spin_frame = device.led_scene["spin_frame"]
    pos = 0
    try:
        while not stop_event.is_set():
            await device.set_leds(spin_frame(pos))
            pos = (pos + 1) % NUM_LEDS
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        pass
    finally:
        await leds_off(device)


# ─── Audio conversion ─────────────────────────────────────────────────────────

def resample_to_stereo_48k(pcm: bytes, from_rate: int) -> bytes:
    """
    Resample mono S16_LE PCM from from_rate to 48kHz stereo S16_LE.

    Uses linear interpolation via numpy. For a 10s Piper response (~220k samples)
    the old pure-Python loop took ~1-2s of wall time in the asyncio event loop;
    numpy completes it in <5ms, keeping the perceived latency budget intact.
    """
    if len(pcm) < 2:
        return b""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_in  = len(samples)
    n_out = int(n_in * SPEAKER_RATE / from_rate)

    # Source indices for each output sample — fractional positions in input
    src  = np.arange(n_out, dtype=np.float64) * from_rate / SPEAKER_RATE
    lo   = src.astype(np.int32)
    hi   = np.minimum(lo + 1, n_in - 1)
    frac = (src - lo).astype(np.float32)

    resampled = samples[lo] * (1.0 - frac) + samples[hi] * frac
    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)

    # Duplicate mono → stereo (L = R)
    stereo = np.empty(n_out * 2, dtype=np.int16)
    stereo[0::2] = resampled
    stereo[1::2] = resampled
    return stereo.tobytes()


# ─── Voice pipeline ───────────────────────────────────────────────────────────


async def _barge_watcher(device: Device, playback_started: asyncio.Event):
    """
    Wake-word watcher spanning the thinking AND playback phases (barge-in,
    §3.2). Started at STT_VAD_END (on_thinking); before that the user's own
    command is streaming and a wake word in it is just speech.

    With barge-in enabled the mic keeps streaming through the whole turn
    (the device's AEC subtracts its own speaker output during playback) and
    oww_paused routes frames to voice_queue — which nothing else reads
    after STT ends, so this watcher drains and scores it with a dedicated
    openwakeword instance (the main wake listener task is blocked awaiting
    the turn).

    The threshold is phase-dependent because the acoustics are: during
    playback the speaker is ~25dB louder than the person at the mic, so
    speech-over-TTS scores are depressed and barge_threshold sits well
    below the wake threshold (~0.05–0.10). During thinking nothing is
    playing — scores are normal, and using the low barge threshold there
    would fire on random speech — so detection is two-tier: a single frame
    at the normal wake threshold fires immediately, and two CONSECUTIVE
    frames at a low tier (0.4× wake threshold, floored at 0.2) also fire.
    The low tier exists because a genuine barge attempt over the watcher's
    cold-started model can plateau below the wake threshold (observed
    2026-07-12: 0.240/0.242 on consecutive frames vs threshold 0.50 —
    missed, and the unwanted answer played in full), while random speech
    near-misses are isolated single frames — two elevated frames in a row
    is wake-word-shaped evidence.

    On detection: set barge_detected + cancel_event. During playback that
    aborts stream_speaker and the drain sleep, plus a device speaker_flush
    so the interruption is audible immediately (not after ~1.4s of queued
    TTS). During thinking there's no audio to flush — instead the in-flight
    HA pipeline is cancelled (local-only; any late HA result is discarded).
    """
    loop = asyncio.get_event_loop()
    if device._barge_model is None or device._barge_model_key != device.oww_model:
        name = device.oww_model
        log.info(f"[{device.device_id}] Barge-in: loading watcher model {name}")
        device._barge_model = await loop.run_in_executor(
            None, lambda: OWWModel(wakeword_models=[name])
        )
        device._barge_model_key = name
    model = device._barge_model
    model.reset()

    # Drop anything queued before the watcher started (command tail,
    # silence) — only fresh audio should be scored.
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    # Playback phase: bargeInThreshold is used as-is — deliberately NOT
    # floored at the wake threshold. The max() clamp guarded against
    # residual echo waking the device before AEC worked; measured with
    # working AEC (2026-07-08), self-echo peaks at 0.004 converged / 0.055
    # worst-case-unconverged, while real speech over TTS scores 0.118+ —
    # the echo is 25dB louder than the speaker at the mic, so
    # speech-over-TTS scores are inherently depressed and a sub-wake
    # threshold (~0.10) is both safe and necessary. Thinking phase uses the
    # normal wake threshold (see docstring).
    threshold = device.barge_threshold  # refined per-frame by phase below
    prev_score = 0.0  # previous frame's score — two-frame low tier (thinking)
    buf = bytearray()
    # Observability: the watcher used to log only on detection, which made a
    # failed barge-in attempt indistinguishable from "no frames arrived at
    # all" (mic not streaming) or "frames arrived but scored ~0" (AEC residual
    # burying the speech). Track both and always report on exit.
    peak   = 0.0
    frames = 0
    # Frame RMS (0.0–1.0) discriminates the failure modes peak alone can't:
    # rms >> noise floor means echo is reaching the watcher raw (AEC off or
    # ineffective — delay mismatch / clipped-nonlinear echo); rms ≈ floor
    # with a low peak means AEC is eating the user's speech along with the
    # echo (over-suppression / divergence during double-talk).
    rms_sum = 0.0
    rms_max = 0.0
    try:
        while True:
            payload = await device.voice_queue.get()
            if payload is None or isinstance(payload, str):
                buf.clear()
                prev_score = 0.0  # sentinel = stream discontinuity; frames
                # across it aren't consecutive for the two-frame low tier
                continue
            buf.extend(payload)
            while len(buf) >= CHUNK_BYTES:
                frame = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                rms_sum += rms
                rms_max  = max(rms_max, rms)
                prediction = await loop.run_in_executor(None, model.predict, samples)
                score = prediction.get(device._barge_model_key, 0.0)
                frames += 1
                in_playback = playback_started.is_set()
                if in_playback:
                    threshold = device.barge_threshold
                    fired     = score >= threshold
                    fire_note = f"score={score:.3f} >= {threshold:.2f}"
                else:
                    # Two-tier thinking detection (see docstring): full wake
                    # threshold on a single frame, OR two consecutive frames
                    # at the low tier.
                    threshold = device.oww_threshold
                    low_tier  = max(0.2, 0.4 * device.oww_threshold)
                    if score >= threshold:
                        fired     = True
                        fire_note = f"score={score:.3f} >= {threshold:.2f}"
                    elif score >= low_tier and prev_score >= low_tier:
                        fired     = True
                        fire_note = (
                            f"scores {prev_score:.3f}/{score:.3f} — two "
                            f"consecutive frames >= low tier {low_tier:.2f}"
                        )
                    else:
                        fired = False
                prev_score = score
                if score > peak:
                    peak = score
                    if score >= 0.1:
                        log.info(
                            f"[{device.device_id}] Barge watcher: score {score:.3f} "
                            f"(threshold {threshold:.2f})"
                        )
                if fired:
                    phase = "playback" if in_playback else "thinking"
                    log.info(
                        f"[{device.device_id}] Barge-in: wake word during {phase} "
                        f"({fire_note}) — cancelling turn"
                    )
                    db.log_device(
                        device.device_id, "info", "device",
                        f"Barge-in during {phase} (score={score:.3f})"
                    )
                    device.barge_detected = True
                    device.cancel_event.set()
                    if in_playback:
                        await device.send_control({"type": "speaker_flush"})
                    else:
                        # Nothing is playing — abort the in-flight HA
                        # pipeline instead (local-only; a late HA result is
                        # discarded on arrival).
                        esphome.cancel_voice_turn(device.device_id)
                    return
    finally:
        rms_mean = rms_sum / frames if frames else 0.0
        log.info(
            f"[{device.device_id}] Barge watcher done: {frames} frames "
            f"({frames * 80}ms) scored, peak={peak:.3f}, threshold={threshold:.2f}, "
            f"rms mean={rms_mean:.4f} max={rms_max:.4f} "
            f"(device noise floor {getattr(device, 'noise_floor', 0.0):.4f})"
        )


async def _run_post_turn_playback(device: Device, voice_response: bytes) -> None:
    """
    Post-turn timing concern: EQ, resample, stream to device, acoustic-feedback wait.

    voice_response is raw Piper-rate mono S16_LE PCM. Returns once the
    device audio buffer has drained (or cancel_event fires), so the caller
    can safely restart the mic without acoustic feedback into the next turn.
    """
    log.info(
        f"[{device.device_id}] EQ: bands={device.eq_bands} "
        f"loudness={device.eq_loudness}"
    )
    eq_pcm      = em_eq.apply(voice_response, PIPER_RATE, device.eq_bands, device.eq_loudness)
    speaker_pcm = resample_to_stereo_48k(eq_pcm, PIPER_RATE)
    log.info(
        f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes "
        f"({len(speaker_pcm)//SPEAKER_BYTES} periods)"
    )
    cancel_task    = asyncio.create_task(device.cancel_event.wait())
    stream_task    = asyncio.create_task(device.stream_speaker(speaker_pcm))
    t_stream_start = asyncio.get_event_loop().time()

    done, _ = await asyncio.wait(
        [stream_task, cancel_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if cancel_task in done:
        log.info(f"[{device.device_id}] Cancelled during playback")
        stream_task.cancel()
    else:
        if not device.cancel_event.is_set():
            audio_duration = len(speaker_pcm) / (SPEAKER_RATE * 4)  # stereo S16LE
            elapsed        = asyncio.get_event_loop().time() - t_stream_start
            remaining      = max(0.0, audio_duration - elapsed)
            log.info(
                f"[{device.device_id}] Streaming took {elapsed:.1f}s, "
                f"sleeping {remaining:.1f}s for buffer drain "
                f"(total={audio_duration:.1f}s)"
            )
            if remaining > 0:
                # The drain sleep must race cancel_event too: the WS write
                # finishes well ahead of real-time playback, so a barge-in
                # usually lands HERE, not mid-stream. An uncancellable sleep
                # left the turn hanging for the rest of the response length
                # after the device had already flushed — no listening LEDs,
                # and the user's follow-up words piling into voice_queue
                # (observed 5.7s dead window, 2026-07-10).
                sleep_task = asyncio.create_task(asyncio.sleep(remaining))
                await asyncio.wait(
                    [sleep_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                sleep_task.cancel()
            if device.cancel_event.is_set():
                log.info(f"[{device.device_id}] Cancelled during buffer drain")
            else:
                log.info(f"[{device.device_id}] Playback complete")

    cancel_task.cancel()


async def _run_voice_locked(device: Device, trigger_label: str = "unknown", is_wakeword: bool = False):
    """
    is_wakeword: explicit flag for whether this turn was triggered by wake-
    word detection (as opposed to a button press). Used to decide preroll
    discard (see C3) — kept as its own parameter rather than inferred by
    parsing trigger_label (which is a free-form string meant for logging/
    trace display, not a control-flow key) so a future change to the label
    format can't silently change behaviour here.
    """
    drained = 0
    while not device.mic_queue.empty():
        try:
            device.mic_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    if drained:
        log.info(f"[{device.device_id}] Voice turn: drained {drained} stale frames")
    try:
        async with device.voice_lock:
            log.info(f"[{device.device_id}] Voice turn starting (esphome mode)")
            device.listening = True
            await leds_listening(device)
            await _push_device_state(device)

            stop_spin = asyncio.Event()
            spin_task = None
            # Barge-in watcher state — reset per turn iteration below.
            watcher          = None
            playback_started = asyncio.Event()

            async def stop_watcher():
                nonlocal watcher
                if watcher is None:
                    return
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning(f"[{device.device_id}] Barge watcher error: {e}")
                watcher = None

            async def cleanup_esphome():
                device.thinking  = False
                device.listening = False
                await _push_device_state(device)
                stop_spin.set()
                if spin_task and not spin_task.done():
                    spin_task.cancel()
                    try:
                        await spin_task
                    except asyncio.CancelledError:
                        pass
                await leds_off(device)

            async def on_thinking_esphome():
                nonlocal spin_task, watcher
                if stop_spin.is_set():
                    return  # cleanup already ran; turn is over
                device.thinking  = True
                device.listening = False
                await _push_device_state(device)
                log.info(f"[{device.device_id}] Thinking (esphome)")
                if not device.cancel_event.is_set() and (
                    spin_task is None or spin_task.done()
                ):
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                # Barge-in watcher starts here, not at playback: STT has
                # ended (VAD_END), so anything on the mic from now on is
                # a potential interruption. Spans thinking → playback;
                # cancelled in the turn loop's finally.
                if device.barge_in_enabled and (
                    watcher is None or watcher.done()
                ):
                    watcher = asyncio.create_task(
                        _barge_watcher(device, playback_started)
                    )

            async def post_turn_play_esphome(voice_response: bytes):
                nonlocal spin_task, watcher
                if spin_task is None or spin_task.done():
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                if device.barge_in_enabled:
                    # Barge-in (§3.2): keep the mic running through
                    # playback — the device's AEC subtracts the speaker
                    # output, which is what makes this safe (enable AEC
                    # before enabling barge-in; without it the watcher
                    # scores raw echo and the raised threshold is the
                    # only defence). The pre-AEC problems the mic_stop
                    # guarded against are gone: AGC no longer exists on
                    # the wake stream (v2.7.0) and echo content is
                    # cancelled at the source (v2.7.3).
                    #
                    # The watcher normally exists already (started at
                    # thinking onset); the phase flag switches it to the
                    # playback threshold. Defensive create for turns
                    # that reach TTS without an STT_VAD_END.
                    playback_started.set()
                    if watcher is None or watcher.done():
                        watcher = asyncio.create_task(
                            _barge_watcher(device, playback_started)
                        )
                    await _run_post_turn_playback(device, voice_response)
                    return
                # Acoustic-feedback guard (barge-in off): stop the mic
                # BEFORE playback, not just in the post-turn finally.
                # With the mic running through TTS pre-AEC, the device
                # processed its own speaker echo (63-65 junk frames per
                # turn measured 2026-07-06) and sent it upstream on the
                # same Wi-Fi radio receiving the TTS frames (speaker
                # underruns → audible stutter). The finally's mic_stop
                # stays as a safety net (StopMic no-ops when already
                # stopped); restart is owned by the continuation branch /
                # wake listener / button handler as before.
                await device.mic_stop()
                await _run_post_turn_playback(device, voice_response)

            # P0-1: no mic_start_turn() here on the initial (wake/button)
            # entry — for a wake turn the stream is already running on
            # ch6 and oww_paused routes frames to voice_queue. The
            # acoustic-feedback guard is mic_stop in
            # post_turn_play_esphome, sent immediately before TTS
            # playback; the finally below is only the safety net.
            #
            # Continuation loop: if HA sets continue_conversation on
            # INTENT_END, re-trigger immediately after TTS+drain rather
            # than returning to OWW idle. The reference implementation
            # (linux-voice-assistant) uses a 0.5s settle delay after TTS
            # before opening the mic — that's already covered by
            # _run_post_turn_playback's buffer drain sleep, so no
            # additional delay is needed here.
            #
            # C2 fix (2026-07-05 review): the `finally` below runs
            # device.mic_stop() on every iteration, including the one
            # that decides to continue — previously nothing ever put the
            # stream back before looping into the next trigger_voice_turn,
            # so a continuation turn streamed from a stopped mic and
            # silently timed out as no_speech every time. Fixed by
            # calling device.mic_start() (no lock_mic — same ch6 stream
            # as the wake path; no-ops if somehow already running) in the
            # continuation branch, before looping.
            #
            # C3 fix: preroll_discard is 0 for button/continuation turns
            # (no wake-word tail to remove — discarding real audio here
            # just clips the first word/words, the exact bug P0-1 fixed
            # on the wake path) and VOICE_PREROLL_DISCARD only for the
            # initial wakeword-triggered turn.
            turn_label      = trigger_label
            preroll_discard = esphome.VOICE_PREROLL_DISCARD if is_wakeword else 0
            while True:
                should_continue = False
                try:
                    should_continue = await esphome.trigger_voice_turn(
                        device=device,
                        on_thinking=on_thinking_esphome,
                        post_turn_play=post_turn_play_esphome,
                        trigger_label=turn_label,
                        preroll_discard=preroll_discard,
                    )
                finally:
                    # Watcher spans thinking→playback and is owned here:
                    # every exit path (normal, barge, error, cancel)
                    # must stop it before the next iteration re-arms.
                    await stop_watcher()
                    # On barge the mic stays up: the user's follow-up
                    # command is already flowing into voice_queue and a
                    # mic_stop/start cycle here would drop the words
                    # spoken in the same breath as the wake word.
                    if not device.barge_detected:
                        await device.mic_stop()
                    await cleanup_esphome()
                    log.info(f"[{device.device_id}] Voice turn complete (esphome mode)")

                if device.barge_detected:
                    # Barge-in: the watcher cancelled playback because
                    # the wake word was spoken over it. Re-enter a fresh
                    # turn immediately — same shape as continuation, but
                    # with the wake-word preroll discard (there IS a
                    # "…rhasspy" tail to drop this time).
                    device.barge_detected = False
                    device.cancel_event.clear()
                    log.info(f"[{device.device_id}] Barge-in: starting interrupting turn")
                    await device.mic_start()  # defensive no-op if running
                    # Re-arm listening state — cleanup_esphome() in the
                    # finally just turned the ring off, which left the
                    # device dark while it was actually listening for the
                    # interrupting command (looked dead — user report
                    # 2026-07-08). Same re-arm as the continuation branch;
                    # no voice_queue drain here though, the follow-up
                    # words spoken after "hey rhasspy" are already in it.
                    device.listening = True
                    await leds_listening(device)
                    await _push_device_state(device)
                    turn_label      = "barge-in"
                    preroll_discard = esphome.VOICE_PREROLL_DISCARD
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                    continue

                if should_continue and not device.cancel_event.is_set():
                    log.info(f"[{device.device_id}] Continuing conversation (HA requested)")
                    # C2 fix: put the mic stream back before looping —
                    # the finally above just stopped it, and the next
                    # trigger_voice_turn will read from voice_queue,
                    # which is fed only while the device stream is
                    # running. No lock_mic — same ch6 stream as wake.
                    await device.mic_start()
                    # Fresh stream starts with the VAD gate closed — the
                    # user must speak again from zero, same onset cost
                    # as any post-mic_stop restart. Acceptable for v1 of
                    # continuation (see review C2 wrinkle note); §3.4's
                    # device preroll ring will fix this properly later.
                    # Drain stale frames accumulated during TTS playback
                    # before the next turn begins — same as post-wake drain.
                    drained = 0
                    while not device.voice_queue.empty():
                        try:
                            device.voice_queue.get_nowait()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
                    if drained:
                        log.debug(f"[{device.device_id}] Continuation: drained {drained} stale frames")
                    # Re-arm listening state for the follow-up turn.
                    device.listening = True
                    await leds_listening(device)
                    await _push_device_state(device)
                    turn_label      = "continuation"
                    preroll_discard = 0
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                else:
                    break

    finally:
        # Drain voice_queue BEFORE clearing oww_paused. If we clear first,
        # handle_data immediately starts routing new frames to mic_queue —
        # correct. But voice_queue still contains frames that arrived during
        # the turn (post-TTS playback, during the buffer drain sleep). Those
        # frames will sit in voice_queue until the NEXT wake detection flips
        # oww_paused back, at which point they arrive at _stream_mic_audio as
        # preamble before the user has said anything — Whisper then transcribes
        # 10+ seconds of ambient noise mixed with the actual utterance.
        # Draining here, while oww_paused is still set, ensures voice_queue is
        # empty before routing flips. The post-turn drain in wake_word_listener
        # (after _run_voice_locked returns) becomes a belt-and-braces no-op.
        _drained = 0
        while not device.voice_queue.empty():
            try:
                device.voice_queue.get_nowait()
                _drained += 1
            except asyncio.QueueEmpty:
                break
        if _drained:
            log.info(
                f"[{device.device_id}] oww_paused drain: "
                f"{_drained} stale frames cleared before routing flip"
            )
        device.oww_paused.clear()
        log.info(f"[{device.device_id}] oww_paused cleared")


# ─── Wake word listener ───────────────────────────────────────────────────────

async def wake_word_listener(device: Device):
    loop = asyncio.get_event_loop()

    current_model_name = device.oww_model
    current_speex_ns    = device.oww_speex_ns
    log.info(
        f"[{device.device_id}] OWW: loading model {current_model_name} "
        f"(speex_ns={current_speex_ns})"
    )
    model = await loop.run_in_executor(
        None,
        lambda: OWWModel(
            wakeword_models=[current_model_name],
            enable_speex_noise_suppression=current_speex_ns,
        ),
    )
    model_key = current_model_name

    log.info(f"[{device.device_id}] OWW: starting (initial threshold={device.oww_threshold:.3f})")
    await device.mic_start()

    buf = bytearray()
    last_near_miss_log_ts = 0.0  # Q4: rate-limit near-miss INFO logging to 1/2s
    try:
        while True:
            if device.oww_model != current_model_name or device.oww_speex_ns != current_speex_ns:
                new_name  = device.oww_model
                new_speex = device.oww_speex_ns
                log.info(
                    f"[{device.device_id}] OWW: reloading model "
                    f"{current_model_name} → {new_name} "
                    f"(speex_ns {current_speex_ns} → {new_speex})"
                )
                try:
                    _n = new_name
                    _s = new_speex
                    new_model = await loop.run_in_executor(
                        None,
                        lambda: OWWModel(
                            wakeword_models=[_n],
                            enable_speex_noise_suppression=_s,
                        ),
                    )
                    model             = new_model
                    model_key         = new_name
                    current_model_name = new_name
                    current_speex_ns  = new_speex
                    buf.clear()
                    log.info(f"[{device.device_id}] OWW: model reloaded → {new_name} (speex_ns={new_speex})")
                except Exception as e:
                    log.error(
                        f"[{device.device_id}] OWW: failed to load {new_name} "
                        f"(speex_ns={new_speex}): {e} "
                        f"— reverting to {current_model_name} (speex_ns={current_speex_ns})"
                    )
                    device.oww_model     = current_model_name
                    device.oww_speex_ns  = current_speex_ns
            try:
                payload = await asyncio.wait_for(
                    device.mic_queue.get(), timeout=10.0
                )
            except asyncio.TimeoutError:
                # The wake stream is ungated and continuous (device sends
                # every 80ms, silence included — hardware mute still produces
                # zero-filled frames), so 10s of nothing on mic_queue means
                # the stream died — NOT ordinary silence, as it did when the
                # device VAD gate existed on this stream. Exception: during a
                # voice turn frames route to voice_queue instead, so an idle
                # mic_queue is expected while oww_paused is set.
                if device.oww_paused.is_set():
                    continue
                log.warning(
                    f"[{device.device_id}] OWW: no mic frames for 10s on the "
                    f"continuous wake stream — sending defensive mic_start"
                )
                await device.mic_start()
                continue

            # VAD sentinel (string; None accepted defensively — the pre-B5
            # encoding) — flush partial audio so OWW never scores across a
            # stream boundary.
            if payload is None or isinstance(payload, str):
                buf.clear()
                continue

            if device.oww_paused.is_set():
                continue

            if device.muted:
                buf.clear()
                continue

            buf.extend(payload)
            while len(buf) >= CHUNK_BYTES:
                frame   = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)

                if device.speaking:
                    continue

                # Per-room noise floor tracking (measurement only — the audio
                # is never modified). Asymmetric EWMA: follows drops quickly
                # (α=0.3) so it converges down fast, rises slowly (α=0.008 ≈
                # 10s time constant at 12.5 chunks/s) so speech bursts don't
                # drag it up. Feeds the SNR-relative no-speech detection in
                # em_esphome._stream_mic_audio and the diagnostics below.
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                if device.noise_floor == 0.0:
                    device.noise_floor = rms
                elif rms < device.noise_floor:
                    device.noise_floor += 0.3 * (rms - device.noise_floor)
                else:
                    device.noise_floor += 0.008 * (rms - device.noise_floor)

                prediction = await loop.run_in_executor(
                    None, model.predict, samples
                )
                score = prediction.get(model_key, 0.0)

                # Log any score above noise floor so we can see near-misses
                # and understand whether failed wakes are "close but below
                # threshold" vs "not registering at all".
                #
                # Q4 fix (2026-07-05 review): this was DEBUG-only, invisible
                # in a normal INFO deployment — exactly the data needed for
                # threshold tuning was blind by default. Now: (1) the debug
                # line stays for verbose troubleshooting, (2) an INFO line
                # fires too, rate-limited to at most once per 2s per device
                # so a run of near-misses doesn't flood the log, and (3) a
                # persistent near_misses counter is exposed to the dashboard
                # via device_update so the count is visible without tailing
                # logs at all.
                if score > 0.05:
                    device.oww_near_misses += 1
                    log.debug(
                        f"[{device.device_id}] OWW score: {score:.3f} "
                        f"(threshold={device.oww_threshold:.3f}, "
                        f"rms={rms:.4f}, floor={device.noise_floor:.4f})"
                    )
                    now = asyncio.get_event_loop().time()
                    if now - last_near_miss_log_ts >= 2.0:
                        last_near_miss_log_ts = now
                        log.info(
                            f"[{device.device_id}] OWW near-miss: score={score:.3f} "
                            f"(threshold={device.oww_threshold:.3f}, "
                            f"total near-misses={device.oww_near_misses})"
                        )
                        await api._push_event({
                            "type":      "device_update",
                            "device_id": device.device_id,
                            "state":     {"owwNearMisses": device.oww_near_misses},
                        })

                if score >= device.oww_threshold:
                    log.info(
                        f"[{device.device_id}] Wake word detected "
                        f"(score={score:.3f}, rms={rms:.4f}, "
                        f"floor={device.noise_floor:.4f})"
                    )
                    db.log_device(
                        device.device_id, "info", "device",
                        f"Wake word detected (score={score:.3f})"
                    )
                    if not device.voice_lock.locked():
                        # P0-1: do NOT send mic_stop/mic_start_turn.
                        # The stream stays running continuously. Flipping
                        # oww_paused routes subsequent frames to voice_queue.
                        # The VAD gate is already open mid-utterance — that's
                        # how OWW got the wake-word audio — so command audio
                        # flows in with zero re-trigger delay and zero RTT gap.
                        # Wake-word tail bleed ("…Jarvis") is handled by the
                        # preroll discard in _stream_mic_audio.
                        # TTS mic_stop/mic_start remains untouched — that
                        # acoustic-feedback guard is load-bearing.
                        model.reset()
                        buf.clear()
                        device.cancel_event.clear()
                        device.oww_paused.set()
                        log.debug(
                            f"[{device.device_id}] OWW: oww_paused set, "
                            f"routing to voice_queue (no mic_stop/mic_start_turn)"
                        )
                        # Lock the beamformer onto the speaker's perimeter mic
                        # NOW, mid-utterance — the onset detector has the
                        # freshest possible signal at this moment. No stream
                        # restart; released by beam_unlock post-turn (and
                        # implicitly by any TTS mic stop/start cycle).
                        await device.beam_lock()
                        await _run_voice_locked(device, trigger_label=f"wakeword({score:.3f})", is_wakeword=True)
                        # Back to ch6 omni for wake listening. Belt-and-braces
                        # for turns that never restarted the stream (no-TTS
                        # outcomes: error, no-speech, cancel) — a lock left
                        # in place would point wake listening at one
                        # perimeter mic instead of omni.
                        await device.beam_unlock()

                        drained = 0
                        while not device.voice_queue.empty():
                            try:
                                device.voice_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        if drained:
                            log.info(
                                f"[{device.device_id}] OWW: "
                                f"drained {drained} stale frames post-turn"
                            )
                        model.reset()
                        buf.clear()
                        # mic_start without lock_mic — device stays on ch6 omni
                        # (beamforming=off), same stream as OWW listening.
                        # This is a defensive restart only: if the stream
                        # somehow died during the turn, this revives it.
                        # If already running, the device no-ops it.
                        log.info(f"[{device.device_id}] OWW: defensive mic_start (no lock_mic)")
                        await device.mic_start()
                    else:
                        log.info(
                            f"[{device.device_id}] Voice turn active — "
                            f"ignoring wake"
                        )
                        model.reset()

    except asyncio.CancelledError:
        await device.mic_stop()
        raise


# ─── Button handler ───────────────────────────────────────────────────────────

async def handle_button_event(device: Device, event: dict):
    click_type = event.get("clickType")
    down       = event.get("down", True)

    if down:
        return

    if click_type == 138:   # DotClick
        if device.voice_lock.locked():
            log.info(f"[{device.device_id}] Dot button — cancelling voice turn")
            device.cancel_event.set()
            esphome.cancel_voice_turn(device.device_id)
        else:
            log.info(f"[{device.device_id}] Dot button → voice turn")
            device.cancel_event.clear()
            device.oww_paused.set()
            async def _button_voice_turn():
                # Button is a deliberate act with no dead zone cost — nothing
                # is being said at the moment of press, so stop/start RTT is
                # fine. Stop the running ch6 stream, restart with lock_mic:true
                # so streamMic calls beam.Lock(beamformingEnabled) and the
                # beamformer selects the best perimeter mic for this turn.
                # mic_start_turn() no-ops if already running, so stop first.
                await device.mic_stop()
                await device.mic_start_turn()
                await _run_voice_locked(device, trigger_label="button", is_wakeword=False)
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                # Post-turn: back to ch6 omni for OWW listening. mic_stop
                # first: if the turn had no TTS (cancel/error/no-speech), the
                # lock_mic stream from mic_start_turn is still running and a
                # bare mic_start would no-op against it — leaving the GATED,
                # beam-locked turn stream as the permanent wake stream. Safe
                # now that streamMic's exit has the ownership check (the
                # stop/start pair can no longer leak a second stream).
                await device.mic_stop()
                await device.mic_start()
            # M1 fix (2026-07-05 review): keep a reference and log exceptions
            # instead of a bare fire-and-forget create_task() — previously
            # any exception raised in this task vanished silently with no
            # log line, standard asyncio fire-and-forget hygiene issue.
            _btn_task = asyncio.create_task(_button_voice_turn())
            _btn_task.add_done_callback(_log_task_exception)


# ─── Control plane handler ────────────────────────────────────────────────────

async def handle_control(ws: WebSocketServerProtocol):
    """
    Handle a /control WebSocket connection from a device.
    """
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "register":
            log.warning(
                f"[control] First message from {remote} was not register — closing"
            )
            await ws.close()
            return

        device_id    = msg["device_id"]
        ip           = msg.get("ip", str(remote[0]))
        version      = msg.get("version")
        capabilities = msg.get("capabilities", [])

        loop         = asyncio.get_event_loop()
        approval_mode = db.get_config("device_approval", DEVICE_APPROVAL)
        row          = await loop.run_in_executor(None, db.get_device, device_id)

        if row is None:
            if approval_mode == "auto":
                label = f"Unknown {device_id[:8]}"
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await loop.run_in_executor(
                    None, db.approve_device, device_id, label, None
                )
                log.info(
                    f"[control] Auto-approved new device: {device_id} "
                    f"label={label!r}"
                )
                row = await loop.run_in_executor(None, db.get_device, device_id)
            else:
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await ws.send(json.dumps({"type": "pending"}))
                log.info(
                    f"[control] Unknown device held as pending: {device_id} "
                    f"from {ip}"
                )
                await api.notify_device_pending(device_id, ip)
                db.log_device(
                    device_id, "info", "controller",
                    f"Device seen for first time — pending approval ({ip})"
                )
                await ws.close()
                return

        if not row["approved"]:
            await loop.run_in_executor(
                None, db.upsert_device_seen, device_id, ip, version
            )
            await ws.send(json.dumps({"type": "pending"}))
            log.info(
                f"[control] Device pending approval: {device_id} from {ip}"
            )
            await api.notify_device_pending(device_id, ip)
            await ws.close()
            return

        await loop.run_in_executor(
            None, db.upsert_device_seen, device_id, ip, version
        )

        device = Device(device_id, ip, capabilities, ws)
        _devices[device_id] = device

        log.info(
            f"[control] Device connected: {device_id} v={version} "
            f"at {ip} caps={capabilities}"
        )
        db.log_device(
            device_id, "info", "controller",
            f"Connected from {ip} version={version}"
        )

        await device.send_control({"type": "ack", "device_id": device_id})

        config = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        await device.send_control({"type": "config", **config})
        device.oww_threshold = float(config.get("owwThreshold", OWW_THRESHOLD))
        device.oww_model     = config.get("owwModel", f"{OWW_MODEL}_v0.1")
        device.oww_speex_ns  = bool(config.get("owwSpeexNs", False))
        device.barge_in_enabled = bool(config.get("bargeInEnabled", False))
        device.barge_threshold  = float(config.get("bargeInThreshold", 0.6))
        device.eq_bands      = config.get("eqBands", [0.0] * 8)
        device.eq_loudness   = bool(config.get("eqLoudness", False))
        device.led_scene     = em_scenes.resolve(config)
        # Initialise volume from stored config — device will report its real
        # value via volume_state on connect, but this seeds a sane default
        # in the window before that first message arrives.
        device.volume = _device_level_to_ha(
            int(config.get("startupVolume", 85))
        )
        log.info(f"[control] Config pushed to {device_id} (volume={device.volume:.3f})")

        await leds_off(device)
        await api.notify_device_connected(device_id)
        _device_ref = device
        async def _standalone_play(pcm_bytes: bytes, _d=_device_ref) -> None:
            # Same acoustic-feedback guard as voice turns: announcements
            # play outside a turn, so the always-on OWW stream is live —
            # stop it for the duration and put it back after.
            await _d.mic_stop()
            try:
                await _run_post_turn_playback(_d, pcm_bytes)
            finally:
                await _d.mic_start()
        async def _send_volume_set(level: int, _d=_device_ref) -> None:
            await _d.send_control({"type": "volume_set", "level": level})
        await esphome.device_connected(
            device_id,
            SERVER_HOST,
            standalone_play=_standalone_play,
            send_volume_set=_send_volume_set,
        )
        # The ESPHome server object caches the OWW model from server
        # creation — refresh it from the config we just loaded so HA's
        # wake-word dropdown tracks dashboard changes across controller
        # restarts too.
        esphome.update_oww_model(device_id, device.oww_model)

        # ── Main message loop ─────────────────────────────────────────────

        async def ping_loop():
            while True:
                await asyncio.sleep(30)
                await device.ping()

        ping_task = asyncio.create_task(ping_loop())
        oww_task  = asyncio.create_task(wake_word_listener(device))

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                msg_type = msg.get("type")

                if msg_type == "button":
                    await handle_button_event(device, msg)

                elif msg_type == "mute_state":
                    device.muted = msg.get("muted", False)
                    if device.muted and device.voice_lock.locked():
                        # Mute during an active turn terminates it — same
                        # cancel as the dot button, plus speaker_flush so
                        # any in-flight TTS goes silent immediately (the
                        # device shows the red ring the moment the button
                        # is pressed; audio carrying on would contradict
                        # it). The device guards its LED ring while muted,
                        # so the cancelled turn's LED cleanup can't clear
                        # the red ring.
                        log.info(
                            f"[{device_id}] Muted during active turn — "
                            f"cancelling"
                        )
                        device.cancel_event.set()
                        esphome.cancel_voice_turn(device_id)
                        await device.send_control({"type": "speaker_flush"})
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {"muted": device.muted},
                    })

                elif msg_type == "volume_state":
                    # Device reports current volume level (0–175 int).
                    # Convert to HA float, update in-memory state, persist to
                    # config so the value survives controller and device restarts.
                    raw_level = int(msg.get("level", 85))
                    device.volume = _device_level_to_ha(raw_level)
                    log.debug(
                        f"[{device_id}] volume_state: level={raw_level} "
                        f"→ {device.volume:.3f}"
                    )
                    # Persist — read-modify-write to avoid stomping other fields
                    stored_config = await loop.run_in_executor(
                        None, db.get_device_config, device_id
                    )
                    stored_config["startupVolume"] = raw_level
                    await loop.run_in_executor(
                        None, db.set_device_config, device_id, stored_config
                    )
                    # Notify ESPHome satellite so HA's media player entity updates
                    esphome.update_device_volume(device_id, device.volume)

                elif msg_type == "stats":
                    device.stats = {
                        "cpuPct":        msg.get("cpuPct"),
                        "memUsedMb":     msg.get("memUsedMb"),
                        "memTotalMb":    msg.get("memTotalMb"),
                        "storageUsedMb": msg.get("storageUsedMb"),
                        "storageTotalMb":msg.get("storageTotalMb"),
                        "wifiRssi":      msg.get("wifiRssi"),
                        "wifiSsid":      msg.get("wifiSsid"),
                    }
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {"stats": device.stats},
                    })

                elif msg_type == "wifi_result":
                    # Outcome of a wifi_change. The device re-sends this
                    # until it sees a wifi_commit ack (a single send can
                    # vanish into a half-open TCP connection killed by the
                    # network switch), so: ALWAYS ack — on success the ack
                    # also finalises the change (deletes rollback backup +
                    # pending marker; a failed change already removed both,
                    # so the ack is a no-op there) — and log/record only
                    # the first arrival.
                    ok    = bool(msg.get("ok"))
                    ssid  = msg.get("ssid", "")
                    error = msg.get("error") or ""
                    st, duplicate = api.wifi_record_result(device_id, ok, ssid, error)
                    await device.send_control({"type": "wifi_commit"})
                    if not duplicate:
                        if ok:
                            log.info(f"[{device_id}] WiFi changed to \"{ssid}\" — committed")
                            db.log_device(device_id, "info", "device",
                                          f'WiFi changed to "{ssid}"')
                        else:
                            log.warning(f"[{device_id}] WiFi change to \"{ssid}\" "
                                        f"failed: {error}")
                            db.log_device(device_id, "warning", "device",
                                          f'WiFi change to "{ssid}" failed: {error}')
                        await api._push_event({
                            "type":      "device_update",
                            "device_id": device_id,
                            "state":     {"wifi": st},
                        })

                elif msg_type == "wifi_scan_result":
                    fut = device.wifi_scan_future
                    if fut is not None and not fut.done():
                        fut.set_result(msg)

                elif msg_type == "log":
                    level   = msg.get("level", "info")
                    message = msg.get("message", "")
                    db.log_device(device_id, level, "device", message)
                    await api._push_log_event(device_id, level, "device", message)

                elif msg_type == "pong":
                    pass

                else:
                    log.debug(
                        f"[{device_id}] Unknown control message: {msg_type}"
                    )

        finally:
            ping_task.cancel()
            oww_task.cancel()

    except asyncio.TimeoutError:
        log.warning(f"[control] Registration timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[control] Handler error: {e}")

    finally:
        if device:
            log.info(f"[control] Device disconnected: {device.device_id}")
            db.log_device(
                device.device_id, "info", "controller", "Disconnected"
            )
            if _devices.get(device.device_id) is device:
                _devices.pop(device.device_id, None)
            await api.notify_device_disconnected(device.device_id)
            await esphome.device_disconnected(device.device_id)


# ─── Data plane handler ───────────────────────────────────────────────────────

async def handle_data(ws: WebSocketServerProtocol):
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "identify":
            log.warning(
                f"[data] First message from {remote} was not identify — closing"
            )
            await ws.close()
            return

        device_id = msg["device_id"]

        for _ in range(20):
            device = _devices.get(device_id)
            if device is not None:
                break
            await asyncio.sleep(0.1)

        if device is None:
            log.warning(f"[data] Unknown device_id: {device_id} — closing")
            await ws.close()
            return

        device.data_ws = ws
        device.data_ready.set()
        log.info(f"[data] Data connection established: {device_id}")

        async for raw in ws:
            if not isinstance(raw, bytes):
                continue
            if len(raw) <= MIC_HEADER_LEN:
                continue
            if raw[0] != MIC_FRAME_TYPE:
                continue
            if len(raw) == MIC_HEADER_LEN + 1 and raw[MIC_HEADER_LEN] in (VAD_END_TYPE, VAD_NO_SPEECH_TIMEOUT_TYPE):
                sentinel = (
                    esphome.VAD_SENTINEL_TIMEOUT
                    if raw[MIC_HEADER_LEN] == VAD_NO_SPEECH_TIMEOUT_TYPE
                    else esphome.VAD_SENTINEL_END
                )
                q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
                if q.full():
                    try:
                        q.get_nowait()
                        log.warning(f"[{device.device_id}] queue full — dropped one frame to deliver VAD sentinel")
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(sentinel)
                except asyncio.QueueFull:
                    log.error(f"[{device.device_id}] VAD sentinel lost — queue still full after drain")
                continue
            payload = raw[MIC_HEADER_LEN:]
            q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the OLDEST frame, not the newest — keeps the tail of
                # the audio contiguous with real time, which is what OWW and
                # STT care about. Dropping the newest froze the queue at a
                # stale snapshot while fresh speech was discarded.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    except asyncio.TimeoutError:
        log.warning(f"[data] Identify timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[data] Handler error: {e}")

    finally:
        if device:
            if device.data_ws is ws:
                device.data_ws = None
                device.data_ready.clear()
            log.info(f"[data] Data connection closed: {device.device_id}")


# ─── Router ───────────────────────────────────────────────────────────────────

# ─── Shell plane handler ──────────────────────────────────────────────────────

async def handle_shell(ws: WebSocketServerProtocol, path: str):
    import aiohttp as _aiohttp

    # Path may carry a query: /shell/{device_id}?pty=1 signals that the
    # device actually established a PTY session (it may have been requested
    # but failed to allocate — the device falls back to a plain pipe and
    # omits the flag). The dashboard needs the established mode, not the
    # requested one, to pick its input framing.
    device_id, _, query = path.removeprefix("/shell/").partition("?")
    pty_mode = "pty=1" in query
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id} (pty={pty_mode})")

    done_future  = _shell_pending.get(device_id)
    dashboard_ws = _shell_dashboard.get(device_id)

    if done_future is None or done_future.done():
        log.warning(f"[shell] No pending shell request for {device_id} — closing")
        await ws.close()
        return

    if dashboard_ws is None:
        log.info(f"[shell] Programmatic session: {device_id}")
        done_future.set_result(ws)
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=300.0)
        except (asyncio.TimeoutError, Exception):
            pass
        log.info(f"[shell] Programmatic session ended: {device_id}")
        return

    log.info(f"[shell] Proxying: {device_id}")

    # Tell the dashboard which mode the device established before any
    # shell bytes flow: PTY sessions use framed input (0x00 stdin /
    # 0x01 resize) and emit terminal escape sequences; pipe sessions
    # (pre-PTY firmware) are raw both ways.
    try:
        await dashboard_ws.send_str(json.dumps({"type": "shell_meta", "pty": pty_mode}))
    except Exception:
        pass

    async def device_to_dashboard():
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await dashboard_ws.send_bytes(msg)
                else:
                    await dashboard_ws.send_str(msg)
        except Exception:
            pass

    async def dashboard_to_device():
        try:
            async for msg in dashboard_ws:
                if msg.type == _aiohttp.WSMsgType.BINARY:
                    await ws.send(msg.data)
                elif msg.type == _aiohttp.WSMsgType.TEXT:
                    await ws.send(msg.data.encode())
                elif msg.type in (_aiohttp.WSMsgType.CLOSE,
                                  _aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass

    tasks = [
        asyncio.create_task(device_to_dashboard()),
        asyncio.create_task(dashboard_to_device()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        log.info(f"[shell] Session ended: {device_id}")
        if not done_future.done():
            done_future.set_result(None)

async def router(ws: WebSocketServerProtocol):
    path = ws.request.path if hasattr(ws, "request") else getattr(ws, "path", "/")

    if path == "/control":
        await handle_control(ws)
    elif path == "/data":
        await handle_data(ws)
    elif path.startswith("/shell/"):
        await handle_shell(ws, path)
    else:
        log.warning(f"Unknown WebSocket path: {path} from {ws.remote_address}")
        await ws.close()


# ─── mDNS ─────────────────────────────────────────────────────────────────────

def _make_mdns_info() -> ServiceInfo:
    return ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=SERVER_PORT,
        properties={"version": "1", "server": MDNS_NAME},
        server=f"{MDNS_NAME}.local.",
    )


async def _mdns_refresh_loop(azc: AsyncZeroconf, info: ServiceInfo) -> None:
    while True:
        await asyncio.sleep(MDNS_REFRESH_INTERVAL)
        try:
            await azc.async_update_service(info)
            log.debug("mDNS registration refreshed")
        except Exception as e:
            log.warning(f"mDNS refresh failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"EchoMuse Controller {api.CONTROLLER_VERSION}")
    db.init(DB_PATH)
    auth.maybe_generate_bootstrap_token()

    runner = await api.create_runner(_devices, _shell_pending, _shell_dashboard)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, API_PORT)
    await site.start()
    log.info(f"Dashboard + API listening on http://{SERVER_HOST}:{API_PORT}")

    release_task       = asyncio.create_task(api.release_poll_loop())
    session_prune_task = asyncio.create_task(api.session_prune_loop())

    azc  = AsyncZeroconf()
    info = _make_mdns_info()
    await azc.async_register_service(info, allow_name_change=True)
    log.info(
        f"mDNS advertising {MDNS_NAME}._emcontroller._tcp.local "
        f"→ {SERVER_IP}:{SERVER_PORT}"
    )
    mdns_task = asyncio.create_task(_mdns_refresh_loop(azc, info))

    log.info(f"WebSocket server starting on {SERVER_HOST}:{SERVER_PORT}")

    try:
        async with websockets.serve(
            router,
            SERVER_HOST,
            SERVER_PORT,
            ping_interval=20,
            ping_timeout=10,
            max_size=10 * 1024 * 1024,
        ):
            await esphome.start_esphome_servers(_devices, SERVER_HOST)

            log.info("EchoMuse Controller ready — waiting for devices")
            await asyncio.Future()

    finally:
        await esphome.stop_esphome_servers()
        release_task.cancel()
        session_prune_task.cancel()
        mdns_task.cancel()
        await azc.async_unregister_service(info)
        await azc.async_close()
        await runner.cleanup()
        log.info("EchoMuse Controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
