"""
The Noise responder against a published vector and against its own edges.

The vector is from cacophony's test set (the Haskell Noise implementation),
as carried in snow's `tests/vectors/cacophony.txt`: the one entry for
Noise_NNpsk0_25519_ChaChaPoly_SHA256. A responder that handshakes with a copy
of itself proves nothing — leave out the extra MixKey a PSK handshake needs on
each `e` and both sides still agree — so the fixed ephemeral key and the
expected bytes of every message are what make this a test of the protocol.
"""

import pytest

pytest.importorskip("cryptography")

from esphome import noise  # noqa: E402

PROLOGUE = bytes.fromhex("4a6f686e2047616c74")
PSK = bytes.fromhex("54686973206973206d7920417573747269616e20706572737065637469766521")
RESP_EPHEMERAL = bytes.fromhex("bbdb4cdbd309f1a1f2e1456967fe288cadd6f712d65dc7b7793d5e63da6b375b")
HANDSHAKE_HASH = bytes.fromhex("f4d03dc34495c95729ea6de9e1b59004b59733102488b3e24bc441e0be208eaf")
# (payload, ciphertext): initiator, responder, then alternating transport.
MESSAGES = [(bytes.fromhex(p), bytes.fromhex(c)) for p, c in [
    ("4c756477696720766f6e204d69736573",
     "ca35def5ae56cec33dc2036731ab14896bc4c75dbb07a61f879f8e3afa4c794479b962b8aff8485742ac32f905ba45369e2465fb59e138a93d67a0d1266b6a54"),
    ("4d757272617920526f746862617264",
     "95ebc60d2b1fa672c1f46a8aa265ef51bfe38e7ccb39ec5be34069f144808843d6062704d5a9c422a8e834423f8c1feada7e8d0d910a1a2cd030fb584221e3"),
    ("462e20412e20486179656b",
     "e632c3763d7669067383433197a3baddf146e9e70ad4b4e9e59e0f"),
    ("4361726c204d656e676572",
     "64c6bee32ea91c8474bb4c21d7a700109ad45af77b29764ba5eb1e"),
    ("4a65616e2d426170746973746520536179",
     "e2fa0bed0603b62d3ccac2ecabbf3fe33f3e86514909b323361626266cb2471cc8"),
    ("457567656e2042f6686d20766f6e2042617765726b",
     "0c01dc9cec1fe4ddd692e8dd32188aa351088dc91183639a53b57aa4692b5ebdef8b8ca111"),
]]


def _responder(psk=PSK, prologue=PROLOGUE):
    return noise.Responder(psk, prologue, ephemeral=RESP_EPHEMERAL)


def _handshake():
    r = _responder()
    r.read_message_1(MESSAGES[0][1])
    r.write_message_2(MESSAGES[1][0])
    return r.split()


def test_published_vector_handshake():
    r = _responder()
    assert r.read_message_1(MESSAGES[0][1]) == MESSAGES[0][0]
    assert r.write_message_2(MESSAGES[1][0]) == MESSAGES[1][1]
    assert r.handshake_hash == HANDSHAKE_HASH


def test_published_vector_transport():
    recv, send = _handshake()
    # Initiator and responder alternate, each direction on its own counter.
    assert recv.decrypt(MESSAGES[2][1]) == MESSAGES[2][0]
    assert send.encrypt(MESSAGES[3][0]) == MESSAGES[3][1]
    assert recv.decrypt(MESSAGES[4][1]) == MESSAGES[4][0]
    assert send.encrypt(MESSAGES[5][0]) == MESSAGES[5][1]


def test_wrong_key_fails_at_the_first_message():
    wrong = bytes(32)
    with pytest.raises(noise.NoiseError):
        _responder(psk=wrong).read_message_1(MESSAGES[0][1])


def test_wrong_prologue_fails():
    # The prologue binds the handshake to what was said before it; ESPHome
    # puts the client's hello frame there.
    with pytest.raises(noise.NoiseError):
        _responder(prologue=PROLOGUE + b"x").read_message_1(MESSAGES[0][1])


@pytest.mark.parametrize("n", [0, 1, 31, 32, 47])
def test_first_message_too_short(n):
    # 32 bytes of ephemeral key and a 16-byte tag is the minimum.
    with pytest.raises(noise.NoiseError):
        _responder().read_message_1(MESSAGES[0][1][:n])


