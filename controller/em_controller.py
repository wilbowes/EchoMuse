"""
EchoMuse Controller
===================
WebSocket server. Echo Dot devices connect via mDNS discovery.
mDNS advertisement is handled internally — no separate container required.

Architecture:
  - Advertise _emcontroller._tcp.local on SERVER_PORT (zeroconf, host network)
  - Devices open TWO connections:
      /control  — JSON control plane (buttons, LEDs, mic_start/stop, ping)
      /data     — binary data plane (mic PCM frames in, speaker PCM frames out)
  - Both connections are associated by device_id on connect

Device WebSocket protocol:

  /control — Device → Server:
    {"type": "register", "device_id": "echo-dot", "ip": "...", "capabilities": [...]}
    {"type": "button", "clickType": 138, "down": false}
    {"type": "pong"}

  /control — Server → Device:
    {"type": "ack", "device_id": "echo-dot"}
    {"type": "leds", "leds": [...]}
    {"type": "mic_start"}
    {"type": "mic_stop"}
    {"type": "ping"}

  /data — Device → Server:
    <binary> [0x01][seq_hi][seq_lo][PCM mono S16_LE 2560 bytes]

  /data — Server → Device:
    <binary> [0x02][PCM stereo S16_LE 48kHz — 8192 bytes per period]
    <binary> [0x03] end of audio stream
"""

import asyncio
import json
import logging
import os
import socket
import struct

import numpy as np
from openwakeword.model import Model as OWWModel
from zeroconf import ServiceInfo, Zeroconf
from zeroconf.asyncio import AsyncZeroconf
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("echomuse")

# ─── Config ───────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
SERVER_IP    = os.environ.get("SERVER_IP", "10.10.1.236")
VOICE_WS_URI = os.environ.get("VOICE_WS_URI", "ws://clara-voice:8765")
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")

# Mic
CHUNK_BYTES           = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
VOICE_PREROLL_DISCARD = 4

# Speaker — must match PcmSpeaker constants in Go
SPEAKER_RATE   = 48000
SPEAKER_PERIOD = 2048
SPEAKER_BYTES  = SPEAKER_PERIOD * 2 * 2  # 8192 bytes/period
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
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3  # [type][seq_hi][seq_lo]

# ─── Device Registry ──────────────────────────────────────────────────

class Device:
    def __init__(self, device_id: str, ip: str, capabilities: list,
                 control_ws: WebSocketServerProtocol):
        self.device_id    = device_id
        self.ip           = ip
        self.capabilities = capabilities
        self.control_ws   = control_ws
        self.data_ws: WebSocketServerProtocol | None = None
        self.voice_lock   = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.speaking     = False  # True while speaker is streaming — suppresses OWW
        self.data_ready   = asyncio.Event()

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

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

    async def stream_speaker(self, pcm: bytes):
        """Stream resampled stereo 48kHz PCM as 0x02 binary frames, then 0x03 EOS."""
        self.speaking = True
        try:
            offset = 0
            while offset + SPEAKER_BYTES <= len(pcm):
                if self.cancel_event.is_set():
                    break
                period = pcm[offset:offset + SPEAKER_BYTES]
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + period)
                offset += SPEAKER_BYTES
            await self.send_data(bytes([SPEAKER_EOS_TYPE]))
        finally:
            self.speaking = False


_devices: dict[str, Device] = {}


def get_device(device_id: str) -> Device | None:
    return _devices.get(device_id)


# ─── LED helpers ──────────────────────────────────────────────────────

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


# ─── Audio conversion ─────────────────────────────────────────────────

def resample_to_stereo_48k(pcm: bytes, from_rate: int) -> bytes:
    if len(pcm) < 2:
        return b""
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    n_in  = len(samples)
    n_out = int(n_in * SPEAKER_RATE / from_rate)
    out = []
    for i in range(n_out):
        src  = i * from_rate / SPEAKER_RATE
        lo   = int(src)
        hi   = min(lo + 1, n_in - 1)
        frac = src - lo
        val  = int(samples[lo] * (1 - frac) + samples[hi] * frac)
        val  = max(-32768, min(32767, val))
        out.append(val)
        out.append(val)
    return struct.pack(f"<{len(out)}h", *out)


# ─── Voice pipeline ───────────────────────────────────────────────────

