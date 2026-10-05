"""
Tests for em_hostip — the SERVER_IP resolution policy.

The bug these exist for produced no error anywhere: an unconfigured
controller advertised a hardcoded developer IP over mDNS, so devices
dialled a machine that was not the controller and simply never arrived.
Every assertion below is about refusing to invent an address.
"""

import pytest

import em_hostip


def test_configured_address_wins():
    assert em_hostip.resolve("10.10.1.81", "192.168.0.5") == (
        "10.10.1.81", "configured")


def test_empty_configured_falls_back_to_detection():
    assert em_hostip.resolve("", "192.168.0.5") == ("192.168.0.5", "detected")


def test_none_configured_falls_back_to_detection():
    assert em_hostip.resolve(None, "192.168.0.5") == ("192.168.0.5", "detected")


def test_whitespace_is_not_a_configured_value():
    # The add-on writes "" for an untouched field, but a user who typed a
    # space into the box means "unset", not an address.
    assert em_hostip.resolve("   ", "192.168.0.5") == ("192.168.0.5", "detected")


def test_nothing_configured_and_nothing_detected_refuses():
    with pytest.raises(em_hostip.ServerIPError) as e:
        em_hostip.resolve("", None)
    # The message has to name the fix — this fires on a fresh add-on install
    # where the only person who can act is looking at the add-on log.
    assert "SERVER_IP" in str(e.value)


def test_malformed_address_refuses_rather_than_being_repaired():
    # socket.inet_aton accepts this and yields 10.0.0.1 — an address the
    # operator never typed, advertised to the whole fleet.
    with pytest.raises(em_hostip.ServerIPError):
        em_hostip.resolve("10.10.1", None)


def test_malformed_address_refuses_even_when_detection_would_work():
    # Silently substituting a detected address for a typo'd one hides the
    # typo; the operator would see devices working and the field wrong.
    with pytest.raises(em_hostip.ServerIPError):
        em_hostip.resolve("not-an-ip", "192.168.0.5")


def test_hostnames_are_refused():
    # mDNS advertises A records via socket.inet_aton — a hostname cannot be
    # advertised at all, so accepting one here defers the failure to startup.
    with pytest.raises(em_hostip.ServerIPError):
        em_hostip.resolve("homeassistant.local", None)


def test_no_literal_fallback_address_remains_in_the_module():
    # The regression itself: any hardcoded routable address here is a
    # deployment being sent to somebody else's machine.
    import inspect
    import re

    source = inspect.getsource(em_hostip)
    literals = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", source)
    # 192.0.2.1 is RFC 5737 TEST-NET-1, the routing probe, and is never
    # advertised to anything.
    assert set(literals) <= {"192.0.2.1"}, (
        f"unexpected IP literal(s) in em_hostip: {literals}")


def test_detect_returns_an_address_or_none_but_never_raises():
    # Called during startup on hosts we know nothing about; a raise here
    # would be an unhandled crash instead of the actionable ServerIPError.
    result = em_hostip.detect()
    assert result is None or isinstance(result, str)


# ── Container-bridge detection ────────────────────────────────────────────────

def test_docker_bridge_addresses_are_flagged():
    # A container without host networking detects its own bridge address and
    # advertises it. The controller looks healthy and no device can reach it.
    for addr in ("172.17.0.2", "172.18.0.5", "172.23.1.1"):
        assert em_hostip.looks_containerised(addr), addr


def test_ordinary_lan_addresses_are_not_flagged():
    # A false warning on a working install teaches people to ignore warnings,
    # which costs more than the one it would have caught.
    for addr in ("10.10.1.81", "192.168.1.50", "172.16.4.2", "172.31.9.9",
                 "172.1.1.1"):
        assert not em_hostip.looks_containerised(addr), addr


def test_the_warning_never_changes_the_answer():
    # It is advisory. A containerised address is still what gets advertised,
    # because refusing would break anyone deliberately running that way.
    assert em_hostip.resolve("", "172.17.0.2") == ("172.17.0.2", "detected")
    assert em_hostip.resolve("172.17.0.2", None) == ("172.17.0.2", "configured")


# ── mDNS on SERVER_IP's interface alone (#604) ──────────────────────────────

class _Responder:
    def __init__(self, interfaces=None):
        self.interfaces = interfaces


def test_mdns_binds_to_the_one_address():
    r = em_hostip.bind_mdns(_Responder, "10.0.0.5")
    assert r.interfaces == ["10.0.0.5"]


def test_an_address_this_host_does_not_have_falls_back_to_every_interface():
    """zeroconf 0.151 raises OSError(ENODEV) for an address that is not local
    and ValueError for one that is not an address. Either used to stop the
    controller at startup."""
    for exc in (OSError(19, "No such device"), ValueError("not an address")):
        calls = []

        def make(interfaces=None, _exc=exc):
            calls.append(interfaces)
            if interfaces is not None:
                raise _exc
            return "every-interface"

        assert em_hostip.bind_mdns(make, "10.99.99.99") == "every-interface"
        assert calls == [["10.99.99.99"], None]


def test_any_other_failure_is_not_swallowed():
    def make(interfaces=None):
        raise RuntimeError("something else")

    try:
        em_hostip.bind_mdns(make, "10.0.0.5")
    except RuntimeError:
        return
    raise AssertionError("a failure that is not about the address must surface")
