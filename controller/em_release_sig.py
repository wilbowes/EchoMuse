"""
em_release_sig.py — release signatures on what the controller installs.

Every firmware binary and emOS init the controller pushes to an Echo comes off
a GitHub release. Without this, whoever can publish a release (a stolen login,
a leaked token, a compromised workflow) can put code on every Echo that
updates. A signature made with a key that never touches GitHub closes that:
the controller refuses a release asset whose signature does not verify against
a key compiled in here.

The controller is the right place for the check, not the Echo: it holds a root
shell on every device, so a compromised controller owns the device whatever
the device checks.

Scope: assets downloaded from a GitHub release (`/releases/download/<tag>/
<name>`). A binary the user uploads to their own controller is their choice and
is not checked. The controller image itself is outside this (HA no longer
verifies add-on image signatures); the repo's rulesets cover it.

Wire format: `<asset>.sig` beside the asset, one base64 Ed25519 signature per
line (several lines = several keys, e.g. a spare). Each signs
`message(tag, name, data)`: the tag and asset name are bound in, so a signed
old binary cannot be replayed under a new tag or as a different asset.

KEYS empty = not enforced yet (the real key is chosen before the first signed
release, 2026-11-13). With a key present, a missing or bad signature refuses.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from urllib.parse import unquote, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Raw 32-byte Ed25519 public keys, base64. Adding one turns enforcement on.
KEYS: tuple[str, ...] = ()

DOMAIN = b"echomuse-release-sig-v1"
SIG_SUFFIX = ".sig"
MAX_SIG_BYTES = 4096

_ASSET_PATH = re.compile(r"^/[^/]+/[^/]+/releases/download/([^/]+)/([^/]+)$")


def enforced(keys: tuple[str, ...] | None = None) -> bool:
    return bool(KEYS if keys is None else keys)


def asset_of(url: str) -> tuple[str, str] | None:
    """(tag, asset name) of a GitHub release download URL, or None."""
    m = _ASSET_PATH.match(urlparse(url).path)
    return (unquote(m.group(1)), unquote(m.group(2))) if m else None


def message(tag: str, name: str, data: bytes) -> bytes:
    return b"\0".join((DOMAIN, tag.encode(), name.encode(),
                       hashlib.sha256(data).digest()))


def problem(url: str, data: bytes, sig: bytes | None,
            keys: tuple[str, ...] | None = None) -> str | None:
    """None if `data` from `url` carries a valid signature, else why not."""
    keys = KEYS if keys is None else keys
    if not keys:
        return None
    asset = asset_of(url)
    if asset is None:
        return "not a GitHub release asset, so it cannot be verified"
    tag, name = asset
    if not sig:
        return f"{name} in {tag} is not signed"
    if len(sig) > MAX_SIG_BYTES:
        return f"{name}{SIG_SUFFIX} in {tag} is too large to be a signature"
    msg = message(tag, name, data)
    for line in sig.decode("ascii", "replace").split():
        try:
            raw = base64.b64decode(line, validate=True)
        except (binascii.Error, ValueError):
            continue
        for key in keys:
            try:
                Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(raw, msg)
                return None
            except (InvalidSignature, ValueError):
                continue
    return f"{name} in {tag} has no valid signature"
