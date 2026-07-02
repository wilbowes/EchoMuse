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
Noise-encrypted frame, which we reject (we don't implement Noise; see
ESPHOME_SPEC.md §5 option (a)).
"""

from __future__ import annotations

import asyncio
import logging
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

    # ── asyncio.Protocol interface ──────────────────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        peer = transport.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        log.info(f"[{self._log_name}] Connection from {self.peer}")
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
            raise FrameProtocolError(
                "peer requested Noise-encrypted frame — this server is "
                "plaintext-only (ESPHOME_SPEC.md §5 option (a))"
            )
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

    # ── Writing ───────────────────────────────────────────────────────────

    def send_packet(self, msg_type: int, payload: bytes) -> None:
        """Encode and write a single packet. Safe to call repeatedly for a burst."""
        if self._transport is None or self._transport.is_closing():
            return
        self._transport.write(encode_frame(msg_type, payload))

    def send_packets(self, packets: list[tuple[int, bytes]]) -> None:
        """Encode and write multiple packets in a single socket write."""
        if self._transport is None or self._transport.is_closing():
            return
        self._transport.write(b"".join(encode_frame(t, p) for t, p in packets))

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
