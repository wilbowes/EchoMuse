"""
em_hostip.py — resolving the LAN address the controller advertises.

SERVER_IP is the address devices are told to dial, over mDNS and in the
ESPHome device info. Nothing validates it against reality: the controller
advertises whatever it is given, devices believe it, and a wrong value
produces a controller that is running perfectly and that no device can
reach. There is no error anywhere in that chain — the device simply never
arrives, which reads as a broken device rather than a mistyped setting.

That is why this module refuses rather than guesses. It used to hold a
literal fallback — a developer's own machine, checked in — so any
deployment that did not set SERVER_IP advertised a real, routable address
on somebody else's network. The Home Assistant add-on ships
`server_ip: ""` and em_start.py deliberately passes empty values through
to "the controller's own fallback" — so a fresh add-on install with the
field left blank sent the whole fleet to that address.

Detection is a plain routing-table lookup: a UDP socket `connect()` sets
the route and picks a source address without sending a single packet.
The destination is 192.0.2.1 — TEST-NET-1 from RFC 5737, reserved for
documentation and never routed to a real host. A public resolver address
would work identically, and is the usual way this trick is written, but it
is indistinguishable from a phone-home in a firewall log or to anyone
reading this file. This project does not make outbound calls and the code
should not need a comment to prove it.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from functools import lru_cache

log = logging.getLogger("echomuse.hostip")

# RFC 5737 TEST-NET-1. Never routed; no packet is sent. See module docstring.
_ROUTE_PROBE = ("192.0.2.1", 9)


class ServerIPError(RuntimeError):
    """SERVER_IP could not be resolved. Carries the operator-facing fix."""


def detect() -> str | None:
    """
    The source address the kernel would use to reach the LAN, or None if
    there is no usable route. Never raises — a failure here is an ordinary
    condition that `resolve` turns into a message.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(_ROUTE_PROBE)
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def resolve(configured: str | None, detected: str | None) -> tuple[str, str]:
    """
    Decide the advertised address. Pure, so the policy is testable without
    a network. Returns (address, source) where source is "configured" or
    "detected"; raises ServerIPError with an actionable message otherwise.
    """
    value = (configured or "").strip()

    if value:
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            # Refused rather than repaired. inet_aton accepts several of
            # these — a three-part form has its last part read as a 16-bit
            # tail — and would advertise an address nobody typed, which is
            # the same silent wrong answer this module exists to prevent.
            raise ServerIPError(
                f"SERVER_IP is set to {value!r}, which is not an IPv4 "
                "address. Set it to this host's LAN address (for the Home "
                "Assistant add-on: the Server IP option), or leave it empty "
                "to detect it automatically."
            ) from None
        return value, "configured"

    if detected:
        return detected, "detected"

    raise ServerIPError(
        "SERVER_IP is not set and this host's LAN address could not be "
        "detected (no default route). Set SERVER_IP to the address devices "
        "should dial — for the Home Assistant add-on, the Server IP option. "
        "Refusing to start rather than advertise an address no device can "
        "reach."
    )


@lru_cache(maxsize=1)
def _resolved(configured: str | None) -> tuple[str, str]:
    return resolve(configured, detect())


def server_ip(configured: str | None) -> str:
    """
    Resolve and log once. Cached because em_controller and em_esphome both
    need the answer and the log line should appear once, not per importer.
    """
    address, source = _resolved(configured)
    if source == "detected":
        log.info(
            "SERVER_IP not set — advertising detected LAN address %s. "
            "Set it explicitly if this host has more than one interface.",
            address,
        )
        if looks_containerised(address):
            # A detected address is more dangerous than a hardcoded one when
            # it is wrong, because it looks authoritative. This is the shape
            # that fails: a container without host networking detects its own
            # bridge address, advertises it over mDNS, and every device dials
            # somewhere unreachable — with the controller apparently healthy.
            log.warning(
                "SERVER_IP was detected as %s, which looks like a container "
                "bridge address rather than a LAN address. Devices on the "
                "network will not be able to reach it. Run with host "
                "networking, or set SERVER_IP to this host's LAN address.",
                address,
            )
    return address


# Docker's default bridge pool. Deliberately NOT the whole of 172.16/12 or
# 10/8 — both are ordinary LAN ranges, and a false warning on a working
# install is worse than no warning, because it teaches people to ignore it.
_BRIDGE_NETS = ("172.17.", "172.18.", "172.19.", "172.20.",
                "172.21.", "172.22.", "172.23.")


def looks_containerised(address: str) -> bool:
    """True if the address is in Docker's default bridge pool."""
    return address.startswith(_BRIDGE_NETS)


def bind_mdns(make, address: str):
    """
    An mDNS responder on `address`'s interface alone: `make(interfaces=[address])`.

    zeroconf's default opens a socket on every interface the host has, and a
    send failing on one that mDNS never needed (a monitoring VLAN, #604)
    surfaces in the event loop and stalls it.

    Falls back to `make()`, every interface, when `address` is not one of this
    host's: zeroconf raises OSError(ENODEV) for an address that is not local
    and ValueError for one that is not an address, and either would otherwise
    stop the controller at startup over a stale SERVER_IP.
    """
    try:
        return make(interfaces=[address])
    except (OSError, ValueError) as e:
        log.warning(
            f"SERVER_IP {address} is not an address on this host ({e}) — "
            f"advertising mDNS on every interface instead")
        return make()

