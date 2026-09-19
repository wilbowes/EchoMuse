"""
What a WiFi network may be called, and what its password may be.

An SSID is 0-32 arbitrary octets (IEEE 802.11) — spaces, quotes,
backslashes, UTF-8 and bytes that are not text at all are all valid — so it
travels as bytes: as `ssid_hex` on the wire when the dashboard has the exact
bytes from a scan, otherwise as the UTF-8 of the name typed. A WPA2-Personal
passphrase is 8-63 printable ASCII characters, or a raw 64-hex PSK.

Pure, and mirrored by the firmware (device/internal/wifi/ssid.go) and the
dashboard (_ssidProblem, _pskProblem). The controller checks first so an
obvious mistake fails in the request instead of after a full switch and
rollback on the device; the device checks again because it is the one writing
the config.
"""
import re

_HEX_PSK = re.compile(r"[0-9a-fA-F]{64}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})*")


def ssid_bytes(ssid: str, ssid_hex: str | None = None) -> bytes:
    """
    The SSID to join: `ssid_hex` decoded when given, else `ssid` as UTF-8.

    Raises ValueError for malformed hex, rather than falling back to the name:
    a request that named exact bytes and got them wrong must not quietly join
    something else.
    """
    if ssid_hex:
        if not _HEX.fullmatch(ssid_hex):
            raise ValueError("ssid_hex is not a hex string")
        return bytes.fromhex(ssid_hex)
    return ssid.encode("utf-8")


def problem(ssid: bytes, psk: str) -> str | None:
    """Why this network cannot be configured, or None."""
    if not ssid or not any(ssid):
        return "empty SSID"
    if len(ssid) > 32:
        return f"SSID is {len(ssid)} bytes; the limit is 32"
    if not psk or _HEX_PSK.fullmatch(psk):
        return None
    if not 8 <= len(psk) <= 63:
        return f"WPA passphrase must be 8–63 characters (got {len(psk)})"
    if any(not 0x20 <= ord(c) <= 0x7E for c in psk):
        return "WPA passphrase may only contain printable ASCII characters"
    return None
