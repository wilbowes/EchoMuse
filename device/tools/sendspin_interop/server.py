"""The server half of the Sendspin interop test: aiosendspin (run.sh picks the
version; Music Assistant 2.10 ships 10.2.1) driving the device's player
(main.go) through every path the device claims to support.

The sync scenarios pin the player to FLAC (main.go's -codecs default): they
read exact sawtooth values back, which a lossy codec cannot give.

Each scenario starts a fresh client process and fails loudly on the first
thing that does not happen. The sync check does not trust the client's own
error estimate: the client logs when each period reaches its simulated DAC on
CLOCK_MONOTONIC_RAW, which is the clock this server stamps audio with, and
the sawtooth signal says which sample was playing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import struct
import sys

from aiosendspin.audio.format import AudioFormat
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.pairing import PairingAttempt
try:  # 10.x names it for its version-0 format, which is unchanged
    from aiosendspin.noise.pairing_token import decode_psk_token as decode_token
except ImportError:  # 9.1.1
    from aiosendspin.noise.pairing_token import decode_token
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server.server import SendspinServer

RATE = 48000
SAW = 20000  # sawtooth period in samples; value = (k % SAW) - SAW/2
PORT = 18928
URL = f"ws://127.0.0.1:{PORT}/sendspin"
BIN = "/w/interop"

log = logging.getLogger("interop")


class Fail(Exception):
    pass


class Client:
    """The device player under test, as a subprocess speaking JSON lines."""

    def __init__(self, *args: str) -> None:
        self.args = args
        self.events: list[dict] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None

    async def start(self, store: str) -> dict:
        self.proc = await asyncio.create_subprocess_exec(
            BIN, "-port", str(PORT), "-store", store, *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._stderr())
        return await self.wait("ready", 10)

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        async for line in self.proc.stdout:
            try:
                self.events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    async def _stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            text = line.decode().rstrip()[20:]
            self.events.append({"event": "log", "line": text})
            print("   client:", text)

    async def wait(self, event: str, timeout: float, pred=lambda e: True) -> dict:
        start = len(self.events)
        for _ in range(int(timeout * 20)):
            for e in self.events[start:]:
                if e.get("event") == event and pred(e):
                    return e
            await asyncio.sleep(0.05)
        raise Fail(f"client never reported {event}")

    def status(self) -> dict:
        for e in reversed(self.events):
            if e.get("event") == "status":
                return e
        return {}

    def periods(self, since: int = 0) -> list[dict]:
        return [e for e in self.events[since:] if e.get("event") == "period"]

    async def command(self, cmd: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((cmd + "\n").encode())
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            await self.command("quit")
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()


def psk_attempt(cid: str, psk: bytes) -> PairingAttempt:
    """10.x names the client in the attempt; 9.1.1 has no such field."""
    try:
        return PairingAttempt(PairMethod.PAIRING_PSK, pairing_psk=psk, client_id=cid)
    except TypeError:
        return PairingAttempt(PairMethod.PAIRING_PSK, pairing_psk=psk)


def sawtooth(start: int, n: int) -> bytes:
    return struct.pack(f"<{n}h", *(((start + i) % SAW) - SAW // 2 for i in range(n)))


async def stream(server: SendspinServer, client_id: str, seconds: float, *, keep=False):
    """Stream the sawtooth to the client's group; returns (stream, start_us)."""
    group = server.get_client(client_id).group
    st = group.start_stream()
    fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=1)
    chunk = RATE // 50  # 20ms
    start_us = None
    k = 0
    while k < seconds * RATE:
        st.prepare_audio(sawtooth(k, chunk), fmt)
        t = await st.commit_audio()
        if start_us is None:
            start_us = t
        k += chunk
        await st.sleep_to_limit_buffer(3_000_000)
    if not keep:
        # Let the buffered tail play: up to 3s queued behind a ~2s lead. 10.x
        # ends the stream at stop() and the player clears on stream/end, as
        # the spec says; 9.1.1 let it play out.
        await asyncio.sleep(5.5)
        st.stop()
    return st, start_us


