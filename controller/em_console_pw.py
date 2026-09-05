"""
Hashing for the emOS USB console password.

The console is an unauthenticated root shell: anyone who plugs a cable into a
device on a shelf gets one, and can read the WiFi PSK out of
/data/misc/wifi/wpa_supplicant.conf. This puts a prompt in front of it.

**It is a nod to security, not Fort Knox.** The record lives on /data, so
recovery is deleting one file from TWRP — an inconvenience for a casual
opportunist, not a defence against anyone holding the device. That is the
intended level and it should not be "hardened" later into something more
complicated for a threat it was never meant to address.

So why hash at all, if deleting the file defeats it? Because the two are not
the same asset. The hash does not protect the DEVICE — it protects the
PASSWORD, which the owner has probably reused somewhere that matters. Someone
who dumps /data should walk away with work to do rather than with a credential.
For the same reason the controller hashes BEFORE the value is ever stored or
pushed, so the plaintext exists only in the browser and the request body and is
never written down at either end.

The algorithm is deliberately plain — salted SHA-256, iterated — because the
other half of the comparison runs in emOS's init, a static C binary that
cannot link a crypto library. bcrypt or scrypt would be better in the abstract
and are not available there; the iteration count is what buys margin instead.

Record format, self-describing so the count can change without stranding
devices that hold an older record:

    <iterations>:<salt hex>:<hash hex>

    h = sha256(salt_bytes + password_bytes)
    repeat iterations-1 times: h = sha256(h)
"""

from __future__ import annotations

import hashlib
import os

# MEASURED at 0.32s on the Echo's own A53 (EFF, 2026-09-05, emos/init/pwcheck
# built for the device and timed there) — unnoticeable at a login prompt, and
# enough that a dumped record is not trivially reversed. Stored in the record
# rather than assumed, so raising it later leaves fielded devices working
# against the record they already hold until they are next pushed to.
ITERATIONS = 100_000
SALT_BYTES = 8


def hash_password(password: str, salt: bytes | None = None,
                  iterations: int = ITERATIONS) -> str:
    """Hash a plaintext password into a storable record."""
    if not password:
        raise ValueError("refusing to hash an empty password")
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    if salt is None:
        salt = os.urandom(SALT_BYTES)
    h = hashlib.sha256(salt + password.encode("utf-8")).digest()
    for _ in range(iterations - 1):
        h = hashlib.sha256(h).digest()
    return f"{iterations}:{salt.hex()}:{h.hex()}"


def parse(record: str) -> tuple[int, bytes, bytes] | None:
    """Split a record into (iterations, salt, hash), or None if unusable."""
    if not record:
        return None
    parts = record.split(":")
    if len(parts) != 3:
        return None
    try:
        iterations = int(parts[0])
        salt = bytes.fromhex(parts[1])
        digest = bytes.fromhex(parts[2])
    except ValueError:
        return None
    if iterations < 1 or not salt or len(digest) != 32:
        return None
    return iterations, salt, digest


def verify(password: str, record: str) -> bool:
    """
    Whether a password matches a record.

    Not used by the controller — the device does the checking — but the C
    implementation in emOS has to agree with this one exactly, and a shared
    vector test is the only thing that can say so.
    """
    parsed = parse(record)
    if parsed is None or not password:
        return False
    iterations, salt, digest = parsed
    h = hashlib.sha256(salt + password.encode("utf-8")).digest()
    for _ in range(iterations - 1):
        h = hashlib.sha256(h).digest()
    # Constant time, out of habit rather than need: the attacker here has the
    # record already, so there is no timing side channel worth the name.
    return _equal(h, digest)


# What a client is shown in place of a stored record, and what it sends back
# unchanged. The record itself must never leave the controller: it is the whole
# point of hashing before storage that the value has no second home.
SENTINEL = "__unchanged__"


def for_display(record: str | None) -> str:
    """What to return to a client in place of the stored record."""
    return SENTINEL if is_set(record) else ""


def resolve_write(incoming, stored: str | None) -> str:
    """
    The record to store, given what a client sent and what is already there.

    Three cases and no fourth: the sentinel means leave it alone, empty means
    remove it, and anything else is a new plaintext password to hash. That last
    rule is deliberately blunt — a caller who sends a record verbatim gets it
    hashed AS a password rather than adopted, which is confusing exactly once
    and cannot silently install a record whose plaintext nobody knows.

    The sentinel is what makes read-modify-write work: a client GETs the
    config, sends it back, and the password survives without ever having been
    disclosed to it.
    """
    if incoming is None:
        return stored or ""
    text = str(incoming).strip()
    if text == SENTINEL:
        return stored or ""
    if not text:
        return ""
    return hash_password(text)


def is_set(record: str | None) -> bool:
    """Whether a stored config value represents a password being in force."""
    return parse((record or "").strip()) is not None


def _equal(a: bytes, b: bytes) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0
