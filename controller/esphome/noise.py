"""
noise.py — the responder half of Noise_NNpsk0_25519_ChaChaPoly_SHA256
=====================================================================

ESPHome's native API encryption, server side. Home Assistant is the
initiator; an ESPHome device — here, one of our listeners — is the responder.
Pure logic, no sockets: frame_protocol.py carries the bytes.

Written from the Noise Protocol Framework specification (rev 34), sections 5
(processing rules) and 9 (pre-shared symmetric keys):

    NNpsk0:
      -> psk, e
      <- e, ee

It exists because an active Bluetooth proxy (#656) lets whoever reaches the
proxy's port operate the devices behind it, and that port had no
authentication at all. The pre-shared key is what Home Assistant asks for as
the device's "encryption key".

Only what this one pattern needs is here. In a PSK handshake an `e` token
also mixes the ephemeral public key into the chaining key (section 9.2); that
line is easy to leave out and the result still handshakes with itself, which
is why the tests use published vectors and the real client.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
KEY_LEN = 32
DH_LEN = 32
TAG_LEN = 16
# A Noise message is at most 65535 bytes; so is an ESPHome frame.
MAX_MESSAGE_LEN = 65535
_MAX_NONCE = 2 ** 64 - 1


class NoiseError(Exception):
    """The handshake or a transport message did not verify."""


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, n: int) -> tuple:
    temp = _hmac(chaining_key, ikm)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = _hmac(temp, prev + bytes((i,)))
        out.append(prev)
    return tuple(out)


class CipherState:
    """One direction of the transport: a key and a counter nonce."""

    def __init__(self, key: bytes) -> None:
        self._aead = ChaCha20Poly1305(key)
        self._n = 0

    def _nonce(self) -> bytes:
        if self._n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        # ChaChaPoly: 32 bits of zeros, then the counter little-endian.
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", self._n)
        self._n += 1
        return nonce

    def encrypt(self, plaintext: bytes, ad: bytes = b"") -> bytes:
        return self._aead.encrypt(self._nonce(), plaintext, ad)

    def decrypt(self, ciphertext: bytes, ad: bytes = b"") -> bytes:
        if len(ciphertext) < TAG_LEN:
            raise NoiseError("message shorter than its authentication tag")
        nonce = self._nonce()
        try:
            return self._aead.decrypt(nonce, ciphertext, ad)
        except InvalidTag:
            raise NoiseError("message failed authentication") from None


class Responder:
    """
    The responder's side of one NNpsk0 handshake.

        r = Responder(psk, prologue)
        payload = r.read_message_1(msg)      # raises NoiseError on a wrong key
        reply   = r.write_message_2()
        recv, send = r.split()

    `ephemeral` fixes the ephemeral private key, for test vectors only.
    """

    def __init__(self, psk: bytes, prologue: bytes,
                 ephemeral: Optional[bytes] = None) -> None:
        if len(psk) != KEY_LEN:
            raise ValueError("the pre-shared key must be 32 bytes")
        self._psk = psk
        self._ephemeral = ephemeral
        # len(PROTOCOL_NAME) > 32, so h starts as its hash.
        self._h = hashlib.sha256(PROTOCOL_NAME).digest()
        self._ck = self._h
        self._k: Optional[bytes] = None
        self._n = 0
        self._re: Optional[bytes] = None
        self._stage = 0
        self._mix_hash(prologue)

    # ── symmetric state ───────────────────────────────────────────────────

    def _mix_hash(self, data: bytes) -> None:
        self._h = hashlib.sha256(self._h + data).digest()

    def _mix_key(self, ikm: bytes) -> None:
        self._ck, self._k = _hkdf(self._ck, ikm, 2)
        self._n = 0

    def _mix_key_and_hash(self, ikm: bytes) -> None:
        self._ck, temp_h, self._k = _hkdf(self._ck, ikm, 3)
        self._mix_hash(temp_h)
        self._n = 0

    def _nonce(self) -> bytes:
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", self._n)
        self._n += 1
        return nonce

    def _encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = ChaCha20Poly1305(self._k).encrypt(self._nonce(), plaintext, self._h)
        self._mix_hash(ciphertext)
        return ciphertext

    def _decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) < TAG_LEN:
            raise NoiseError("handshake message shorter than its tag")
        try:
            plaintext = ChaCha20Poly1305(self._k).decrypt(self._nonce(), ciphertext, self._h)
        except InvalidTag:
            # The first thing the key authenticates. A wrong pre-shared key
            # shows up here and nowhere earlier.
            raise NoiseError("handshake failed authentication") from None
        self._mix_hash(ciphertext)
        return plaintext

    # ── the two messages ──────────────────────────────────────────────────

    def read_message_1(self, message: bytes) -> bytes:
        """-> psk, e   Returns the initiator's payload."""
        if self._stage != 0:
            raise NoiseError("handshake message out of order")
        if len(message) < DH_LEN + TAG_LEN or len(message) > MAX_MESSAGE_LEN:
            raise NoiseError("handshake message has the wrong length")
        self._mix_key_and_hash(self._psk)
        self._re = message[:DH_LEN]
        self._mix_hash(self._re)
        self._mix_key(self._re)
        payload = self._decrypt_and_hash(message[DH_LEN:])
        self._stage = 1
        return payload

    def write_message_2(self, payload: bytes = b"") -> bytes:
        """<- e, ee"""
        if self._stage != 1:
            raise NoiseError("handshake message out of order")
        if self._ephemeral is not None:
            e = X25519PrivateKey.from_private_bytes(self._ephemeral)
        else:
            e = X25519PrivateKey.generate()
        e_pub = e.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._mix_hash(e_pub)
        self._mix_key(e_pub)
        try:
            shared = e.exchange(X25519PublicKey.from_public_bytes(self._re))
        except ValueError:
            # A low-order point: the exchange would produce all zeros.
            raise NoiseError("initiator sent an invalid public key") from None
        self._mix_key(shared)
        message = e_pub + self._encrypt_and_hash(payload)
        self._stage = 2
        return message

    def split(self) -> tuple:
        """(receive, send) cipher states for the transport phase."""
        if self._stage != 2:
            raise NoiseError("handshake is not finished")
        initiator_to_responder, responder_to_initiator = _hkdf(self._ck, b"", 2)
        self._stage = 3
        return CipherState(initiator_to_responder), CipherState(responder_to_initiator)

    @property
    def handshake_hash(self) -> bytes:
        return self._h