async def run_voice_turn(device: Device):
    log.info(f"[{device.device_id}] Voice turn starting")
    await leds_listening(device)

    voice_response: bytes = b""
    stop_spin = asyncio.Event()
    spin_task = None

    async def cleanup():
        stop_spin.set()
        if spin_task and not spin_task.done():
            spin_task.cancel()
            try:
                await spin_task
            except asyncio.CancelledError:
                pass
        await leds_off(device)

    try:
        async with websockets.connect(VOICE_WS_URI, max_size=10 * 1024 * 1024) as ws:
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
                                device.mic_queue.get(), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            continue
                        pcm_buf.extend(payload)
                        while len(pcm_buf) >= CHUNK_BYTES:
                            chunk = bytes(pcm_buf[:CHUNK_BYTES])
                            del pcm_buf[:CHUNK_BYTES]
                            await ws.send(chunk)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.error(f"[{device.device_id}] Mic stream error: {e}")

            async def receive_response():
                nonlocal voice_response, spin_task
                try:
                    async for message in ws:
                        if isinstance(message, str) and message == "THINKING":
                            log.info(f"[{device.device_id}] Transcribing — starting spinner")
                            if not device.cancel_event.is_set() and (spin_task is None or spin_task.done()):
                                spin_task = asyncio.create_task(leds_spin_green(device, stop_spin))
                        elif isinstance(message, bytes) and message:
                            voice_response = message
                            log.info(f"[{device.device_id}] Received {len(voice_response)} bytes audio")
                            return
                except Exception as e:
                    log.error(f"[{device.device_id}] WS receive error: {e}")

            await device.mic_start()

            mic_task     = asyncio.create_task(stream_mic())
            receive_task = asyncio.create_task(receive_response())
            cancel_task  = asyncio.create_task(device.cancel_event.wait())

            done, _ = await asyncio.wait(
                [receive_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

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
        log.info(f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes to speaker ({len(speaker_pcm)//SPEAKER_BYTES} periods)")

        cancel_task = asyncio.create_task(device.cancel_event.wait())
        stream_task = asyncio.create_task(device.stream_speaker(speaker_pcm))

        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during playback")
            stream_task.cancel()
        cancel_task.cancel()

    except Exception as e:
        log.error(f"[{device.device_id}] Speaker failed: {e}")
    finally:
        await cleanup()

    log.info(f"[{device.device_id}] Voice turn complete")


async def _run_voice_locked(device: Device):
    device.cancel_event.clear()
    async with device.voice_lock:
        await run_voice_turn(device)


# ─── Wake word listener ────────────────────────────────────────────────

_oww_model: OWWModel | None = None

def _get_oww_model() -> OWWModel:
    global _oww_model
    if _oww_model is None:
        log.info(f"Loading OpenWakeWord model: {OWW_MODEL}")
        _oww_model = OWWModel(
            wakeword_model_paths=[
                f"/usr/local/lib/python3.12/site-packages/openwakeword/resources/models/{OWW_MODEL}_v0.1.onnx"
            ],
            enable_speex_noise_suppression=False,
        )
        log.info("OpenWakeWord model ready")
    return _oww_model


async def wake_word_listener(device: Device):
    loop      = asyncio.get_event_loop()
    model     = await loop.run_in_executor(None, _get_oww_model)
    model_key = f"{OWW_MODEL}_v0.1"

    log.info(f"[{device.device_id}] OWW: starting mic stream")
    await device.mic_start()

    buf = bytearray()

    try:
        while True:
            try:
                payload = await asyncio.wait_for(device.mic_queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning(f"[{device.device_id}] OWW: mic queue timeout")
                continue

            buf.extend(payload)

            while len(buf) >= CHUNK_BYTES:
                frame = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]

                samples = np.frombuffer(frame, dtype=np.int16)

                if device.speaking:
                    continue  # discard — don't feed speaker output to OWW

                prediction = await loop.run_in_executor(None, model.predict, samples)
                score      = prediction.get(model_key, 0.0)

                if score >= OWW_THRESHOLD:
                    log.info(f"[{device.device_id}] Wake word detected (score={score:.3f})")

                    if not device.voice_lock.locked():
                        await device.mic_stop()
                        model.reset()
                        buf.clear()

                        await _run_voice_locked(device)

                        # Drain stale frames accumulated during voice turn
                        drained = 0
                        while not device.mic_queue.empty():
                            try:
                                device.mic_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        if drained:
                            log.info(f"[{device.device_id}] OWW: drained {drained} stale frames")
                        model.reset()
                        buf.clear()

                        log.info(f"[{device.device_id}] OWW: restarting mic stream")
                        await device.mic_start()
                    else:
                        log.info(f"[{device.device_id}] Voice turn already active — ignoring wake")
                        model.reset()

    except asyncio.CancelledError:
        await device.mic_stop()
        raise


# ─── Button handler ───────────────────────────────────────────────────

async def handle_button_event(device: Device, event: dict):
    click_type = event.get("clickType")
    down       = event.get("down", True)
    if down:
        return
    if click_type == 138:  # DotClick
        if device.voice_lock.locked():
            log.info(f"[{device.device_id}] Dot button — cancelling voice turn")
            device.cancel_event.set()
        else:
            log.info(f"[{device.device_id}] Dot button → voice turn")
            asyncio.create_task(_run_voice_locked(device))


# ─── Control plane handler ────────────────────────────────────────────

async def handle_control(ws: WebSocketServerProtocol):
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "register":
            log.warning(f"[control] First message from {remote} was not register — closing")
            await ws.close()
            return

        device_id    = msg["device_id"]
        ip           = msg.get("ip", str(remote[0]))
        capabilities = msg.get("capabilities", [])

        device = Device(device_id, ip, capabilities, ws)
        _devices[device_id] = device

        log.info(f"[control] Device registered: {device_id} at {ip} caps={capabilities}")
        await device.send_control({"type": "ack", "device_id": device_id})
        await leds_off(device)

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
                elif msg_type == "pong":
                    pass
                else:
                    log.debug(f"[{device_id}] Unknown control message: {msg_type}")

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
            _devices.pop(device.device_id, None)


# ─── Data plane handler ───────────────────────────────────────────────

async def handle_data(ws: WebSocketServerProtocol):
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "identify":
            log.warning(f"[data] First message from {remote} was not identify — closing")
            await ws.close()
            return

        device_id = msg["device_id"]

        for _ in range(20):  # up to 2 seconds
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
        log.info(f"[data] Data connection established for {device_id}")

        async for raw in ws:
            if not isinstance(raw, bytes):
                continue
            if len(raw) <= MIC_HEADER_LEN:
                continue
            if raw[0] != MIC_FRAME_TYPE:
                continue
            payload = raw[MIC_HEADER_LEN:]
            try:
                device.mic_queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # OWW fell behind — drop frame

    except asyncio.TimeoutError:
        log.warning(f"[data] Identify timeout from {remote}")
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"[data] Handler error: {e}")
    finally:
        if device:
            device.data_ws = None
            device.data_ready.clear()
            log.info(f"[data] Data connection closed for {device.device_id}")


# ─── Router ───────────────────────────────────────────────────────────

async def router(ws: WebSocketServerProtocol):
    path = ws.request.path if hasattr(ws, 'request') else getattr(ws, 'path', '/')
    if path == "/control":
        await handle_control(ws)
    elif path == "/data":
        await handle_data(ws)
    else:
        log.warning(f"Unknown path: {path} from {ws.remote_address}")
        await ws.close()


# ─── mDNS ─────────────────────────────────────────────────────────────

def _make_mdns_info() -> ServiceInfo:
    return ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=SERVER_PORT,
        properties={"version": "1", "server": MDNS_NAME},
        server=f"{MDNS_NAME}.local.",
    )


async def mdns_refresh_loop(azc: AsyncZeroconf, info: ServiceInfo):
    while True:
        await asyncio.sleep(MDNS_REFRESH_INTERVAL)
        await azc.async_unregister_service(info)
        await azc.async_register_service(info, allow_name_change=True)
        log.info("mDNS re-registered (IGMP refresh)")

# ─── Main ─────────────────────────────────────────────────────────────

async def main():
    azc  = AsyncZeroconf()
    info = _make_mdns_info()
    await azc.async_register_service(info, allow_name_change=True)
    log.info(f"mDNS advertising {MDNS_NAME}._emcontroller._tcp.local → {SERVER_IP}:{SERVER_PORT}")

    mdns_task = asyncio.create_task(mdns_refresh_loop(azc.zeroconf, info))

    log.info(f"EchoMuse Controller starting on {SERVER_HOST}:{SERVER_PORT}")
    log.info(f"Voice server: {VOICE_WS_URI}")

    try:
        async with websockets.serve(
            router,
            SERVER_HOST,
            SERVER_PORT,
            ping_interval=None,
            max_size=10 * 1024 * 1024,
        ):
            log.info("Waiting for devices...")
            await asyncio.Future()
    finally:
        mdns_task.cancel()
        await azc.async_unregister_service(info)
        await azc.async_close()
        log.info("mDNS stopped")

if __name__ == "__main__":
    asyncio.run(main())