def sync_errors(periods: list[dict], start_us: int, delay_us: int = 0) -> list[float]:
    """Error of each played period against the server's schedule, in µs."""
    errs = []
    for p in periods:
        elapsed = p["rawUs"] + delay_us - start_us
        guess = round(elapsed * RATE / 1e6)
        v = p["first"] + SAW // 2
        # The sample index is v mod SAW; take the candidate nearest the guess.
        k = guess - ((guess - v) % SAW)
        if guess - k > SAW / 2:
            k += SAW
        errs.append(p["rawUs"] + delay_us - (start_us + k * 1e6 / RATE))
    return errs


async def until(pred, timeout: float, what: str) -> None:
    for _ in range(int(timeout * 20)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise Fail(what)


def player_available(server: SendspinServer, cid: str) -> bool:
    c = server.get_client(cid)
    return c is not None and c.is_connected and c.available and "player@v1" in c.active_role_ids


def check_sync(periods, start_us, label: str, delay_us: int = 0) -> None:
    errs = sync_errors(periods, start_us, delay_us)[40:]  # past the first ~1.7s
    if len(errs) < 40:
        first = (periods[0]["rawUs"] - start_us) / 1000 if periods else None
        raise Fail(f"{label}: only {len(errs)} periods played past the first 40 "
                   f"({len(periods)} in all, the first {first}ms after the stream's start)")
    worst = max(abs(e) for e in errs)
    print(f"   {label}: {len(errs)} periods, mean {statistics.mean(errs):+.0f}us, "
          f"p99 |err| {sorted(abs(e) for e in errs)[int(len(errs) * 0.99)]:.0f}us, worst {worst:.0f}us")
    if worst > 1000:
        raise Fail(f"{label}: sync error {worst:.0f}us exceeds the spec's 1ms floor")


async def scenario_pair_and_play() -> None:
    print("== pair by token, play, volume, delay, clear, HA takes over, reconnect paired")
    store = InMemoryServerPairingStore()
    server = SendspinServer(asyncio.get_running_loop(), Identity.generate(), "interop-server",
                            pairing_store=store)
    client = Client("-dacppm", "-560")
    ready = await client.start("/tmp/pair.json")
    cid = ready["clientId"]
    try:
        server.connect_to_client(URL)
        await until(lambda: (c := server.get_client(cid)) is not None and c.is_connected, 10, "never connected")
        if player_available(server, cid):
            raise Fail("unpaired, unapproved client was given playback")
        token = decode_token(ready["token"])
        if token.client_id != cid:
            raise Fail("token names a different client")
        await server.initiate_pairing(cid, psk_attempt(cid, token.pairing_psk))
        await until(lambda: server.get_client(cid).is_paired, 10, "not paired")
        print("   paired by token")
        await until(lambda: player_available(server, cid), 15, "paired player never became available")
        await client.wait("status", 5, lambda e: e.get("paired"))
        print("   client reports paired, clock synced")

        n0 = len(client.events)
        st, start_us = await stream(server, cid, 12, keep=True)
        await asyncio.sleep(1.1)  # a status tick, with audio still queued
        s = client.status()
        print(f"   client: snaps {s['player']['snaps']} corrections {s['player']['corrections']} "
              f"underruns {s['player']['underruns']} syncErr {s.get('syncErrUs')}us")
        if s["player"]["underruns"]:
            raise Fail("underrun on a local link")
        await asyncio.sleep(2.5)
        check_sync(client.periods(n0), start_us, "sync, DAC -560ppm")

        # Volume from the server: settings event, and the output drops by
        # (0.5)^1.5 in amplitude.
        before = statistics.mean(p["rms"] for p in client.periods(n0)[-20:])
        server.get_client(cid).role("player@v1").set_volume(50)
        await client.wait("settings", 5, lambda e: e["volume"] == 50)
        n1 = len(client.events)
        k = 0
        fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=1)
        for _ in range(150):
            st.prepare_audio(sawtooth(k, 960), fmt)
            await st.commit_audio()
            k += 960
            await st.sleep_to_limit_buffer(3_000_000)
        await asyncio.sleep(3.5)
        after = statistics.mean(p["rms"] for p in client.periods(n1)[-20:])
        ratio = after / before
        print(f"   volume 50: power ratio {ratio:.3f} (want {0.5 ** 3:.3f})")
        if abs(ratio - 0.125) > 0.02:
            raise Fail("volume not applied on the spec's curve")
        server.get_client(cid).role("player@v1").set_volume(100)
        await client.wait("settings", 5, lambda e: e["volume"] == 100)

        # The other direction: the Echo's own volume moves (a button, HA) and
        # the server hears it, so Music Assistant's slider follows.
        await client.command("volume 30")
        await until(lambda: server.get_client(cid).role("player@v1").volume == 30, 5,
                    "server never heard the device's volume change")
        print("   device volume 30: server shows 30")
        # Back to 100: it persists, and without a device volume this harness
        # applies it as gain, which would scale the sawtooth the sync check
        # reads sample positions from.
        await client.command("volume 100")
        await until(lambda: server.get_client(cid).role("player@v1").volume == 100, 5,
                    "volume did not return to 100")

        # Delay from the server: the Echo applies it and reports it back
        # under the name this server reads (9.1.1 static_delay_ms, 10.x
        # output_delay_ms; the Echo sends both).
        role = server.get_client(cid).role("player@v1")
        set_delay = getattr(role, "set_output_delay", None) or role.set_static_delay
        reported = lambda: getattr(role, "output_delay_ms", getattr(role, "static_delay_ms", None))
        set_delay(120)
        await client.wait("settings", 5, lambda e: e["delayMs"] == 120)
        await until(lambda: reported() == 120, 5, f"server reads the delay as {reported()}, not 120")
        set_delay(0)
        await until(lambda: reported() == 0, 5, "delay did not return to 0")
        print("   delay 120ms: applied, and read back by the server")

        # stream/clear (a seek): the buffer empties and play resumes in sync.
        st.clear()
        n2 = len(client.events)
        base = None
        for i in range(250):
            st.prepare_audio(sawtooth(i * 960, 960), fmt)
            t = await st.commit_audio()
            base = base or t
            await st.sleep_to_limit_buffer(3_000_000)
        await asyncio.sleep(3.5)
        check_sync(client.periods(n2), base, "after stream/clear")

        # HA takes the music plane: the device goes unavailable and the
        # server stops streaming to it.
        await client.command("external on")
        await until(lambda: not server.get_client(cid).available, 5, "server still sees the client available")
        print("   HA took over: client unavailable to the server")
        await client.command("external off")
        await until(lambda: server.get_client(cid).available, 5, "client never came back")
        st.stop()

        # The controller link drops and returns: the player says goodbye
        # "restart" and comes back on the same port. The server must redial by
        # itself — the firmware relies on it.
        await client.command("relink")
        await client.wait("relinked", 10)
        await until(lambda: player_available(server, cid), 30, "server never redialled after a restart goodbye")
        print("   player restarted (link down/up): server redialled on its own")

        # Reconnect: the long-term PSK must carry the session with no pairing.
        server.disconnect_from_client(URL)
        await until(lambda: not server.get_client(cid).is_connected, 5, "did not disconnect")
        server.connect_to_client(URL)
        await until(lambda: player_available(server, cid), 15, "paired reconnect never became available")
        await client.wait("status", 5, lambda e: e.get("paired") and e.get("state") != "listening")
        print("   reconnected under the long-term PSK")
        n3 = len(client.events)
        _, start_us = await stream(server, cid, 5)
        check_sync(client.periods(n3), start_us, "sync after reconnect")
    finally:
        await client.stop()
        await server.close()


