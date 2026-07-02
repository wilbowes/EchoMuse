"""
message_registry.py — msg_type <-> protobuf message class mapping
====================================================================

ESPHome's wire protocol identifies each message by a small integer
(`msg_type`, see frame_protocol.py), not by name. The mapping is declared
in api.proto via a custom field option (`id`, extension number 1036 on
MessageOptions) on every top-level message — see api_options_pb2.py.

aioesphomeapi itself maintains this as a hand-written table
(`aioesphomeapi.core.MESSAGE_TYPE_TO_PROTO`) — confirmed by reading
linux-voice-assistant's api_server.py, which imports that table directly
rather than deriving it. We don't have that module available since we're
vendoring only api_pb2.py/api_options_pb2.py (see ESPHOME_SPEC.md §5/§7.1
for why — avoiding the Noise-PSK crypto deps and the zeroconf version
bump that come with the full aioesphomeapi package).

Rather than hand-copy a second table that has to be kept in sync with
api_pb2.py by hand (and would silently drift if api_pb2.py is ever
regenerated from a newer api.proto), this module derives the table once
at import time directly from the descriptor `id` extension already
present on every message class. Verified to produce 148 unique,
collision-free entries against aioesphomeapi==45.3.1's api.proto.
"""

from __future__ import annotations

import logging

from esphome.vendor import api_options_pb2, api_pb2

log = logging.getLogger("echomuse.esphome.registry")

_ID_EXTENSION = api_options_pb2.id

# msg_type (int) -> message class
MESSAGE_TYPE_TO_CLASS: dict[int, type] = {}
# message class -> msg_type (int) — the inverse, for encoding outbound messages
CLASS_TO_MESSAGE_TYPE: dict[type, int] = {}


def _build_registry() -> None:
    for name in dir(api_pb2):
        cls = getattr(api_pb2, name)
        descriptor = getattr(cls, "DESCRIPTOR", None)
        if descriptor is None or not hasattr(descriptor, "GetOptions"):
            continue
        try:
            msg_id = descriptor.GetOptions().Extensions[_ID_EXTENSION]
        except Exception:
            continue
        if not msg_id:
            continue
        if msg_id in MESSAGE_TYPE_TO_CLASS:
            raise RuntimeError(
                f"msg_type collision while building registry: {msg_id} maps to "
                f"both {MESSAGE_TYPE_TO_CLASS[msg_id].__name__} and {name} — "
                f"this indicates api_pb2.py was regenerated with a protocol "
                f"change that this module's derivation logic no longer matches"
            )
        MESSAGE_TYPE_TO_CLASS[msg_id] = cls
        CLASS_TO_MESSAGE_TYPE[cls] = msg_id


_build_registry()
log.debug(f"Message registry built: {len(MESSAGE_TYPE_TO_CLASS)} message types")


def decode(msg_type: int, payload: bytes):
    """
    Decode a raw (msg_type, payload) pair into a protobuf message instance.

    Raises KeyError if msg_type is not a known message — callers should
    treat this as a protocol error (log + close connection), not retry.
    """
    cls = MESSAGE_TYPE_TO_CLASS[msg_type]
    return cls.FromString(payload)


def encode(msg) -> tuple[int, bytes]:
    """
    Encode a protobuf message instance into a (msg_type, payload) pair
    ready for frame_protocol.encode_frame() / PlaintextFrameProtocol.send_packet().
    """
    msg_type = CLASS_TO_MESSAGE_TYPE[type(msg)]
    return msg_type, msg.SerializeToString()
