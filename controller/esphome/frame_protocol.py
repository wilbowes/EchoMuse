"""
frame_protocol.py — ESPHome native API plaintext wire framing
================================================================

A server-shaped asyncio.Protocol implementing ESPHome's native API frame
format (plaintext only — no Noise-PSK, see ESPHOME_SPEC.md §5/§7.2).

Why this is hand-written rather than vendored from aioesphomeapi's
_frame_helper/plain_text.py:

  aioesphomeapi's frame helpers are CLIENT-shaped — APIFrameHelper.__init__
  takes an `APIConnection` (the client's own connection object) and the
  Cython-compiled hot path (.pxd-coordinated __slots__, `_int`/`_bytes`
  aliases) assumes that calling context. We are the SERVER side of this
  protocol (HA dials in to us — see ESPHOME_SPEC.md §2.1), a role
  aioesphomeapi doesn't implement at all; per ESPHOME_SPEC.md §7.1, even
  the reference `linux-voice-assistant` project hand-rolls its own
  ~190-line server rather than importing one.

  What IS worth preserving from the original: the varuint encode/decode
  algorithm and the frame-length bounds-checking (DoS hardening — caps
  varuint length at 4 bytes / frame length at 65535 bytes, matching the
  firmware's uint16_t wire limit). Those are reimplemented below following
  the same logic, not blindly copied, since the buffering strategy differs
  (server handles N concurrent connections, not one).

Wire format (plaintext frame):
    [0x00] [varuint: payload length] [varuint: message type] [payload bytes]

The leading 0x00 byte is the plaintext indicator — a 0x01 there signals a
Noise-encrypted frame.

ENCRYPTION (2026-10-03, #656). A listener given a key with set_encryption()
speaks ESPHome's Noise framing instead, and refuses plaintext:

    [0x01] [length, 2 bytes big-endian] [data]

    client → hello frame (data empty today; whatever it holds is bound into
             the handshake as the prologue "NoiseAPIInit" + length + data)
    server → [0x01] name NUL mac NUL          chosen protocol, who we are
    client → [0x00] + Noise message 1         -> psk, e
    server → [0x00] + Noise message 2         <- e, ee
    then, each way: encrypt( type(2) length(2) payload )

A failed handshake is answered with [0x01] + a reason before closing, and the
reason strings are the ones ESPHome's own firmware sends, because Home
Assistant's client keys on them: "Handshake MAC failure" is what makes it ask
for the key again rather than report a dead device. A plaintext client gets
"Bad indicator byte"; any frame that opens with 0x01 is how it learns the
device requires encryption. The handshake itself is esphome/noise.py.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Callable, Optional

log = logging.getLogger("echomuse.esphome.frame")

# DoS bound: caps decoded varuint value so it can never overflow / wrap.
# A 4-byte varuint maxes out at 2**28-1, comfortably above anything this
# protocol actually uses (frame length is capped at 65535 = 17 bits;
# message type fits in a handful of bits). See aioesphomeapi's own comment
# on this exact constant for the overflow rationale this mirrors.
_MAX_VARUINT_BYTES = 4
_MAX_VARUINT_BITPOS = 7 * _MAX_VARUINT_BYTES

# Matches the firmware's uint16_t wire-format frame length cap.
MAX_PLAINTEXT_FRAME_SIZE = 65535

# TCP keepalive tuning for accepted HA connections.
#
# Why this exists: a real ESP32 losing its HA connection gets a proper TCP
# FIN/RST from the OS in basically every failure mode. Our situation is
# different — em_esphome.py's device_connected()/device_disconnected()
# lifecycle correctly tears the *listener* down when the physical Echo Dot
# drops, but there's no equivalent signal for the *client* (HA) side. If
# HA's host restarts in a way that doesn't cleanly close its socket (hard
# reboot, container kill, network blip) rather than a graceful shutdown,
# asyncio has no way to know — connection_lost() only fires when the OS
# tells it the peer is gone, and with no keepalive that can be never. The
# controller is then left with a stale _active_satellite that
# get_satellite() keeps returning, so trigger_voice_turn() silently no-ops
# forever until something else (a device reboot) forces the listener to
# restart. Keepalive gives the OS a way to actually detect the dead peer
# instead of waiting indefinitely for a FIN that may never come.
#
# Values chosen for "a human already knows something's wrong" latency, not
# ESPHome-protocol-mandated — the protocol has no opinion on this, it's
# pure TCP hygiene sitting below it. ~30s to first probe, 3 missed probes
# 10s apart = dead connection reaped within ~60s of actually going dark.
_KEEPALIVE_IDLE_SEC = 30
_KEEPALIVE_INTERVAL_SEC = 10
_KEEPALIVE_COUNT = 3


def _enable_tcp_keepalive(transport: asyncio.BaseTransport, log_name: str) -> None:
    """
    Turn on SO_KEEPALIVE with explicit Linux tuning on an accepted socket.

    Best-effort: asyncio's transport wraps a real socket, but reaching it
    is via get_extra_info("socket") rather than a typed API, and the
    per-platform TCP_KEEPIDLE/TCP_KEEPINTVL/TCP_KEEPCNT options are
    Linux-specific (absent on macOS/BSD). Controller runs in a Linux
    Docker container per the Dockerfile, so this is the only platform
    that actually matters here — the try/except is defensive for local
    dev on other platforms, not a sign this is expected to fail in prod.
    """
    sock = transport.get_extra_info("socket")
    if sock is None:
        log.warning(f"[{log_name}] no underlying socket — keepalive not set")
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _KEEPALIVE_IDLE_SEC)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPALIVE_INTERVAL_SEC)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPALIVE_COUNT)
    except OSError as e:
        log.warning(f"[{log_name}] failed to set TCP keepalive: {e}")

# Sentinels for the streaming varuint reader (negative — varuints are
# never negative, so these can't collide with a real decoded value).
_VARUINT_INCOMPLETE = -1
_VARUINT_TOO_LONG = -2


class FrameProtocolError(Exception):
    """Raised on a malformed frame or a frame requiring encryption we don't support."""