async def scenario_unpaired() -> None:
    print("== unpaired access off: an approved-but-unpaired server is refused playback")
    server = SendspinServer(asyncio.get_running_loop(), Identity.generate(), "interop-server",
                            pairing_store=InMemoryServerPairingStore())
    client = Client()
    ready = await client.start("/tmp/unpaired-off.json")
    cid = ready["clientId"]
    try:
        server.connect_to_client(URL)
        await until(lambda: (c := server.get_client(cid)) is not None and c.is_connected, 10, "never connected")
        await server.trust_unpaired(cid)
        await asyncio.sleep(2)
        if player_available(server, cid):
            raise Fail("device accepted unpaired playback with unpaired access off")
        print("   refused: no playback without pairing")
    finally:
        await client.stop()
        await server.close()

    print("== unpaired access on: approval alone is enough")
    server = SendspinServer(asyncio.get_running_loop(), Identity.generate(), "interop-server",
                            pairing_store=InMemoryServerPairingStore())
    client = Client("-unpaired")
    ready = await client.start("/tmp/unpaired-on.json")
    cid = ready["clientId"]
    try:
        server.connect_to_client(URL)
        await until(lambda: (c := server.get_client(cid)) is not None and c.is_connected, 10, "never connected")
        await server.trust_unpaired(cid)
        await until(lambda: player_available(server, cid), 15, "approved unpaired client never available")
        # 10.x keeps a player out of a stream until its first client/state,
        # then joins it late, skipping by its lead: a stream started in that
        # window loses ~2s on any spec client (docs/history/device.md,
        # 2026-11-10). Start once the server has the state, as Music
        # Assistant does for a player it is already connected to.
        awaits = getattr(server.get_client(cid), "awaits_role_state", lambda _f: False)
        await until(lambda: not awaits("player"), 5, "server never received the player's state")
        n0 = len(client.events)
        _, start_us = await stream(server, cid, 5)
        periods = client.periods(n0)
        first_ms = (periods[0]["rawUs"] - start_us) / 1000 if periods else None
        print(f"   first sample played {first_ms:.0f}ms after it was due" if periods else "   nothing played")
        if first_ms is None or abs(first_ms) > 100:
            raise Fail(f"the stream's first audio played {first_ms}ms after it was due")
        check_sync(periods, start_us, "unpaired sync")
    finally:
        await client.stop()
        await server.close()


