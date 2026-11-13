"""
Scenario harness: the real controller, driven from both ends.

The unit suite mocks Home Assistant and the Echo, so it agrees with our own
idea of each. Here the controller runs as its own process (`em_controller.py`,
as the add-on runs it) and both sides are as close to real as we can get
without hardware:

  - Home Assistant is aioesphomeapi, the library HA's ESPHome integration uses
    to talk to a satellite. HA's pipeline (STT, intent, TTS) is scripted: a
    scenario says which events HA sends, in what order, and when.
  - The Echo is FakeEcho, which speaks the wire contract in
    docs/device-controller-interface.md: register, listen_state, oww_wake,
    session audio on /data, playback_stats when a reply has drained.
  - TTS audio is a WAV served over HTTP, as HA's tts_proxy serves it.

A scenario asserts on what a person would notice: what the Echo was told to
show and play, and what was recorded about the turn.

Everything binds 127.0.0.1, mDNS included (em_hostip.bind_mdns), so a run
never advertises on the LAN.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from aiohttp import web
import websockets

CONTROLLER = Path(__file__).resolve().parents[1]
MIC_RATE = 16000
FRAME_S = 0.08                      # one 80 ms mic frame, as the Echo sends
FRAME_BYTES = int(MIC_RATE * FRAME_S) * 2


def free_port(below: int = 65536) -> int:
    """A port nothing holds now. `below`: the satellite base must be under
    60000 (em_db._esphome_port_base), and the kernel may hand out more."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port < below:
            return port
    raise RuntimeError(f"no free port below {below}")