def encode_varuint(value: int) -> bytes:
    """Encode a non-negative int as an ESPHome-protocol varuint."""
    if value < 0:
        raise ValueError(f"varuint cannot encode negative value: {value}")
    if value <= 0x7F:
        return bytes((value,))
    out = bytearray()
    while value:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
    return bytes(out)


def encode_frame(msg_type: int, payload: bytes) -> bytes:
    """
    Encode a single (msg_type, payload) pair as a plaintext wire frame.

    Multiple encoded frames may be concatenated and written in one
    socket write — the protocol does not require one frame per write.
    """
    return (
        b"\x00"
        + encode_varuint(len(payload))
        + encode_varuint(msg_type)
        + payload
    )


class _VaruintReader:
    """
    Incremental varuint decoder over a growing byte buffer.

    Mirrors the read-don't-copy buffering strategy of the original —
    `pos` tracks how far into `buf` the current frame attempt has
    consumed, and the caller only slices/discards once a full frame is
    confirmed present. This avoids re-copying the buffer on every
    partial read when a frame arrives across multiple TCP segments.
    """

    __slots__ = ("buf", "pos")

    def __init__(self) -> None:
        self.buf = b""
        self.pos = 0

    def feed(self, data: bytes) -> None:
        if self.pos:
            # A previous read_varuint/read_exact call consumed `pos` bytes
            # from `buf` for a still-incomplete frame; drop the consumed
            # prefix before appending so `pos` can reset to 0.
            self.buf = self.buf[self.pos:]
            self.pos = 0
        self.buf += data

    def read_varuint(self) -> int:
        """Returns decoded value, _VARUINT_INCOMPLETE, or _VARUINT_TOO_LONG."""
        result = 0
        bitpos = 0
        n = len(self.buf)
        pos = self.pos
        while pos < n:
            val = self.buf[pos]
            pos += 1
            result |= (val & 0x7F) << bitpos
            if (val & 0x80) == 0:
                self.pos = pos
                return result
            bitpos += 7
            if bitpos >= _MAX_VARUINT_BITPOS:
                self.pos = pos
                return _VARUINT_TOO_LONG
        return _VARUINT_INCOMPLETE

    def read_exact(self, length: int) -> Optional[bytes]:
        """Returns `length` bytes from the current position, or None if not yet buffered."""
        end = self.pos + length
        if len(self.buf) < end:
            return None
        data = self.buf[self.pos:end]
        self.pos = end
        return data

    def reset_to_start(self) -> None:
        """Rewind to the beginning of the current (incomplete) frame attempt."""
        self.pos = 0