async def live_lead(server: SendspinServer, client: Client, cid: str, seconds: float) -> float:
    """Stream a live source (radio, Spotify) at real-time pace; returns the
    median buffered lead the client reported over the last half."""
    group = server.get_client(cid).group
    st = group.start_stream()
    st.set_live_source(True)
    fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=1)
    chunk = RATE // 50
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    n0 = len(client.events)
    k = 0
    while k < seconds * RATE:
        st.prepare_audio(sawtooth(k, chunk), fmt)
        await st.commit_audio()
        k += chunk
        await asyncio.sleep(max(0.0, t0 + k / RATE - loop.time()))
    st.stop()
    leads = [e["bufferedMs"] for e in client.events[n0:]
             if e.get("event") == "status" and e.get("bufferedMs")]
    tail = leads[len(leads) // 2:]
    if not tail:
        raise Fail("client reported no buffered lead")
    return statistics.median(tail)


async def scenario_live_buffer() -> None:
    print("== a live source is buffered to the player's min_buffer_ms, and a raise takes effect (#1004)")
    server = SendspinServer(asyncio.get_running_loop(), Identity.generate(), "interop-server",
                            pairing_store=InMemoryServerPairingStore())
    client = Client("-unpaired")
    ready = await client.start("/tmp/live-buffer.json")
    cid = ready["clientId"]
    try:
        server.connect_to_client(URL)
        await until(lambda: (c := server.get_client(cid)) is not None and c.is_connected, 10, "never connected")
        await server.trust_unpaired(cid)
        await until(lambda: player_available(server, cid), 15, "never available")
        base = await live_lead(server, client, cid, 8)
        print(f"   default: {base:.0f}ms buffered")
        if not 1500 <= base <= 2600:
            raise Fail(f"live lead {base:.0f}ms at the default, want ~2000")
        await client.command("buffer 4000")
        role = server.get_client(cid).role("player@v1")
        await until(lambda: role.min_buffer_ms == 4000, 5, "server never saw min_buffer_ms 4000")
        raised = await live_lead(server, client, cid, 10)
        print(f"   raised: {raised:.0f}ms buffered")
        if raised < 3500:
            raise Fail(f"live lead {raised:.0f}ms after asking for 4000")
        await client.command("buffer 0")
        await until(lambda: role.min_buffer_ms == 2000, 5, "min_buffer_ms not restored to 2000")
    finally:
        await client.stop()
        await server.close()


def tones(start: int, n: int, amp: float) -> bytes:
    """Stereo: 1kHz on the left, 440Hz on the right."""
    out = []
    for i in range(start, start + n):
        out.append(round(amp * math.sin(2 * math.pi * 1000 * i / RATE)))
        out.append(round(amp * math.sin(2 * math.pi * 440 * i / RATE)))
    return struct.pack(f"<{2 * n}h", *out)


async def scenario_opus() -> None:
    print("== the device's own format list: the server picks stereo Opus, the player downmixes")
    server = SendspinServer(asyncio.get_running_loop(), Identity.generate(), "interop-server",
                            pairing_store=InMemoryServerPairingStore())
    client = Client("-unpaired", "-codecs", "")
    ready = await client.start("/tmp/opus.json")
    cid = ready["clientId"]
    try:
        server.connect_to_client(URL)
        await until(lambda: (c := server.get_client(cid)) is not None and c.is_connected, 10, "never connected")
        await server.trust_unpaired(cid)
        await until(lambda: player_available(server, cid), 15, "never available")
        awaits = getattr(server.get_client(cid), "awaits_role_state", lambda _f: False)
        await until(lambda: not awaits("player"), 5, "server never received the player's state")
        amp = 32767 * 10 ** (-12 / 20)
        group = server.get_client(cid).group
        st = group.start_stream()
        fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=2)
        n0 = len(client.events)
        k = 0
        while k < 8 * RATE:
            st.prepare_audio(tones(k, RATE // 50, amp), fmt)
            await st.commit_audio()
            k += RATE // 50
            await st.sleep_to_limit_buffer(3_000_000)
        await asyncio.sleep(1.1)  # a status tick, with audio still queued
        if client.status()["player"]["underruns"]:
            raise Fail("underrun on a local link")
        await asyncio.sleep(4.4)
        st.stop()
        started = [e["line"] for e in client.events[n0:] if e.get("event") == "log" and "stream:" in e["line"]]
        if not started or "stream: opus 48000Hz 2ch" not in started[0]:
            raise Fail(f"server did not pick stereo Opus: {started}")
        print(f"   {started[0].split('] ', 1)[-1]}")
        if any("decode:" in e.get("line", "") for e in client.events[n0:]):
            raise Fail("the player logged a decode error")
        periods = client.periods(n0)[40:-40]
        if len(periods) < 60:
            raise Fail(f"only {len(periods)} periods played")
        # Each side a sine at amp, uncorrelated: the average's mean square is amp^2/4.
        db = 10 * math.log10(statistics.mean(p["rms"] for p in periods) / (amp * amp / 4))
        print(f"   {len(periods)} periods, downmix level {db:+.2f} dB from expected")
        if abs(db) > 1:
            raise Fail(f"downmix {db:+.2f} dB from the average of the two sides")
    finally:
        await client.stop()
        await server.close()


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    failed = 0
    for sc in (scenario_pair_and_play, scenario_unpaired, scenario_live_buffer, scenario_opus):
        try:
            await sc()
            print("   PASS")
        except Fail as e:
            failed += 1
            print(f"   FAIL: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
