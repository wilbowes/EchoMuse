"""
em_endpoints.py — the controller address list devices dial before mDNS
======================================================================

`controllerEndpoints` is a fleet-only setting (Config → Advanced): an ordered
list of addresses an Echo tries before falling back to mDNS. The firmware
half is #166 (v2.16.0+): the device reads
/data/local/etc/echomuse/controller.json fresh on every dial, tries each entry
twice, then makes one bounded mDNS attempt. This module validates the setting
and renders that file; em_api delivers it over the shell plane and the
provisioning wizard writes it over adb.

Three rules:

- **mDNS stays on.** The file never carries `"mdns": false`, so a wrong
  address costs a slower reconnect, never a stranded Echo. `mdns:false` is
  for a pinned test fleet and stays a hand edit.
- **The controller only removes a file it wrote.** #166 documented writing
  controller.json by hand, for exactly the routed and tunnelled devices that
  cannot use mDNS; deleting one of those because this setting is empty would
  strand the device it exists for. Files written here carry MANAGED_KEY,
  which the firmware's JSON decoder ignores.
- **Hosts are checked against what the device can dial**: an IPv4 or IPv6
  literal, or a DNS name to RFC 1123 (labels of 1-63 letters, digits and
  hyphens, no leading or trailing hyphen, 253 characters in all). A dotted
  name whose last label is all digits is refused as a mistyped IP, since no
  top-level domain is numeric (RFC 3696 §2).

Pure: tested without aiohttp (tests/test_endpoints.py).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re

DEVICE_PATH = "/data/local/etc/echomuse/controller.json"
MANAGED_KEY = "managed_by"
MANAGED_BY = "echomuse-controller"

# Each entry is dialled twice per pass, so the list bounds how long a device
# with a stale list takes to reach mDNS.
MAX_ENDPOINTS = 8

_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def host_problem(host: str) -> str | None:
    """Why `host` cannot be dialled, or None."""
    if not host:
        return "is empty"
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(h)
        return None
    except ValueError:
        pass
    if ":" in h:
        return "is not a valid IPv6 address"
    name = h[:-1] if h.endswith(".") else h
    if len(name) > 253:
        return "is longer than 253 characters"
    labels = name.split(".")
    for label in labels:
        if not _LABEL.match(label):
            return (f"has an invalid part '{label}' — letters, digits and "
                    f"hyphens only, 1-63 characters, not starting or ending "
                    f"with a hyphen")
    if len(labels) > 1 and labels[-1].isdigit():
        return "is not a valid IP address"
    return None


def normalise(entries, default_port: int, default_tls_port: int) -> tuple[list[dict], str | None]:
    """
    The stored form of a submitted list, or an error naming the bad entry.

    Each entry is {host, port, tlsPort}; a missing port takes the
    controller's own, and tlsPort 0 means dial plain.
    """
    if entries is None:
        return [], None
    if not isinstance(entries, list):
        return [], "must be a list"
    if len(entries) > MAX_ENDPOINTS:
        return [], f"has more than {MAX_ENDPOINTS} addresses"
    out, seen = [], set()
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            return [], f"address {i} is not an object"
        host = str(e.get("host") or "").strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if (why := host_problem(host)):
            return [], f"address {i} ('{host}') {why}"
        port = _port(e.get("port"), default_port)
        tls = _port(e.get("tlsPort"), default_tls_port, allow_zero=True)
        if port is None:
            return [], f"address {i}: port must be 1-65535"
        if tls is None:
            return [], f"address {i}: TLS port must be 0 (off) or 1-65535"
        key = (host.lower().rstrip("."), port)
        if key in seen:
            return [], f"address {i} ('{host}:{port}') is listed twice"
        seen.add(key)
        out.append({"host": host, "port": port, "tlsPort": tls})
    return out, None


def _port(v, default: int, allow_zero: bool = False) -> int | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return None
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    if p != v and str(p) != str(v).strip():
        return None  # 80.5, "8o"
    if p == 0 and allow_zero:
        return 0
    return p if 1 <= p <= 65535 else None


def file_bytes(entries: list[dict]) -> bytes | None:
    """controller.json for the device, or None when there is nothing to set."""
    if not entries:
        return None
    doc = {
        MANAGED_KEY: MANAGED_BY,
        "endpoints": [{"host": e["host"], "port": e["port"], "tls_port": e["tlsPort"]}
                      for e in entries],
    }
    return (json.dumps(doc, separators=(",", ":")) + "\n").encode("ascii")


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
