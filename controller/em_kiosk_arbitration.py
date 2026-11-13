"""Kiosk Satellite wake arbitration over the local network."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import secrets

from em_wakelevel import dbfs

PORT = 2330
BROADCAST = ("255.255.255.255", PORT)
MAX_PACKET_BYTES = 1024
CLAIM_TTL = 2.0
PEER_TTL = 30 * 60
HELLO_INTERVAL = 5 * 60
log = logging.getLogger("echomuse.kiosk_arbitration")


@dataclass(frozen=True)
class Claim:
    peer_id: str
    phrase: str
    energy: float | None
    received_at: float
    device_id: str | None = None

    @property
    def rank(self) -> tuple[float, float, str]:
        if self.energy is None:
            return math.inf, self.received_at, self.peer_id
        return -self.energy, 0.0, self.peer_id


def normalize(phrase: str) -> str:
    return " ".join(phrase.lower().split())


def _round_energy(energy: float) -> float:
    # Kiosk rounds halves away from zero.
    return math.copysign(math.floor(abs(energy) * 10 + 0.5) / 10, energy)


def wake_energy(level: float | None, floor_rms: float, gain_db: float,
                offset_db: float) -> float | None:
    """Estimate dB over the noise floor using EchoMuse's wake measurements."""
    if (level is None or floor_rms <= 0
            or not all(map(math.isfinite, (level, floor_rms, gain_db, offset_db)))):
        return None
    # EchoMuse removes digital gain from the wake level. Compare the noise
    # floor on the same scale. The -75 dBFS clamp is on the measured input.
    floor = max(-75.0, dbfs(floor_rms)) - gain_db
    return _round_energy(max(0.0, level - floor) + offset_db)


def _packet(peer_id: str, **fields) -> bytes:
    return json.dumps({"ks": "wake", "v": 1, "id": peer_id, **fields},
                      separators=(",", ":")).encode()


class KioskArbiter(asyncio.DatagramProtocol):
    def __init__(self, window_ms: int = 400, offset_db: float = 0) -> None:
        if not 100 <= window_ms <= 500:
            raise ValueError("Kiosk arbitration window must be 100–500 ms")
        if not -30 <= offset_db <= 30:
            raise ValueError("Kiosk arbitration offset must be -30 to +30 dB")
        self.window = window_ms / 1000
        self.offset_db = offset_db
        self._id = secrets.token_hex(8)
        self._transport: asyncio.DatagramTransport | None = None
        self._hello_timer: asyncio.TimerHandle | None = None
        self._claims: OrderedDict[tuple[str, int], Claim] = OrderedDict()
        self._peers: OrderedDict[str, float] = OrderedDict()
        self._local_ids: set[str] = {self._id}

    @property
    def active(self) -> bool:
        return self._transport is not None

    async def start(self) -> bool:
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: self, local_addr=("0.0.0.0", PORT), allow_broadcast=True,
            )
        except OSError as err:
            log.warning("Could not listen on UDP %s. Using native EchoMuse arbitration: %s",
                        PORT, err)
        return self.active

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport
        self._say_hello()

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None
        if self._hello_timer is not None:
            self._hello_timer.cancel()
        self._claims.clear()
        self._peers.clear()
        if exc:
            log.warning("Kiosk arbitration socket closed: %s", exc)

    def error_received(self, exc: Exception) -> None:
        log.debug("Kiosk arbitration datagram failed: %s", exc)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def _send(self, data: bytes, address: tuple[str, int]) -> None:
        if self._transport is not None:
            try:
                self._transport.sendto(data, address)
            except OSError as err:
                self.error_received(err)

    def _say_hello(self) -> None:
        self._send(_packet(self._id, hello=True), BROADCAST)
        self._hello_timer = asyncio.get_running_loop().call_later(
            HELLO_INTERVAL, self._say_hello,
        )

    def _prune(self, now: float) -> None:
        while self._claims and now - next(iter(self._claims.values())).received_at > CLAIM_TTL:
            self._claims.popitem(last=False)
        while self._peers and now - next(iter(self._peers.values())) > PEER_TTL:
            self._peers.popitem(last=False)

    def _remember(self, key: tuple[str, int], claim: Claim) -> None:
        if key not in self._claims:
            self._claims[key] = claim
            if len(self._claims) > 512:
                self._claims.popitem(last=False)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self.active or len(data) > MAX_PACKET_BYTES:
            return
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict) or msg.get("ks") != "wake" or msg.get("v") != 1:
            return
        peer_id = msg.get("id")
        if (not isinstance(peer_id, str) or not 0 < len(peer_id) <= 64
                or peer_id in self._local_ids):
            return

        hello = msg.get("hello") is True
        if not hello:
            phrase, energy, nonce = msg.get("p"), msg.get("e"), msg.get("n")
            if (not isinstance(phrase, str) or not 0 < len(phrase) <= 128
                    or type(energy) not in (int, float) or not -150 <= energy <= 150
                    or type(nonce) is not int or not 0 <= nonce < 2**30):
                return
            phrase = normalize(phrase)
            if not phrase:
                return

        now = asyncio.get_running_loop().time()
        self._prune(now)
        known = addr[0] in self._peers
        self._peers[addr[0]] = now
        self._peers.move_to_end(addr[0])
        if len(self._peers) > 64:
            self._peers.popitem(last=False)
        if hello:
            if not known:
                self._send(_packet(self._id, hello=True), (addr[0], PORT))
        else:
            self._remember((peer_id, nonce), Claim(peer_id, phrase, _round_energy(energy), now))

    async def contest(self, device_id: str, phrase: str, energy: float | None) -> Claim:
        """Broadcast this Echo's claim and choose using the on-wire ranking."""
        loop = asyncio.get_running_loop()
        if energy is not None:
            energy = _round_energy(energy)
        now = loop.time()
        self._prune(now)
        # Each Echo needs its own tie-break ID even though one controller
        # sends all of their claims.
        peer_id = hashlib.blake2s(f"{self._id}:{device_id}".encode(), digest_size=8).hexdigest()
        self._local_ids.add(peer_id)
        phrase = normalize(phrase)
        nonce = secrets.randbelow(2**30)
        mine = Claim(peer_id, phrase, energy, now, device_id)
        key = (peer_id, nonce)
        self._remember(key, mine)
        retries = []
        if energy is not None:
            data = _packet(peer_id, n=nonce, p=phrase, e=energy)

            def send_claim() -> None:
                self._send(data, BROADCAST)
                for peer in self._peers:
                    self._send(data, (peer, PORT))

            send_claim()
            retries = [loop.call_later(delay, send_claim) for delay in (0.015, 0.030)]
        try:
            await asyncio.sleep(self.window)
        except asyncio.CancelledError:
            self._claims.pop(key, None)
            raise
        finally:
            for retry in retries:
                retry.cancel()

        rivals = (
            claim for claim in self._claims.values()
            if claim.peer_id != peer_id and claim.phrase == phrase
            and abs(claim.received_at - now) <= self.window
        )
        return min((mine, *rivals), key=lambda claim: claim.rank)