def tone(seconds: float, rate: int, hz: float = 440.0, level: float = 0.3) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * hz * t) * level * 32767).astype("<i2").tobytes()


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def wait_for(predicate, timeout: float, what: str, step: float = 0.05):
    """Poll until predicate() is truthy; fail naming what never happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ── The controller ───────────────────────────────────────────────────────────

class Controller:
    """em_controller.py in its own process, on free ports, its own database."""

    def __init__(self, workdir: Path, **env):
        self.workdir = workdir
        self.db_path = workdir / "echomuse.db"
        self.port = free_port()
        self.api_port = free_port()
        self.esphome_base = free_port(below=59000)
        self.log_path = workdir / "controller.log"
        self.env = {
            **os.environ,
            "DB_PATH": str(self.db_path),
            "SERVER_IP": "127.0.0.1",
            "SERVER_HOST": "127.0.0.1",
            "SERVER_PORT": str(self.port),
            "SERVER_TLS_PORT": str(free_port()),
            "API_PORT": str(self.api_port),
            "EM_ESPHOME_PORT_BASE": str(self.esphome_base),
            "EM_SKIP_DB_BACKUP": "1",
            "PYTHONUNBUFFERED": "1",
            **env,
        }
        self.proc: subprocess.Popen | None = None

    async def start(self) -> None:
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "em_controller.py"], cwd=CONTROLLER,
            env=self.env, stdout=self._log, stderr=subprocess.STDOUT)
        await wait_for(lambda: self.proc.poll() is not None or
                       "Controller ready" in self.log(), 30, "the controller to start")
        assert self.proc.poll() is None, "controller exited at startup:\n" + self.log()[-3000:]
        # Admit the scenario's Echoes without an admin. Set in the database:
        # the stored value always wins over DEVICE_APPROVAL, which is seeded
        # 'strict' by the first migration (found by this harness, 2026-11-12).
        self.execute("UPDATE system_config SET value = 'auto' WHERE key = 'device_approval'")

    def execute(self, sql: str, *args) -> None:
        con = sqlite3.connect(self.db_path, timeout=5)
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if getattr(self, "_log", None):
            self._log.close()

    def query(self, sql: str, *args) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def esphome_port(self, device_id: str) -> int | None:
        rows = self.query("SELECT esphome_api_port FROM devices WHERE device_id = ?", device_id)
        return rows[0][0] if rows else None

    def turns(self, device_id: str) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT * FROM turns WHERE device_id = ? ORDER BY id", device_id)]


# ── The Echo ─────────────────────────────────────────────────────────────────

# What current firmware announces on an Echo Dot 2 under emOS, less the radio
# and Sendspin features no scenario here drives.
ECHO_CAPS = ["mic", "speaker", "leds", "led_anim", "buttons", "oww_shadow",
             "oww_trigger", "button_hold", "audio_mix", "oww_local_only",
             "output_chain", "wake_cue", "volume_cue", "response_level",
             "wake_word_off"]


@dataclass
class FakeEcho:
    """One Echo on the wire: /control JSON and /data binary."""

    controller: Controller
    device_id: str = "G0K0SCEN0001"
    capabilities: list[str] = field(default_factory=lambda: list(ECHO_CAPS))
    control: list[dict] = field(default_factory=list)   # everything the controller sent
    voice: bytearray = field(default_factory=bytearray)  # 0x02 payloads, in order
    voice_eos: int = 0                                   # 0x03 count
    features: list[str] = field(default_factory=list)
    _session: int = 0
    max_open_hit: bool = False

    async def connect(self) -> None:
        url = f"ws://127.0.0.1:{self.controller.port}"
        self._ctl = await websockets.connect(f"{url}/control", max_size=None)
        await self._ctl.send(json.dumps({
            "type": "register", "device_id": self.device_id, "ip": "127.0.0.1",
            "version": "v9.9.9-scenario", "capabilities": self.capabilities,
            "board": "biscuit", "base_os": "emos"}))
        ack = json.loads(await asyncio.wait_for(self._ctl.recv(), 10))
        assert ack.get("type") == "ack", f"not admitted: {ack}"
        self.features = ack.get("features", [])
        self._data = await websockets.connect(f"{url}/data", max_size=None)
        # As device/internal/client/data.go: identify before anything else.
        await self._data.send(json.dumps({"type": "identify", "device_id": self.device_id}))
        self._tasks = [asyncio.create_task(self._read_control()),
                       asyncio.create_task(self._read_data(self._data))]
        await self.send({"type": "listen_state",
                         "state": "local" if "listen_session" in self.features else "stream"})

    async def close(self) -> None:
        for t in getattr(self, "_tasks", []):
            t.cancel()
        for ws in (getattr(self, "_data", None), getattr(self, "_ctl", None)):
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()

    async def send(self, msg: dict) -> None:
        await self._ctl.send(json.dumps(msg))

    async def _read_control(self) -> None:
        async for raw in self._ctl:
            msg = json.loads(raw)
            self.control.append(msg)
            if msg.get("type") == "ping":
                reply = {"type": "pong"}
                if "id" in msg:
                    reply.update(id=msg["id"], mono=int(time.monotonic() * 1000))
                await self.send(reply)

    async def redial_data(self):
        """Open a second /data connection, as an Echo that lost power mid-
        stream does before the controller notices the first is dead. The old
        socket is returned, left open from this end."""
        old = self._data
        self._data = await websockets.connect(
            f"ws://127.0.0.1:{self.controller.port}/data", max_size=None)
        await self._data.send(json.dumps({"type": "identify", "device_id": self.device_id}))
        self._tasks.append(asyncio.create_task(self._read_data(self._data)))
        return old

    async def _read_data(self, ws) -> None:
        async for raw in ws:
            if not isinstance(raw, bytes) or not raw:
                continue
            if raw[0] == 0x02:
                self.voice += raw[1:]
            elif raw[0] == 0x03:
                self.voice_eos += 1
                # The Echo reports a stream once it has drained; the
                # controller's turn waits on it to stop "speaking".
                await self.send({"type": "playback_stats", "periods": 1,
                                 "underruns": 0, "stats": {}})

    def sent(self, kind: str) -> list[dict]:
        return [m for m in self.control if m.get("type") == kind]

    def closed(self, sid: int) -> bool:
        return any(m.get("session") == sid for m in self.sent("listen_close"))

    async def wake(self, speech: bytes, score: float = 0.9,
                   max_open: float = 30.0, **extra) -> int:
        """Say the wake word, then `speech` (16 kHz PCM), as a session.

        As the firmware does: the session streams until the controller closes
        it or `max_open` passes (the Echo's own limit), so after the speech
        the room is silent but frames keep coming. Returns once the speech
        has been sent; the silence streams in the background. `extra` goes
        on the oww_wake (level, peak, barge).
        """
        self._session += 1
        sid = self._session
        await self.send({"type": "oww_wake", "score": score, "threshold": 0.5,
                         "ageMs": 0, "session": sid, "barge": False,
                         # RMS 0..1, as listen.Gate.Floor(): a quiet room.
                         "floor": 0.003, **extra})
        frames = [speech[i:i + FRAME_BYTES] for i in range(0, len(speech), FRAME_BYTES)]
        done = asyncio.Event()

        async def stream():
            seq, t0 = 0, time.monotonic()
            silence = bytes(FRAME_BYTES)
            while not self.closed(sid):
                if time.monotonic() - t0 > max_open:
                    await self.send({"type": "listen_end", "session": sid,
                                     "reason": "max_open"})
                    self.max_open_hit = True
                    break
                frame = frames[seq] if seq < len(frames) else silence
                if seq == len(frames):
                    done.set()
                await self._data.send(bytes([0x07]) + struct.pack(">IH", sid, seq & 0xFFFF) + frame)
                seq += 1
                await asyncio.sleep(FRAME_S)
            done.set()

        self._tasks.append(asyncio.create_task(stream()))
        await done.wait()
        return sid


# ── Home Assistant ───────────────────────────────────────────────────────────

class TtsServer:
    """Serves TTS audio as HA's tts_proxy does: GET a URL, get the file."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fetched: list[str] = []

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/tts/{name}", self._get)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self.port = free_port()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        await self._runner.cleanup()

    def add(self, name: str, body: bytes) -> str:
        self.files[name] = body
        return f"http://127.0.0.1:{self.port}/tts/{name}"

    async def _get(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        self.fetched.append(name)
        if name not in self.files:
            raise web.HTTPNotFound()
        return web.Response(body=self.files[name], content_type="audio/wav")


class FakeHA:
    """Home Assistant's ESPHome integration, as far as a voice turn goes.

    Connects with aioesphomeapi and answers the satellite's run requests with
    a scripted pipeline. `pipeline` is a coroutine function given this object
    once the satellite asks for a run; it sends events with `event()`.
    """

    def __init__(self, port: int):
        self.port = port
        self.audio = bytearray()
        self.starts: list[dict] = []
        self.stops: list[bool] = []
        self.announce_finished = 0
        self.pipeline = None
        self._run: asyncio.Task | None = None

    async def connect(self) -> None:
        from aioesphomeapi import APIClient
        self.client = APIClient("127.0.0.1", self.port, None,
                                client_info="Home Assistant (scenario)")
        await self.client.connect(login=True)
        self.device_info = await self.client.device_info()
        self.entities, _ = await self.client.list_entities_services()
        self.client.subscribe_states(lambda state: None)
        self.client.subscribe_voice_assistant(
            handle_start=self._start, handle_stop=self._stop,
            handle_audio=self._audio,
            handle_announcement_finished=self._announced)

    async def disconnect(self) -> None:
        if self._run:
            self._run.cancel()
        with contextlib.suppress(Exception):
            await self.client.disconnect()

    async def _start(self, conversation_id, flags, audio_settings, wake_word_phrase):
        self.starts.append({"conversation_id": conversation_id, "flags": flags,
                            "wake_word_phrase": wake_word_phrase})
        if self.pipeline is not None:
            self._run = asyncio.create_task(self.pipeline(self))
        return 0        # 0: audio over the API connection, as HA does

    async def _stop(self, abort: bool) -> None:
        self.stops.append(abort)

    async def _audio(self, data: bytes) -> None:
        self.audio += data

    async def _announced(self, msg) -> None:
        self.announce_finished += 1

    def event(self, kind: str, data: dict | None = None) -> None:
        from aioesphomeapi import VoiceAssistantEventType as E
        self.client.send_voice_assistant_event(
            getattr(E, f"VOICE_ASSISTANT_{kind}"), data)

    async def answer(self, text: str, reply_text: str, tts_url: str,
                     hear_s: float = 0.6) -> None:
        """HA's pipeline for an ordinary question: hear it, answer it."""
        self.event("RUN_START")
        self.event("STT_START")
        await wait_for(lambda: len(self.audio) > 0, 5, "mic audio to reach HA")
        self.event("STT_VAD_START")
        await asyncio.sleep(hear_s)
        self.event("STT_VAD_END")
        self.event("STT_END", {"text": text})
        self.event("INTENT_START")
        self.event("INTENT_END", {"conversation_id": "c1"})
        self.event("TTS_START", {"text": reply_text})
        self.event("TTS_END", {"url": tts_url})
        self.event("RUN_END")
