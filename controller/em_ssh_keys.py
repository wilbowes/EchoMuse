"""
The SSH keys an owner gives one Echo, checked before they are stored.

The Echo checks them again (device/internal/config/ssh.go, ParseSSHKeys) and
the two must agree: tests/test_ssh_keys.py and the Go test both run
device/internal/config/testdata/ssh_keys.json.

Ed25519 only, the one type tinysshd accepts, checked against its wire form
(RFC 8709 §4, RFC 4253 §6.6): string "ssh-ed25519", then a string of exactly
32 octets, nothing after. Options ("from=…", "command=…") are refused because
tinysshd would not enforce them. Blank lines and # comments are dropped;
duplicates are kept once.
"""

from __future__ import annotations

import base64
import binascii

MAX_KEYS = 16
MAX_COMMENT = 100
KEY_TYPE = "ssh-ed25519"


class KeyError_(ValueError):
    """A list refused, with the 1-based line it names (0 = the whole list)."""

    def __init__(self, line: int, msg: str):
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.line = line


def _ssh_string(b: bytes) -> tuple[bytes, bytes] | None:
    if len(b) < 4:
        return None
    n = int.from_bytes(b[:4], "big")
    if n > len(b) - 4:
        return None
    return b[4:4 + n], b[4 + n:]


def _check_blob(b64: str) -> str | None:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return "key is not valid base64"
    # Strict: the canonical encoding only, as Go's StdEncoding.Strict().
    if base64.b64encode(raw).decode() != b64:
        return "key is not valid base64"
    first = _ssh_string(raw)
    if first is None or first[0] != KEY_TYPE.encode():
        return "key data is not an ssh-ed25519 key"
    second = _ssh_string(first[1])
    if second is None or len(second[0]) != 32:
        return "an Ed25519 public key is 32 bytes"
    if second[1]:
        return "trailing bytes after the key"
    return None


def parse(text: str) -> list[str]:
    """One normalised line per distinct key, or KeyError_ naming the line."""
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate((text or "").split("\n"), start=1):
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0] != KEY_TYPE:
            raise KeyError_(i, "only ssh-ed25519 keys are accepted")
        if len(fields) < 2:
            raise KeyError_(i, "no key after ssh-ed25519")
        if (err := _check_blob(fields[1])):
            raise KeyError_(i, err)
        comment = " ".join(fields[2:])
        if len(comment) > MAX_COMMENT:
            raise KeyError_(i, f"comment longer than {MAX_COMMENT} characters")
        if any(not (0x20 <= ord(c) <= 0x7E) for c in comment):
            raise KeyError_(i, "comment must be printable ASCII")
        if fields[1] in seen:
            continue
        seen.add(fields[1])
        out.append(f"{KEY_TYPE} {fields[1]}" + (f" {comment}" if comment else ""))
        if len(out) > MAX_KEYS:
            raise KeyError_(0, f"more than {MAX_KEYS} keys")
    return out
