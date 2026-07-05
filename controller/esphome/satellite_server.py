"""
satellite_server.py — ESPHome native API satellite server
============================================================

Server-side handler for one device's ESPHome-API TCP listener (see
ESPHOME_SPEC.md §2.1/§2.2 — one port per device, 16001+, never reused).

Dispatch shape mirrors linux-voice-assistant's api_server.py (confirmed
by reading the actual source, not inferred): HelloRequest, PingRequest,
and DisconnectRequest are answered directly in process_packet; everything
else is handed to an overridable handle_message() that yields zero or
more response messages. This module implements the handshake only —
DeviceInfoRequest, ListEntitiesRequest, and the SubscribeVoiceAssistantRequest
/ voice-turn event sequence are left to a subclass (controller integration
work, not part of this standalone protocol-layer build).

Differences from the reference implementation, intentional:
  - Built on our own frame_protocol.PlaintextFrameProtocol rather than
    vendoring aioesphomeapi's _frame_helper directly — see frame_protocol.py
    docstring for why. Includes frame-length and varuint bounds checking
    the reference's hand-rolled reader doesn't have.
  - msg_type <-> class mapping is derived from descriptor extensions
    (message_registry.py) rather than imported from aioesphomeapi.core,
    since we don't depend on the full package.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Iterable, Optional

import esphome.message_registry as registry
from esphome.frame_protocol import FrameProtocolError, PlaintextFrameProtocol
from esphome.vendor import api_pb2

log = logging.getLogger("echomuse.esphome.satellite")

# ESPHome native API protocol version this server implements / reports in
# HelloResponse. linux-voice-assistant reports 1.10 — matching that rather
# than guessing keeps us behaving like hardware HA already knows how to talk to.
API_VERSION_MAJOR = 1
API_VERSION_MINOR = 10

# Sentinel yielded by handle_message() subclass implementations that handle
# a message but have nothing to send in response. Distinguishes "handled,
# no wire response" from "not handled at all" so _process_message can log
# genuinely unhandled messages without false-positives on intentional no-ops
# (SubscribeVoiceAssistantRequest, VoiceAssistantResponse, etc).
#
# Usage in subclass:
#   if isinstance(msg, SomeSilentMessageType):
#       do_work()
#       yield HANDLED
#       return
_HANDLED = object()


class SatelliteServerProtocol(PlaintextFrameProtocol):
    """
    One instance per accepted TCP connection (HA dials in — see §2.1).

    Subclasses implement handle_message() to respond to anything beyond
    the base Hello/Ping/Disconnect handshake (DeviceInfoRequest,
    ListEntitiesRequest, SubscribeVoiceAssistantRequest, etc).
    """

    def __init__(self, server_name: str, log_name: str = "esphome") -> None:
        super().__init__(
            on_packet=self._on_packet,
            on_connected=None,
            on_disconnected=self._on_disconnected_internal,
            log_name=log_name,
        )
        self.server_name = server_name
        self._disconnected_hook: Optional[callable] = None

    # ── Required override ───────────────────────────────────────────────

    @abstractmethod
    def handle_message(self, msg) -> Iterable:
        """
        Handle any message not covered by the base handshake.

        Yields zero or more protobuf message instances to send in response.
        Subclasses should yield, not return a list directly, to match the
        reference implementation's generator-based style (cheap to extend
        without restructuring callers).
        """
        return iter(())

    # ── Packet dispatch ──────────────────────────────────────────────────

    def _on_packet(self, proto, msg_type: int, payload: bytes) -> None:
        try:
            msg = registry.decode(msg_type, payload)
        except KeyError:
            log.warning(
                f"[{self._log_name}] {self.peer}: unknown msg_type {msg_type} "
                f"({len(payload)} bytes) — ignoring"
            )
            return
        except Exception as e:
            log.warning(
                f"[{self._log_name}] {self.peer}: failed to decode msg_type "
                f"{msg_type}: {e} — closing connection"
            )
            self.close()
            return

        self._process_message(msg)

    def _process_message(self, msg) -> None:
        if isinstance(msg, api_pb2.HelloRequest):
            log.info(
                f"[{self._log_name}] {self.peer}: Hello from "
                f"client_info={msg.client_info!r} "
                f"api_version={msg.api_version_major}.{msg.api_version_minor}"
            )
            self._send_one(
                api_pb2.HelloResponse(
                    api_version_major=API_VERSION_MAJOR,
                    api_version_minor=API_VERSION_MINOR,
                    server_info=self.server_name,
                    name=self.server_name,
                )
            )
            return

        if isinstance(msg, api_pb2.PingRequest):
            self._send_one(api_pb2.PingResponse())
            return

        if isinstance(msg, api_pb2.DisconnectRequest):
            log.info(f"[{self._log_name}] {self.peer}: Disconnect requested")
            self._send_one(api_pb2.DisconnectResponse())
            self.close()
            return

        if isinstance(msg, api_pb2.AuthenticationRequest):
            # Plaintext-only server (ESPHOME_SPEC.md §5 option (a)) — we
            # never request authentication, but respond gracefully if a
            # client sends it unprompted rather than silently dropping it.
            log.debug(
                f"[{self._log_name}] {self.peer}: unexpected AuthenticationRequest "
                f"— this server does not require auth, acknowledging anyway"
            )
            self._send_one(api_pb2.AuthenticationResponse(invalid_password=False))
            return

        try:
            responses = list(self.handle_message(msg))
        except Exception as e:
            log.error(
                f"[{self._log_name}] {self.peer}: handle_message raised for "
                f"{type(msg).__name__}: {e}"
            )
            return

        # Filter out _HANDLED sentinels before deciding what to send.
        # A subclass yields _HANDLED to signal "I dealt with this, nothing
        # to send" — distinct from an empty list which means the message
        # wasn't recognised at all.
        was_handled = any(r is _HANDLED for r in responses)
        to_send = [r for r in responses if r is not _HANDLED]

        if to_send:
            self._send_many(to_send)
        elif not was_handled:
            log.debug(
                f"[{self._log_name}] {self.peer}: unhandled message type "
                f"{type(msg).__name__} (no response)"
            )

    # ── Sending ──────────────────────────────────────────────────────────

    def _send_one(self, msg) -> None:
        msg_type, payload = registry.encode(msg)
        self.send_packet(msg_type, payload)

    def _send_many(self, msgs) -> None:
        packets = [registry.encode(m) for m in msgs]
        self.send_packets(packets)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _on_disconnected_internal(self, proto) -> None:
        if self._disconnected_hook:
            self._disconnected_hook(self)


async def serve(
    protocol_factory,
    host: str,
    port: int,
) -> asyncio.AbstractServer:
    """
    Start a TCP server on (host, port) using protocol_factory to build a
    new SatelliteServerProtocol subclass instance per connection.

    One call per device port (ESPHOME_SPEC.md §2.1 — per-device listener,
    not a shared/multiplexed port).
    """
    loop = asyncio.get_event_loop()
    server = await loop.create_server(protocol_factory, host, port)
    log.info(f"ESPHome satellite server listening on {host}:{port}")
    return server
