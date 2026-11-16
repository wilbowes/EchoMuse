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
  - Given an `fs_root`, the Echo also answers the shell plane with a real sh
    on that scratch /data, so a firmware update runs end to end (test_ota.py).

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
import random
import re
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


# Every port handed out in this process, and what for. bind(0) only promises
# the port is free NOW, and nothing binds it until later: the satellite base
# not until an Echo registers, so a later pick (the TTS server) could land on
# it and HA dialled an HTTP server ("Invalid preamble 48": 0x48 is 'H',
# 2026-11-16).
_handed_out: dict[int, str] = {}

# The satellite range a scenario can reach: one listener per Echo from the
# base, BLE proxies BLE_PORT_OFFSET (1000) above (em_db).
SATELLITE_SPAN = tuple(range(9)) + tuple(range(1000, 1009))


def _bindable(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(what: str = "test") -> int:
    """A port nothing holds now and nothing in this run has been given.
    bind(0) can return the same port twice running: the controller was once
    given one for both its API and /control (2026-11-16)."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _handed_out:
            _handed_out[port] = what
            return port
    raise RuntimeError("no free port")


def satellite_base() -> int:
    """A base for the controller's satellite listeners, with the whole
    SATELLITE_SPAN free, below the kernel's ephemeral range. Inside it, an
    outgoing connection made after the pick (the fake Echo's, the
    controller's own) can take base+1 before the second Echo's listener
    binds it; below it, the kernel never hands a port out unasked."""
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            low = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        low = 32768
    top = min(low, 59000) - max(SATELLITE_SPAN) - 1
    for _ in range(200):
        base = random.randrange(10000, top)
        ports = [base + o for o in SATELLITE_SPAN]
        if any(p in _handed_out for p in ports) or not all(map(_bindable, ports)):
            continue
        for p in ports:
            _handed_out[p] = "satellites"
        return base
    raise RuntimeError("no free satellite range")


def port_owner(port: int) -> str:
    """What this run handed `port` out for, for a failure message."""
    return _handed_out.get(port, "nothing in this run")


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
        self.port = free_port(what="controller /control")
        self.api_port = free_port(what="controller API")
        self.esphome_base = satellite_base()
        self.log_path = workdir / "controller.log"
        self.env = {
            **os.environ,
            "DB_PATH": str(self.db_path),
            "SERVER_IP": "127.0.0.1",
            "SERVER_HOST": "127.0.0.1",
            "SERVER_PORT": str(self.port),
            "SERVER_TLS_PORT": str(free_port(what="controller TLS")),
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

    def admin_token(self) -> str:
        """A session for an admin, written straight into the database: the
        API checks the token against `sessions`, and a scenario has no
        browser to sign in with."""
        token = "scenario-admin-" + os.urandom(8).hex()
        now = int(time.time())
        self.execute("INSERT OR IGNORE INTO users (username, password_hash, role, created_at) "
                     "VALUES ('scenario-admin', '!', 'admin', ?)", now)
        uid = self.query("SELECT id FROM users WHERE username = 'scenario-admin'")[0][0]
        self.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?)", token, uid, now, now + 3600)
        return token

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
    version: str = "v9.9.9-scenario"
    # The Echo's /data, for scenarios that drive the shell plane (an OTA).
    # None: shell_open is ignored, as before.
    fs_root: Path | None = None
    restarts: int = 0

    async def connect(self) -> None:
        url = f"ws://127.0.0.1:{self.controller.port}"
        self._ctl = await websockets.connect(f"{url}/control", max_size=None)
        await self._ctl.send(json.dumps({
            "type": "register", "device_id": self.device_id, "ip": "127.0.0.1",
            "version": self.version, "capabilities": self.capabilities,
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
            if msg.get("type") == "shell_open" and self.fs_root is not None:
                self._tasks.append(asyncio.create_task(self._shell_session()))
            if msg.get("type") == "ping":
                reply = {"type": "pong"}
                if "id" in msg:
                    reply.update(id=msg["id"], mono=int(time.monotonic() * 1000))
                await self.send(reply)

    # ── The shell plane ──────────────────────────────────────────────────
    #
    # As device/internal/client/control.go runShellSession in pipe mode: on
    # shell_open the Echo dials /shell/{id} and pipes a fresh sh's stdin and
    # merged stdout/stderr through it, binary frames out. Here the sh is real
    # (`sh -c 'sh; true'`, so the inner sh's $PPID is a process standing in
    # for the firmware: `kill $PPID` is the OTA's restart, and its death is
    # this Echo restarting). Paths under /data are mapped onto fs_root both
    # ways, and `busybox <tool>` runs the host's tool.

    def _to_host(self, text: str) -> str:
        return re.sub(r"(?<![\w/.])/data(?=[/\s;|&)\"']|$)", f"{self.fs_root}/data", text)

    def _shims(self) -> Path:
        d = self.fs_root / ".shims"
        if not d.exists():
            d.mkdir(parents=True)
            bb = d / "busybox"
            bb.write_text('#!/bin/sh\nexec "$@"\n')
            bb.chmod(0o755)
        return d

    async def _shell_session(self) -> None:
        ws = await websockets.connect(
            f"ws://127.0.0.1:{self.controller.port}/shell/{self.device_id}", max_size=None)
        env = {**os.environ, "PATH": f"{self._shims()}:{os.environ.get('PATH', '')}"}
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "sh; true", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=self.fs_root, env=env)
        root = str(self.fs_root)

        async def pump_out():
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                with contextlib.suppress(Exception):
                    await ws.send(chunk.replace(root.encode(), b""))

        async def pump_in():
            with contextlib.suppress(Exception):
                async for raw in ws:
                    text = raw.decode() if isinstance(raw, bytes) else raw
                    proc.stdin.write(self._to_host(text).encode())
                    await proc.stdin.drain()
            with contextlib.suppress(Exception):
                proc.stdin.close()

        out, inp = asyncio.create_task(pump_out()), asyncio.create_task(pump_in())
        rc = await proc.wait()
        await out
        inp.cancel()
        with contextlib.suppress(Exception):
            await ws.close()
        if rc < 0:  # the stand-in firmware was killed: restart
            self._tasks.append(asyncio.create_task(self._restart()))

    def running_binary(self) -> bytes:
        return (self.fs_root / "data/local/bin/server").read_bytes()

    async def _restart(self) -> None:
        """The firmware restarted: drop every connection and register again
        as whatever binary the server symlink now points at, as
        start_server.sh does."""
        self.restarts += 1
        for ws in (getattr(self, "_data", None), getattr(self, "_ctl", None)):
            with contextlib.suppress(Exception):
                await ws.close()
        await asyncio.sleep(0.5)
        m = re.search(rb"v\d+\.\d+\.\d+-\d+-g[0-9a-f]{7,40}", self.running_binary())
        if m:
            self.version = m.group(0).decode()
        await self.connect()

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

    def rings_off(self) -> int:
        """How many times the controller has turned the ring off: the end of
        a turn as a person sees it, and the earliest a second wake is taken
        (one sooner is declined as busy, correctly)."""
        return sum(1 for m in self.sent("led_anim")
                   if (m.get("anim") or {}).get("pattern") == "off")

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
        self.port = free_port(what="TTS server")
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
