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
import em_esphome as esphome

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("echomuse")

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
API_PORT     = int(os.environ.get("API_PORT", "8768"))
SERVER_IP    = os.environ.get("SERVER_IP", "10.10.1.236")
VOICE_WS_URI = os.environ.get("VOICE_WS_URI", "ws://clara-voice:8765")
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")
DB_PATH      = os.environ.get("DB_PATH", "echomuse.db")

# Voice mode — 'claracore' (default) or 'esphome'.
# See ESPHOME_SPEC.md §1.2. Changing requires a controller restart.
VOICE_MODE   = os.environ.get("VOICE_MODE", "claracore")

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")

# Mic
CHUNK_BYTES          = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
# VOICE_PREROLL_DISCARD: frames to drop from voice_queue at the start of each
# turn. Under the P0-1 architecture the stream never stops, so voice_queue
# contains the tail of the wake word utterance ("…Jarvis") before the command
# audio arrives. Discarding the first N×80ms chunks removes most of that bleed.
# N=3 = 240ms. Tune empirically: if the first word of commands is still being
# clipped, lower; if "Hey Jarvis" bleed still reaches STT, raise.
VOICE_PREROLL_DISCARD = 3

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
# detected and then ended normally. Both still push a plain None sentinel
# onto the queue (existing claracore/OWW consumers don't need to
# differentiate), but esphome-mode's _stream_mic_audio checks
# device.last_vad_was_timeout to skip straight to a quiet turn-end rather
# than treating it as "waiting on a real pipeline response that never came."
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
        # Set immediately before queueing the None sentinel that resulted
        # from VAD_NO_SPEECH_TIMEOUT_TYPE, cleared immediately after —
        # esphome-mode's _stream_mic_audio checks this right after receiving
        # the sentinel to distinguish "never spoke" from a normal VAD end.
        # claracore mode and the OWW loop ignore it; both already treat any
        # None sentinel identically and don't need the distinction.
        self.last_vad_was_timeout = False

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
        self.eq_bands:      list  = [0.0] * 8
        self.eq_loudness:   bool  = False
        self.stats:         dict | None = None

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

    async def set_leds(self, leds: list):
        await self.send_control({"type": "leds", "leds": leds})

    async def ping(self):
        await self.send_control({"type": "ping"})

    async def mic_start(self):
        await self.send_control({"type": "mic_start"})

    async def mic_start_turn(self):
        """Start mic for a voice turn — signals device to lock the best directional mic."""
        await self.send_control({"type": "mic_start", "lock_mic": True})

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

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
            await self.send_data(bytes([SPEAKER_EOS_TYPE]))
        finally:
            self.speaking = False


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
    await device.set_leds(_make_leds(0, 180, 0))


async def leds_spin_green(device: Device, stop_event: asyncio.Event):
    pos = 0
    try:
        while not stop_event.is_set():
            leds = []
            for i in range(NUM_LEDS):
                if i == pos:
                    leds.append({"id": i, "r": 0, "g": 200, "b": 0})
                elif i == (pos - 1) % NUM_LEDS:
                    leds.append({"id": i, "r": 0, "g": 60, "b": 0})
                else:
                    leds.append({"id": i, "r": 0, "g": 0, "b": 0})
            await device.set_leds(leds)
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

