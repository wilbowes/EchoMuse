"""
The encrypted ESPHome framing in frame_protocol.py, driven as a client would.

The initiator here is ours, so on its own this would only show the two halves
agree with each other. What pins them to the real protocol is elsewhere:
tests/test_noise.py holds the handshake to a published vector, and
tools/noise_client_check.py runs Home Assistant's own client against a
listener (right key, wrong key, no key). These tests are for the framing's
edges, which neither of those reaches: bytes arriving one at a time, the exact
refusal each bad opening gets, and nothing ever being sent in the clear.
"""

import hashlib
import struct

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: E402
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

from esphome import noise  # noqa: E402
from esphome.frame_protocol import (  # noqa: E402
    FrameProtocolError, PlaintextFrameProtocol, encode_frame)

KEY = bytes(range(32))
NAME, MAC = "echomuse-test-bt", "02ec0000abcd"


class Transport:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data):
        assert not self.closed, "wrote after close"
        self.written += data

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    def get_extra_info(self, name):
        return ("192.0.2.1", 5555) if name == "peername" else None

    def take(self):
        out, self.written = bytes(self.written), bytearray()
        return out


def listener(key=KEY):
    packets = []
    proto = PlaintextFrameProtocol(on_packet=lambda p, t, d: packets.append((t, d)))
    if key is not None:
        proto.set_encryption(key, NAME, MAC)
    transport = Transport()
    proto.connection_made(transport)
    return proto, transport, packets


def frame(data: bytes) -> bytes:
    return b"\x01" + struct.pack(">H", len(data)) + data


def frames(raw: bytes) -> list:
    out = []
    while raw:
        assert raw[0] == 0x01
        n = struct.unpack(">H", raw[1:3])[0]
        out.append(raw[3:3 + n])
        raw = raw[3 + n:]
    return out


class Initiator:
    """The client's half of NNpsk0, enough to drive the listener."""

    def __init__(self, psk=KEY, hello=b""):
        self.psk = psk
        self.h = hashlib.sha256(noise.PROTOCOL_NAME).digest()
        self.ck = self.h
        self._mix_hash(b"NoiseAPIInit" + struct.pack(">H", len(hello)) + hello)
        self.e = X25519PrivateKey.generate()
        self.n = 0

    def _mix_hash(self, data):
        self.h = hashlib.sha256(self.h + data).digest()

    def _mix_key(self, ikm):
        self.ck, self.k = noise._hkdf(self.ck, ikm, 2)
        self.n = 0

    def _nonce(self):
        self.n += 1
        return b"\x00" * 4 + struct.pack("<Q", self.n - 1)

    def message_1(self) -> bytes:
        self.ck, temp_h, self.k = noise._hkdf(self.ck, self.psk, 3)
        self._mix_hash(temp_h)
        pub = self.e.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._mix_hash(pub)
        self._mix_key(pub)
        tag = ChaCha20Poly1305(self.k).encrypt(self._nonce(), b"", self.h)
        self._mix_hash(tag)
        return pub + tag

    def read_message_2(self, msg: bytes):
        re = msg[:32]
        self._mix_hash(re)
        self._mix_key(re)
        self._mix_key(self.e.exchange(X25519PublicKey.from_public_bytes(re)))
        ChaCha20Poly1305(self.k).decrypt(self._nonce(), msg[32:], self.h)
        to_server, to_client = noise._hkdf(self.ck, b"", 2)
        self.send, self.recv = noise.CipherState(to_server), noise.CipherState(to_client)

    def data(self, msg_type: int, payload: bytes) -> bytes:
        return frame(self.send.encrypt(struct.pack(">HH", msg_type, len(payload)) + payload))

    def open(self, data: bytes):
        plain = self.recv.decrypt(data)
        msg_type, n = struct.unpack(">HH", plain[:4])
        return msg_type, plain[4:4 + n]


def connect(proto, transport, psk=KEY, hello=b""):
    client = Initiator(psk, hello)
    proto.data_received(frame(hello) + frame(b"\x00" + client.message_1()))
    hello_frame, handshake = frames(transport.take())
    assert hello_frame == b"\x01" + NAME.encode() + b"\x00" + MAC.encode() + b"\x00"
    assert handshake[0] == 0x00
    client.read_message_2(handshake[1:])
    return client


def test_session_delivered_one_byte_at_a_time():
    proto, transport, packets = listener()
    client = Initiator()
    opening = frame(b"") + frame(b"\x00" + client.message_1())
    for i in range(len(opening)):
        assert not proto.encrypted
        proto.data_received(opening[i:i + 1])
    assert proto.encrypted
    _, handshake = frames(transport.take())
    client.read_message_2(handshake[1:])

    wire = client.data(7, b"ping") + client.data(9, b"")
    for i in range(len(wire)):
        proto.data_received(wire[i:i + 1])
    assert packets == [(7, b"ping"), (9, b"")]

    proto.send_packets([(8, b"pong"), (10, b"x" * 300)])
    got = [client.open(f) for f in frames(transport.take())]
    assert got == [(8, b"pong"), (10, b"x" * 300)]
    assert not transport.closed


