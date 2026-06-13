"""
EchoMuse Controller
===================

WebSocket server. Echo Dot devices connect via mDNS discovery.

mDNS advertisement is handled internally — no separate container required.

Architecture:
- Advertise _emcontroller._tcp.local on SERVER_PORT (zeroconf, host network)
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

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")

# Mic
CHUNK_BYTES          = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
VOICE_PREROLL_DISCARD = 4

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
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3   # [type][seq_hi][seq_lo]

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

        self.data_ready = asyncio.Event()

        # Tunable at runtime — updated when a config push arrives.
        # wake_word_listener reads this each detection cycle rather than
        # caching a snapshot at startup, so config changes take effect
        # without requiring a device reconnect.
        self.oww_threshold: float = OWW_THRESHOLD

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
# _shell_pending:   Future resolved by handle_shell when proxying is complete.
# _shell_dashboard: dashboard WebSocket, set by em_api before shell_open.
_shell_pending:   dict[str, asyncio.Future] = {}
_shell_dashboard: dict[str, object]         = {}


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

async def run_voice_turn(device: Device):
    log.info(f"[{device.device_id}] Voice turn starting")
    device.listening = True
    await leds_listening(device)
    await _push_device_state(device)

    voice_response: bytes = b""
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
                nonlocal voice_response, spin_task
                try:
                    async for message in ws:
                        if isinstance(message, str) and message == "THINKING":
                            device.thinking  = True
                            device.listening = False
                            await _push_device_state(device)
                            log.info(f"[{device.device_id}] Thinking — starting spinner")
                            if not device.cancel_event.is_set() and (
                                spin_task is None or spin_task.done()
                            ):
                                spin_task = asyncio.create_task(
                                    leds_spin_green(device, stop_spin)
                                )
                        elif isinstance(message, bytes) and message:
                            voice_response = message
                            log.info(
                                f"[{device.device_id}] Received "
                                f"{len(voice_response)} bytes audio"
                            )
                            return
                except Exception as e:
                    log.error(f"[{device.device_id}] WS receive error: {e}")

            await device.mic_start_turn()
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
                await device.mic_stop()
                mic_task.cancel()
                await cleanup()
                return

            await device.mic_stop()
            mic_task.cancel()

            if cancel_task in done:
                log.info(f"[{device.device_id}] Voice turn cancelled")
                receive_task.cancel()
                await cleanup()
                return

            cancel_task.cancel()
            try:
                await mic_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        log.error(f"[{device.device_id}] Voice turn WS failed: {e}")
        await cleanup()
        return

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
        speaker_pcm = resample_to_stereo_48k(voice_response, PIPER_RATE)
        log.info(
            f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes "
            f"({len(speaker_pcm)//SPEAKER_BYTES} periods)"
        )
        cancel_task  = asyncio.create_task(device.cancel_event.wait())
        stream_task  = asyncio.create_task(device.stream_speaker(speaker_pcm))

        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during playback")
            stream_task.cancel()
        else:
            # Wait for device audio buffer to drain before restarting mic.
            # The device has ~341ms of buffered audio after the last frame
            # is sent (4 audioCh + 4 ALSA hw periods at 2048 samples/48kHz).
            # Without this wait, the mic restarts while the speaker is still
            # playing, causing acoustic feedback into the next voice turn.
            if not device.cancel_event.is_set():
                # Sleep for the actual audio duration so the spinner keeps
                # running until playback truly completes on the device.
                # stream_speaker completes as soon as frames are buffered,
                # not when they finish playing — hence this wait.
                audio_duration = len(speaker_pcm) / (SPEAKER_RATE * 4)  # stereo S16LE
                log.info(f"[{device.device_id}] Waiting {audio_duration:.1f}s for audio playback")
                await asyncio.sleep(audio_duration)
                log.info(f"[{device.device_id}] Playback complete")

        cancel_task.cancel()

    except Exception as e:
        log.error(f"[{device.device_id}] Speaker failed: {e}")
    finally:
        await cleanup()

    log.info(f"[{device.device_id}] Voice turn complete")


async def _run_voice_locked(device: Device):
    # oww_paused already set by caller (button handler or OWW trigger)
    # Drain any frames already in the queues — stale from OWW
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
            await run_voice_turn(device)
    finally:
        device.oww_paused.clear()
        log.info(f"[{device.device_id}] oww_paused cleared")


# ─── Wake word listener ───────────────────────────────────────────────────────

async def wake_word_listener(device: Device):
    loop      = asyncio.get_event_loop()

    # Each device gets its own model instance — the OWW model holds internal
    # streaming state (feature buffer, VAD history) that is not thread-safe and
    # must not be shared across devices. Models are small; the memory cost is
    # acceptable for a fleet of 5-6 devices.
    log.info(f"[{device.device_id}] OWW: loading model {OWW_MODEL}")
    model = await loop.run_in_executor(
        None,
        lambda: OWWModel(
            wakeword_models=[f"{OWW_MODEL}_v0.1"],
            enable_speex_noise_suppression=False,
        ),
    )
    model_key = f"{OWW_MODEL}_v0.1"

    log.info(f"[{device.device_id}] OWW: starting (initial threshold={device.oww_threshold:.3f})")
    await device.mic_start()

    buf = bytearray()
    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    device.mic_queue.get(), timeout=10.0
                )
            except asyncio.TimeoutError:
                log.warning(f"[{device.device_id}] OWW: mic queue timeout")
                continue

            if payload is None:
                # VAD end sentinel — discard, OWW handles its own state
                buf.clear()
                continue

            # During a voice turn the OWW loop stops consuming —
            # frames go into voice_queue instead (see data handler)
            if device.oww_paused.is_set():
                continue

            buf.extend(payload)
            while len(buf) >= CHUNK_BYTES:
                frame   = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)

                if device.speaking:
                    continue  # suppress OWW during speaker playback

                prediction = await loop.run_in_executor(
                    None, model.predict, samples
                )
                score = prediction.get(model_key, 0.0)

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
                        await device.mic_stop()
                        model.reset()
                        buf.clear()
                        device.cancel_event.clear()
                        device.oww_paused.set()
                        await _run_voice_locked(device)

                        # Drain stale frames accumulated during voice turn
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
                                f"drained {drained} stale frames"
                            )
                        model.reset()
                        buf.clear()
                        log.info(f"[{device.device_id}] OWW: restarting mic")
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
        else:
            log.info(f"[{device.device_id}] Dot button → voice turn")
            device.cancel_event.clear()
            device.oww_paused.set()
            async def _button_voice_turn():
                await _run_voice_locked(device)
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                await device.mic_start()
            asyncio.create_task(_button_voice_turn())


# ─── Control plane handler ────────────────────────────────────────────────────

async def handle_control(ws: WebSocketServerProtocol):
    """
    Handle a /control WebSocket connection from a device.

    Registration flow:
      1. Receive register message with device_id (ro.serialno) and version
      2. Look up device in DB
         - Not found → insert as pending, send {"type": "pending"}, close
         - Found, approved=false → send {"type": "pending"}, close
         - Found, approved=true → send ack + config, start pipeline
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

        # ── Device approval check ─────────────────────────────────────────────
        loop         = asyncio.get_event_loop()
        approval_mode = db.get_config("device_approval", DEVICE_APPROVAL)
        row          = await loop.run_in_executor(None, db.get_device, device_id)

        if row is None:
            if approval_mode == "auto":
                # Auto-approve with generated label
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
                # Strict mode — register as pending and reject
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
            # Known but not yet approved
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

        # ── Approved — proceed with normal registration ────────────────────
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

        # Send ack
        await device.send_control({"type": "ack", "device_id": device_id})

        # Push stored config immediately after ack
        config = await loop.run_in_executor(
            None, db.get_device_config, device_id
        )
        await device.send_control({"type": "config", **config})
        device.oww_threshold = float(config.get("owwThreshold", OWW_THRESHOLD))
        log.info(f"[control] Config pushed to {device_id}")

        await leds_off(device)
        await api.notify_device_connected(device_id)

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

                elif msg_type == "log":
                    # Device-side log entry — persist and push to dashboard
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
            # Identity check — a reconnect may have already replaced our entry.
            if _devices.get(device.device_id) is device:
                _devices.pop(device.device_id, None)
            await api.notify_device_disconnected(device.device_id)


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

        for _ in range(20):   # up to 2 seconds
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
            # VAD end sentinel: mic frame with single-byte payload 0x04
            if len(raw) == MIC_HEADER_LEN + 1 and raw[MIC_HEADER_LEN] == VAD_END_TYPE:
                try:
                    if device.oww_paused.is_set():
                        device.voice_queue.put_nowait(None)
                    else:
                        device.mic_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
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
            # Identity check — a reconnect may have replaced our data_ws already.
            if device.data_ws is ws:
                device.data_ws = None
                device.data_ready.clear()
            log.info(f"[data] Data connection closed: {device.device_id}")