class PlaintextFrameProtocol(asyncio.Protocol):
    """
    Server-side asyncio.Protocol for one ESPHome native API connection.

    Usage: subclass or pass callbacks via the constructor. on_packet is
    called as on_packet(msg_type: int, payload: bytes) for every fully
    decoded frame. on_connected/on_disconnected are lifecycle hooks.

    One instance is created per accepted TCP connection (see
    asyncio.start_server's protocol_factory).
    """

    def __init__(
        self,
        on_packet: Callable[["PlaintextFrameProtocol", int, bytes], None],
        on_connected: Optional[Callable[["PlaintextFrameProtocol"], None]] = None,
        on_disconnected: Optional[Callable[["PlaintextFrameProtocol"], None]] = None,
        log_name: str = "esphome",
    ) -> None:
        self._on_packet = on_packet
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._log_name = log_name
        self._reader = _VaruintReader()
        self._transport: Optional[asyncio.Transport] = None
        self.peer: str = "unknown"
        # Encryption: None for a plaintext listener. See set_encryption.
        self._noise_psk: Optional[bytes] = None
        self._noise_hello = b""
        self._noise_state = "hello"       # hello → handshake → data
        self._noise_buf = bytearray()
        self._noise_responder = None
        self._noise_recv = None
        self._noise_send = None

    def set_encryption(self, psk: bytes, server_name: str, mac: str) -> None:
        """
        Require Noise encryption with this 32-byte key on this connection.
        Call before the connection is made. `mac` is twelve lowercase hex
        digits, as ESPHome reports it in its hello.
        """
        if len(psk) != 32:
            raise ValueError("the encryption key must be 32 bytes")
        self._noise_psk = psk
        self._noise_hello = (b"\x01" + server_name.encode() + b"\x00"
                             + mac.encode() + b"\x00")

    @property
    def encrypted(self) -> bool:
        """True once the handshake is done and frames are encrypted."""
        return self._noise_state == "data"

    # ── asyncio.Protocol interface ──────────────────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        peer = transport.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        log.info(f"[{self._log_name}] Connection from {self.peer}")
        _enable_tcp_keepalive(transport, self._log_name)
        if self._on_connected:
            self._on_connected(self)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        log.info(
            f"[{self._log_name}] Connection closed: {self.peer}"
            + (f" ({exc})" if exc else "")
        )
        if self._on_disconnected:
            self._on_disconnected(self)

    def data_received(self, data: bytes) -> None:
        if self._noise_psk is not None:
            self._noise_received(data)
            return
        self._reader.feed(data)
        try:
            while True:
                if not self._try_decode_one_frame():
                    return
        except FrameProtocolError as e:
            log.warning(f"[{self._log_name}] {self.peer}: {e} — closing connection")
            self.close()

    # ── Frame decoding ───────────────────────────────────────────────────

    def _try_decode_one_frame(self) -> bool:
        """
        Attempt to decode one complete frame from the buffer.

        Returns True if a frame was decoded (and dispatched to on_packet) —
        caller should loop again in case more frames are already buffered.
        Returns False if more data is needed — caller should wait for the
        next data_received call.

        Raises FrameProtocolError on a malformed frame or a frame
        indicating Noise encryption (preamble 0x01), which this
        plaintext-only server does not support.
        """
        r = self._reader
        if len(r.buf) - r.pos < 3:
            return False  # minimum possible frame: 1 + 1 + 1 byte header

        start_pos = r.pos

        preamble = r.read_varuint()
        if preamble == _VARUINT_INCOMPLETE:
            r.pos = start_pos
            return False
        if preamble == _VARUINT_TOO_LONG:
            raise FrameProtocolError("preamble varuint exceeds byte limit")
        if preamble == 0x01:
            # A client that holds an encryption key for a listener that has
            # none — a Bluetooth proxy whose connections were switched off
            # again. Answered as ESPHome's firmware answers it, a plaintext
            # indicator and a reason, because that is what tells Home
            # Assistant the device no longer uses encryption so it can offer
            # to drop the key. Closing without a word reads as a dead device
            # and it retries for ever (measured with its client, 2026-10-03).
            if self._transport is not None and not self._transport.is_closing():
                self._transport.write(b"\x00Bad indicator byte")
            raise FrameProtocolError(
                "peer sent an encrypted frame to a listener with no key")
        if preamble != 0x00:
            raise FrameProtocolError(f"invalid frame preamble 0x{preamble:02x}")

        length = r.read_varuint()
        if length == _VARUINT_INCOMPLETE:
            r.pos = start_pos
            return False
        if length == _VARUINT_TOO_LONG:
            raise FrameProtocolError("length varuint exceeds byte limit")
        if length > MAX_PLAINTEXT_FRAME_SIZE:
            raise FrameProtocolError(
                f"frame length {length} exceeds {MAX_PLAINTEXT_FRAME_SIZE}-byte limit"
            )

        msg_type = r.read_varuint()
        if msg_type == _VARUINT_INCOMPLETE:
            r.pos = start_pos
            return False
        if msg_type == _VARUINT_TOO_LONG:
            raise FrameProtocolError("msg_type varuint exceeds byte limit")

        if length == 0:
            payload = b""
        else:
            payload = r.read_exact(length)
            if payload is None:
                r.pos = start_pos
                return False

        self._on_packet(self, msg_type, payload)
        return True

    # ── Encrypted framing ────────────────────────────────────────────────

    def _noise_received(self, data: bytes) -> None:
        buf = self._noise_buf
        buf += data
        while buf:
            if self._transport is None or self._transport.is_closing():
                return
            if buf[0] != 0x01:
                # A plaintext client. The reply opens with 0x01, which is how
                # it learns this device requires encryption.
                log.warning(f"[{self._log_name}] {self.peer}: unencrypted "
                            f"connection refused — this port requires a key")
                self._noise_reject("Bad indicator byte")
                return
            if len(buf) < 3:
                return
            length = (buf[1] << 8) | buf[2]
            if len(buf) < 3 + length:
                return
            frame = bytes(buf[3:3 + length])
            del buf[:3 + length]
            try:
                self._noise_frame(frame)
            except FrameProtocolError as e:
                log.warning(f"[{self._log_name}] {self.peer}: {e} — closing connection")
                self.close()
                return

    def _noise_frame(self, frame: bytes) -> None:
        from esphome import noise  # needs `cryptography`; plaintext does not

        if self._noise_state == "hello":
            prologue = b"NoiseAPIInit" + struct.pack(">H", len(frame)) + frame
            self._noise_responder = noise.Responder(self._noise_psk, prologue)
            self._noise_write(self._noise_hello)
            self._noise_state = "handshake"
            return

        if self._noise_state == "handshake":
            if not frame:
                self._noise_reject("Bad handshake packet len")
                return
            if frame[0] != 0x00:
                self._noise_reject("Bad handshake error byte")
                return
            try:
                self._noise_responder.read_message_1(frame[1:])
                reply = self._noise_responder.write_message_2()
                self._noise_recv, self._noise_send = self._noise_responder.split()
            except noise.NoiseError as e:
                log.warning(f"[{self._log_name}] {self.peer}: encryption "
                            f"handshake failed ({e}) — wrong key?")
                self._noise_reject("Handshake MAC failure")
                return
            self._noise_responder = None
            self._noise_write(b"\x00" + reply)
            self._noise_state = "data"
            return

        try:
            plain = self._noise_recv.decrypt(frame)
        except noise.NoiseError as e:
            raise FrameProtocolError(f"encrypted frame rejected: {e}") from None
        if len(plain) < 4:
            raise FrameProtocolError("encrypted frame shorter than its header")
        msg_type, length = struct.unpack(">HH", plain[:4])
        if length > len(plain) - 4:
            raise FrameProtocolError("encrypted frame shorter than its stated length")
        self._on_packet(self, msg_type, plain[4:4 + length])

    def _noise_write(self, data: bytes) -> None:
        if self._transport is None or self._transport.is_closing():
            return
        self._transport.write(b"\x01" + struct.pack(">H", len(data)) + data)

    def _noise_reject(self, reason: str) -> None:
        self._noise_write(b"\x01" + reason.encode())
        self._noise_state = "failed"
        self.close()

    def _noise_encode(self, msg_type: int, payload: bytes) -> bytes:
        if len(payload) > MAX_PLAINTEXT_FRAME_SIZE - 4 - 16:
            raise FrameProtocolError(f"message of {len(payload)} bytes does not fit a frame")
        data = self._noise_send.encrypt(struct.pack(">HH", msg_type, len(payload)) + payload)
        return b"\x01" + struct.pack(">H", len(data)) + data

    # ── Writing ───────────────────────────────────────────────────────────

    def send_packet(self, msg_type: int, payload: bytes) -> None:
        """Encode and write a single packet. Safe to call repeatedly for a burst."""
        self.send_packets([(msg_type, payload)])

    def send_packets(self, packets: list[tuple[int, bytes]]) -> None:
        """Encode and write multiple packets in a single socket write."""
        if self._transport is None or self._transport.is_closing():
            return
        if self._noise_psk is None:
            self._transport.write(b"".join(encode_frame(t, p) for t, p in packets))
            return
        if self._noise_state != "data":
            return  # nothing may be said in the clear on an encrypted port
        self._transport.write(b"".join(self._noise_encode(t, p) for t, p in packets))

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