def test_first_message_minimum_is_an_empty_payload():
    # What Home Assistant sends: e, and the tag over an empty payload. Built
    # from the vector's initiator key by tampering is not possible, so check
    # the length rule at its edge instead: 48 bytes is accepted as a length
    # and rejected only for failing authentication.
    with pytest.raises(noise.NoiseError, match="authentication"):
        _responder().read_message_1(MESSAGES[0][1][:48])


@pytest.mark.parametrize("at", [0, 31, 32, 63])
def test_tampered_first_message_fails(at):
    msg = bytearray(MESSAGES[0][1])
    msg[at] ^= 0x01
    with pytest.raises(noise.NoiseError):
        r = _responder()
        r.read_message_1(bytes(msg))
        # A changed ephemeral key can only show in the tag, which the line
        # above checks; reaching here means it was accepted.


def test_low_order_public_key_is_refused():
    # An all-zero X25519 public key makes the shared secret all zeros
    # whatever our key is. The handshake must not complete on it.
    import hashlib
    import hmac as hmac_mod
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    def hkdf(ck, ikm, n):
        temp = hmac_mod.new(ck, ikm, hashlib.sha256).digest()
        out, prev = [], b""
        for i in range(1, n + 1):
            prev = hmac_mod.new(temp, prev + bytes((i,)), hashlib.sha256).digest()
            out.append(prev)
        return out

    # An initiator's first message, built by hand with e = 0.
    h = hashlib.sha256(noise.PROTOCOL_NAME).digest()
    ck = h
    h = hashlib.sha256(h + PROLOGUE).digest()
    ck, temp_h, _ = hkdf(ck, PSK, 3)
    h = hashlib.sha256(h + temp_h).digest()
    e = bytes(32)
    h = hashlib.sha256(h + e).digest()
    ck, k = hkdf(ck, e, 2)
    tag = ChaCha20Poly1305(k).encrypt(bytes(12), b"", h)

    r = noise.Responder(PSK, PROLOGUE)
    assert r.read_message_1(e + tag) == b""      # well-formed and authentic
    with pytest.raises(noise.NoiseError, match="public key"):
        r.write_message_2()


def test_messages_out_of_order():
    with pytest.raises(noise.NoiseError):
        _responder().write_message_2()
    with pytest.raises(noise.NoiseError):
        _responder().split()
    r = _responder()
    r.read_message_1(MESSAGES[0][1])
    with pytest.raises(noise.NoiseError):
        r.read_message_1(MESSAGES[0][1])
    with pytest.raises(noise.NoiseError):
        r.split()
    r.write_message_2()
    r.split()
    with pytest.raises(noise.NoiseError):
        r.split()


@pytest.mark.parametrize("n", [0, 31, 33, 64])
def test_key_must_be_32_bytes(n):
    with pytest.raises(ValueError):
        noise.Responder(bytes(n), b"")


def test_transport_rejects_tampering_replay_and_truncation():
    recv, _ = _handshake()
    bad = bytearray(MESSAGES[2][1])
    bad[0] ^= 0x01
    with pytest.raises(noise.NoiseError):
        recv.decrypt(bytes(bad))

    recv, _ = _handshake()
    assert recv.decrypt(MESSAGES[2][1]) == MESSAGES[2][0]
    with pytest.raises(noise.NoiseError):
        recv.decrypt(MESSAGES[2][1])            # the counter has moved on

    recv, _ = _handshake()
    for n in (0, 15):
        with pytest.raises(noise.NoiseError):
            recv.decrypt(MESSAGES[2][1][:n])


def test_transport_round_trip_sizes():
    # Empty and the largest payload a frame can carry (65535 less the tag).
    recv, send = _handshake()
    for size in (0, 1, 65535 - noise.TAG_LEN):
        ciphertext = send.encrypt(bytes(size))
        assert len(ciphertext) == size + noise.TAG_LEN


def test_each_handshake_uses_a_fresh_ephemeral_key():
    replies = set()
    for _ in range(3):
        r = noise.Responder(PSK, PROLOGUE)
        r.read_message_1(MESSAGES[0][1])
        replies.add(r.write_message_2()[:32])
    assert len(replies) == 3