# ─── Router ───────────────────────────────────────────────────────────────────



# ─── Shell plane handler ──────────────────────────────────────────────────────

async def handle_shell(ws: WebSocketServerProtocol, path: str):
    """
    Handle an inbound /shell/{device_id} WebSocket connection from the device.

    Owns all proxying between device shell and dashboard terminal.
    em_api just coordinates signalling via shared dicts.
    """
    import aiohttp as _aiohttp

    device_id = path.removeprefix("/shell/")
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id}")

    done_future  = _shell_pending.get(device_id)
    dashboard_ws = _shell_dashboard.get(device_id)

    if done_future is None or done_future.done() or dashboard_ws is None:
        log.warning(f"[shell] No pending shell request for {device_id} — closing")
        await ws.close()
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

    try:
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(device_to_dashboard()),
                asyncio.create_task(dashboard_to_device()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
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
    """
    Periodically re-register the mDNS service to keep IGMP multicast group
    membership alive on the LAN. Required when running behind a Proxmox bridge
    (or any managed switch that ages out multicast memberships). Without this,
    mDNS responses stop arriving at devices after MDNS_REFRESH_INTERVAL seconds
    of silence and discovery fails silently.
    """
    while True:
        await asyncio.sleep(MDNS_REFRESH_INTERVAL)
        try:
            await azc.async_update_service(info)
            log.debug("mDNS registration refreshed")
        except Exception as e:
            log.warning(f"mDNS refresh failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    # ── Database ──────────────────────────────────────────────────────────────
    db.init(DB_PATH)

    # ── Auth — generate bootstrap token if no users exist ─────────────────
    auth.maybe_generate_bootstrap_token()

    # ── API server ────────────────────────────────────────────────────────────
    runner = await api.create_runner(_devices, _shell_pending, _shell_dashboard)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, API_PORT)
    await site.start()
    log.info(f"Dashboard + API listening on http://{SERVER_HOST}:{API_PORT}")

    # ── Background tasks ──────────────────────────────────────────────────────
    release_task       = asyncio.create_task(api.release_poll_loop())
    session_prune_task = asyncio.create_task(api.session_prune_loop())

    # ── mDNS ──────────────────────────────────────────────────────────────────
    azc  = AsyncZeroconf()
    info = _make_mdns_info()
    await azc.async_register_service(info, allow_name_change=True)
    log.info(
        f"mDNS advertising {MDNS_NAME}._emcontroller._tcp.local "
        f"→ {SERVER_IP}:{SERVER_PORT}"
    )
    mdns_task = asyncio.create_task(_mdns_refresh_loop(azc, info))

    # ── WebSocket server ──────────────────────────────────────────────────────
    log.info(f"WebSocket server starting on {SERVER_HOST}:{SERVER_PORT}")
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
            log.info("EchoMuse Controller ready — waiting for devices")
            await asyncio.Future()

    finally:
        release_task.cancel()
        session_prune_task.cancel()
        mdns_task.cancel()
        await azc.async_unregister_service(info)
        await azc.async_close()
        await runner.cleanup()
        log.info("EchoMuse Controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