async def _run_claracore_backend(
    device: Device,
    on_thinking,
) -> bytes:
    """
    Wire-protocol concern: bespoke ClaraCore WebSocket exchange.

    Connects to VOICE_WS_URI, streams mic audio from device.voice_queue,
    signals END on VAD sentinel, and returns the raw TTS PCM bytes when
    they arrive.

    Calls on_thinking() (async callable, no args) once when the "THINKING"
    sentinel is received from the voice server — device mechanics layer
    uses this to update LED/state without this function knowing about any
    of that.

    Returns b"" if cancelled, timed out, or the server sent no audio.
    Returns the raw PCM bytes on success.
    """
    voice_response: bytes = b""

    try:
        async with websockets.connect(
            VOICE_WS_URI, max_size=10 * 1024 * 1024
        ) as ws:
            log.info(f"[{device.device_id}] Connected to voice server")
            await ws.send("START")

            pcm_buf = bytearray()

            async def stream_mic():
                try:
                    while True:
                        if device.cancel_event.is_set():
                            return
                        try:
                            payload = await asyncio.wait_for(
                                device.voice_queue.get(), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            continue
                        if payload is None:
                            # VAD end sentinel — signal voice server speech has ended
                            log.info(
                                f"[{device.device_id}] VAD end — signalling voice server "
                                f"(pcm_buf={len(pcm_buf)}b queued)"
                            )
                            await ws.send("END")
                            return
                        log.debug(
                            f"[{device.device_id}] stream_mic: chunk "
                            f"{len(payload)}b (buf={len(pcm_buf)}b)"
                        )
                        pcm_buf.extend(payload)
                        while len(pcm_buf) >= CHUNK_BYTES:
                            chunk = bytes(pcm_buf[:CHUNK_BYTES])
                            del pcm_buf[:CHUNK_BYTES]
                            await ws.send(chunk)
                            log.debug(
                                f"[{device.device_id}] stream_mic: sent "
                                f"{len(chunk)}b to voice server"
                            )
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.error(f"[{device.device_id}] Mic stream error: {e}")

            async def receive_response():
                nonlocal voice_response
                try:
                    async for message in ws:
                        if isinstance(message, str) and message == "THINKING":
                            log.info(f"[{device.device_id}] Thinking signal from voice server")
                            await on_thinking()
                        elif isinstance(message, bytes) and message:
                            voice_response = message
                            log.info(
                                f"[{device.device_id}] Received "
                                f"{len(voice_response)} bytes audio"
                            )
                            return
                except Exception as e:
                    log.error(f"[{device.device_id}] WS receive error: {e}")

            mic_task     = asyncio.create_task(stream_mic())
            receive_task = asyncio.create_task(receive_response())
            cancel_task  = asyncio.create_task(device.cancel_event.wait())

            done, pending = await asyncio.wait(
                [receive_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=45.0,
            )

            if not done:
                log.warning(f"[{device.device_id}] Voice turn: voice server timeout — no response in 45s")
                for task in pending:
                    task.cancel()
                mic_task.cancel()
                return b""

            mic_task.cancel()

            if cancel_task in done:
                log.info(f"[{device.device_id}] Voice turn cancelled during backend exchange")
                receive_task.cancel()
                return b""

            cancel_task.cancel()
            try:
                await mic_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        log.error(f"[{device.device_id}] Voice turn WS failed: {e}")
        return b""

    return voice_response


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
                await asyncio.sleep(remaining)
            log.info(f"[{device.device_id}] Playback complete")

    cancel_task.cancel()


async def run_voice_turn(device: Device):
    """
    Device mechanics concern: coordinates a single voice turn end-to-end.
    """
    log.info(f"[{device.device_id}] Voice turn starting")
    device.listening = True
    await leds_listening(device)
    await _push_device_state(device)

    stop_spin  = asyncio.Event()
    spin_task  = None

    async def cleanup():
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

    async def on_thinking():
        nonlocal spin_task
        device.thinking  = True
        device.listening = False
        await _push_device_state(device)
        log.info(f"[{device.device_id}] Thinking — starting spinner")
        if not device.cancel_event.is_set() and (spin_task is None or spin_task.done()):
            spin_task = asyncio.create_task(leds_spin_green(device, stop_spin))

    # P0-1: no mic_start_turn(). Stream already running; oww_paused routes
    # frames to voice_queue. mic_stop after response remains for TTS feedback.
    try:
        voice_response = await _run_claracore_backend(device, on_thinking)
    except Exception as e:
        log.error(f"[{device.device_id}] Backend error: {e}")
        await device.mic_stop()
        await cleanup()
        return

    await device.mic_stop()

    if not voice_response:
        log.warning(f"[{device.device_id}] No audio response — ignoring")
        await cleanup()
        return

    if device.cancel_event.is_set():
        log.info(f"[{device.device_id}] Cancelled before playback")
        await cleanup()
        return

    if spin_task is None or spin_task.done():
        spin_task = asyncio.create_task(leds_spin_green(device, stop_spin))

    try:
        await _run_post_turn_playback(device, voice_response)
    except Exception as e:
        log.error(f"[{device.device_id}] Speaker failed: {e}")
    finally:
        await cleanup()

    log.info(f"[{device.device_id}] Voice turn complete")


async def _run_voice_locked(device: Device, trigger_label: str = "unknown"):
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
            if VOICE_MODE == "esphome":
                log.info(f"[{device.device_id}] Voice turn starting (esphome mode)")
                device.listening = True
                await leds_listening(device)
                await _push_device_state(device)

                stop_spin = asyncio.Event()
                spin_task = None

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
                    nonlocal spin_task
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

                async def post_turn_play_esphome(voice_response: bytes):
                    nonlocal spin_task
                    if spin_task is None or spin_task.done():
                        spin_task = asyncio.create_task(
                            leds_spin_green(device, stop_spin)
                        )
                    await _run_post_turn_playback(device, voice_response)

                # P0-1: no mic_start_turn() here. The stream is already
                # running on ch6; oww_paused routes frames to voice_queue.
                # mic_stop is still sent after the turn (before TTS playback)
                # as the acoustic-feedback guard — that remains load-bearing.
                try:
                    await esphome.trigger_voice_turn(
                        device=device,
                        on_thinking=on_thinking_esphome,
                        post_turn_play=post_turn_play_esphome,
                        trigger_label=trigger_label,
                    )
                finally:
                    await device.mic_stop()
                    await cleanup_esphome()
                    log.info(f"[{device.device_id}] Voice turn complete (esphome mode)")

            else:
                await run_voice_turn(device)
    finally:
        device.oww_paused.clear()
        log.info(f"[{device.device_id}] oww_paused cleared")


# ─── Wake word listener ───────────────────────────────────────────────────────

async def wake_word_listener(device: Device):
    loop = asyncio.get_event_loop()

    current_model_name = device.oww_model
    log.info(f"[{device.device_id}] OWW: loading model {current_model_name}")
    model = await loop.run_in_executor(
        None,
        lambda: OWWModel(
            wakeword_models=[current_model_name],
            enable_speex_noise_suppression=False,
        ),
    )
    model_key = current_model_name

    log.info(f"[{device.device_id}] OWW: starting (initial threshold={device.oww_threshold:.3f})")
    await device.mic_start()

    buf = bytearray()
    try:
        while True:
            if device.oww_model != current_model_name:
                new_name = device.oww_model
                log.info(
                    f"[{device.device_id}] OWW: reloading model "
                    f"{current_model_name} → {new_name}"
                )
                try:
                    _n = new_name
                    new_model = await loop.run_in_executor(
                        None,
                        lambda: OWWModel(
                            wakeword_models=[_n],
                            enable_speex_noise_suppression=False,
                        ),
                    )
                    model             = new_model
                    model_key         = new_name
                    current_model_name = new_name
                    buf.clear()
                    log.info(f"[{device.device_id}] OWW: model reloaded → {new_name}")
                except Exception as e:
                    log.error(
                        f"[{device.device_id}] OWW: failed to load {new_name}: {e} "
                        f"— reverting to {current_model_name}"
                    )
                    device.oww_model = current_model_name
            try:
                payload = await asyncio.wait_for(
                    device.mic_queue.get(), timeout=10.0
                )
            except asyncio.TimeoutError:
                log.warning(f"[{device.device_id}] OWW: mic queue timeout")
                continue

            if payload is None:
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

                prediction = await loop.run_in_executor(
                    None, model.predict, samples
                )
                score = prediction.get(model_key, 0.0)

                # Log any score above noise floor so we can see near-misses
                # and understand whether failed wakes are "close but below
                # threshold" vs "not registering at all". Only at DEBUG to
                # avoid flooding logs during normal idle operation.
                if score > 0.05:
                    log.debug(
                        f"[{device.device_id}] OWW score: {score:.3f} "
                        f"(threshold={device.oww_threshold:.3f})"
                    )

                if score >= device.oww_threshold:
                    log.info(
                        f"[{device.device_id}] Wake word detected "
                        f"(score={score:.3f})"
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
                        # preroll discard in _stream_mic_audio (esphome path)
                        # and the claracore stream_mic equivalent.
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
                        await _run_voice_locked(device, trigger_label=f"wakeword({score:.3f})")

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
            if VOICE_MODE == "esphome":
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
                await _run_voice_locked(device, trigger_label="button")
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                # Post-turn: back to ch6 omni for OWW listening.
                await device.mic_start()
            asyncio.create_task(_button_voice_turn())


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
        device.eq_bands      = config.get("eqBands", [0.0] * 8)
        device.eq_loudness   = bool(config.get("eqLoudness", False))
        # Initialise volume from stored config — device will report its real
        # value via volume_state on connect, but this seeds a sane default
        # in the window before that first message arrives.
        device.volume = _device_level_to_ha(
            int(config.get("startupVolume", 85))
        )
        log.info(f"[control] Config pushed to {device_id} (volume={device.volume:.3f})")

        await leds_off(device)
        await api.notify_device_connected(device_id)
        if VOICE_MODE == "esphome":
            _device_ref = device
            async def _standalone_play(pcm_bytes: bytes, _d=_device_ref) -> None:
                await _run_post_turn_playback(_d, pcm_bytes)
            async def _send_volume_set(level: int, _d=_device_ref) -> None:
                await _d.send_control({"type": "volume_set", "level": level})
            await esphome.device_connected(
                device_id,
                SERVER_HOST,
                standalone_play=_standalone_play,
                send_volume_set=_send_volume_set,
            )

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
                    if VOICE_MODE == "esphome":
                        esphome.update_device_volume(device_id, device.volume)

                elif msg_type == "stats":
                    device.stats = {
                        "cpuPct":        msg.get("cpuPct"),
                        "memUsedMb":     msg.get("memUsedMb"),
                        "memTotalMb":    msg.get("memTotalMb"),
                        "storageUsedMb": msg.get("storageUsedMb"),
                        "storageTotalMb":msg.get("storageTotalMb"),
                        "wifiRssi":      msg.get("wifiRssi"),
                    }
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {"stats": device.stats},
                    })

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
            if VOICE_MODE == "esphome":
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
                device.last_vad_was_timeout = (raw[MIC_HEADER_LEN] == VAD_NO_SPEECH_TIMEOUT_TYPE)
                q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
                if q.full():
                    try:
                        q.get_nowait()
                        log.warning(f"[{device.device_id}] queue full — dropped one frame to deliver VAD sentinel")
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    log.error(f"[{device.device_id}] VAD sentinel lost — queue still full after drain")
                continue
            payload = raw[MIC_HEADER_LEN:]
            try:
                if device.oww_paused.is_set():
                    device.voice_queue.put_nowait(payload)
                else:
                    device.mic_queue.put_nowait(payload)
            except asyncio.QueueFull:
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

    device_id = path.removeprefix("/shell/")
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id}")

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
    log.info(f"Voice mode: {VOICE_MODE}")
    if VOICE_MODE != "esphome":
        log.info(f"Voice server: {VOICE_WS_URI}")

    try:
        async with websockets.serve(
            router,
            SERVER_HOST,
            SERVER_PORT,
            ping_interval=20,
            ping_timeout=10,
            max_size=10 * 1024 * 1024,
        ):
            if VOICE_MODE == "esphome":
                await esphome.start_esphome_servers(_devices, SERVER_HOST)

            log.info("EchoMuse Controller ready — waiting for devices")
            await asyncio.Future()

    finally:
        if VOICE_MODE == "esphome":
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