def test_plaintext_client_is_told_encryption_is_required():
    proto, transport, packets = listener()
    proto.data_received(encode_frame(1, b"hello"))
    # Opens with 0x01, which is the whole signal to a plaintext client, and
    # carries the reason ESPHome's firmware gives.
    assert transport.written == frame(b"\x01Bad indicator byte")
    assert transport.closed and packets == []


def test_wrong_key_gets_the_reason_home_assistant_keys_on():
    proto, transport, packets = listener()
    client = Initiator(psk=bytes(32))
    proto.data_received(frame(b"") + frame(b"\x00" + client.message_1()))
    hello_frame, reject = frames(transport.take())
    assert hello_frame.startswith(b"\x01" + NAME.encode())
    assert reject == b"\x01Handshake MAC failure"
    assert transport.closed and packets == [] and not proto.encrypted


@pytest.mark.parametrize("handshake,reason", [
    (b"", b"Bad handshake packet len"),
    (b"\x01" + bytes(48), b"Bad handshake error byte"),
    (b"\x00" + bytes(47), b"Handshake MAC failure"),      # too short to verify
    (b"\x00" + bytes(48), b"Handshake MAC failure"),
])
def test_bad_handshake_frames(handshake, reason):
    proto, transport, packets = listener()
    proto.data_received(frame(b"") + frame(handshake))
    assert frames(transport.take())[-1] == b"\x01" + reason
    assert transport.closed and packets == []


def test_nothing_is_sent_before_the_handshake_finishes():
    proto, transport, _ = listener()
    proto.send_packet(1, b"device info")
    assert transport.written == b""
    proto.data_received(frame(b""))
    transport.take()                      # the server hello
    proto.send_packet(1, b"device info")
    assert transport.written == b""


def test_the_client_hello_is_bound_into_the_handshake():
    # Same key, but the client and the listener disagree about what the
    # hello frame held: the handshake must fail.
    proto, transport, _ = listener()
    connect(proto, transport, hello=b"future")
    assert proto.encrypted

    proto, transport, _ = listener()
    client = Initiator(hello=b"")
    proto.data_received(frame(b"future") + frame(b"\x00" + client.message_1()))
    assert frames(transport.take())[-1] == b"\x01Handshake MAC failure"


def test_tampered_or_replayed_data_closes_the_connection():
    for mutate in ("flip", "replay"):
        proto, transport, packets = listener()
        client = connect(proto, transport)
        wire = bytearray(client.data(7, b"ping"))
        if mutate == "flip":
            wire[-1] ^= 0x01
            proto.data_received(bytes(wire))
            assert packets == []
        else:
            proto.data_received(bytes(wire))
            proto.data_received(bytes(wire))
            assert packets == [(7, b"ping")]
        assert transport.closed


@pytest.mark.parametrize("plain", [
    b"",                                         # no header at all
    b"\x00\x07\x00",                             # header cut short
    struct.pack(">HH", 7, 5) + b"ping",          # says 5, carries 4
])
def test_malformed_inner_frame_closes_the_connection(plain):
    proto, transport, packets = listener()
    client = connect(proto, transport)
    proto.data_received(frame(client.send.encrypt(plain)))
    assert transport.closed and packets == []


def test_largest_message_and_one_byte_over():
    proto, transport, _ = listener()
    client = connect(proto, transport)
    biggest = 65535 - 4 - noise.TAG_LEN
    proto.send_packet(1, bytes(biggest))
    assert client.open(frames(transport.take())[0]) == (1, bytes(biggest))
    with pytest.raises(FrameProtocolError):
        proto.send_packet(1, bytes(biggest + 1))


def test_key_must_be_32_bytes():
    proto = PlaintextFrameProtocol(on_packet=lambda *a: None)
    for n in (0, 16, 31, 33):
        with pytest.raises(ValueError):
            proto.set_encryption(bytes(n), NAME, MAC)


def test_a_plaintext_listener_is_unchanged():
    proto, transport, packets = listener(key=None)
    proto.data_received(encode_frame(1, b"hello") + encode_frame(7, b""))
    assert packets == [(1, b"hello"), (7, b"")]
    proto.send_packet(2, b"world")
    assert transport.written == encode_frame(2, b"world")
    # And it refuses an encrypted client rather than misreading it — saying
    # so in plaintext first, which is how Home Assistant learns the device no
    # longer uses a key (ESPHome's firmware sends exactly these bytes).
    transport.take()
    proto.data_received(frame(b""))
    assert transport.written == b"\x00Bad indicator byte"
    assert transport.closed
