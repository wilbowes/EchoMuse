"""
Tests for em_identify: the dashboard's Identify action (#860).

The firmware takes the whole ring for 30s on an `identify` message, above every
other ring owner, muted or not, so the controller has only two questions: is
the Echo connected, and does its firmware announce `identify_ring`. There is no
fallback for older firmware.

The decision is a pure function, so the table is tested whole. The route is
checked by reading its source: it must ask before it sends, and send only on OK.
"""

import ast
import asyncio
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import _oww_stub

_oww_stub.install()

import _sources  # noqa: E402
import em_controller  # noqa: E402 — must follow the stub
import em_identify as ident  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    yield
    _oww_stub.uninstall()


FREE = dict(connected=True, identify_ring_capable=True)


def test_a_connected_echo_with_the_capability_may_be_identified():
    assert ident.decide(**FREE) == ident.OK


def test_an_offline_echo_cannot_be_identified():
    assert ident.decide(**{**FREE, "connected": False}) == ident.NOT_CONNECTED


def test_firmware_without_identify_ring_is_refused_with_its_own_reason():
    assert ident.decide(**{**FREE, "identify_ring_capable": False}) == ident.NO_IDENTIFY


def test_offline_outranks_a_missing_capability():
    assert ident.decide(connected=False, identify_ring_capable=False) == ident.NOT_CONNECTED


def test_only_one_combination_of_inputs_is_ok():
    oks = [c for c in itertools.product([True, False], repeat=2)
           if ident.decide(**dict(zip(FREE, c))) == ident.OK]
    assert oks == [(True, True)]


def test_every_refusal_has_a_message_and_a_status():
    for verdict in (ident.NOT_CONNECTED, ident.NO_IDENTIFY):
        assert ident.MESSAGES[verdict]
        assert ident.STATUS[verdict] == 409
    assert ident.STATUS[ident.OK] == 200


def test_identify_lasts_thirty_seconds():
    # The device owns the timer; this is what the dashboard shows while it runs.
    assert ident.DURATION_S == 30


def test_nothing_about_the_ring_state_can_refuse_it():
    """Muted, busy and led_anim were the old refusals; the new layer ignores all three."""
    assert set(ident.STATUS) == {ident.OK, ident.NOT_CONNECTED, ident.NO_IDENTIFY}
    assert not hasattr(ident, "MUTED") and not hasattr(ident, "BUSY")
    assert not hasattr(ident, "IDENTIFY_ANIM")


def _handler():
    _path, fn = _sources.find_def("_post_identify")
    return fn


def test_the_route_asks_before_it_sends_and_sends_only_on_ok():
    fn = _handler()
    called = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert called.index("em_identify.decide") < called.index("live.identify")
    src = ast.unparse(fn)
    assert "em_identify.OK" in src and "return em_http.error" in src


def test_the_route_does_not_look_at_mute_busy_or_led_anim():
    src = _sources.code_only(ast.unparse(_handler()))
    for word in ("muted", "voice_lock", "speaking", "speaker_busy",
                 "timer_alarm_ringing", "led_anim", "send_led_anim"):
        assert word not in src, f"identify must not depend on {word}"


def test_the_route_is_admin_only():
    decorators = [ast.unparse(d) for d in _handler().decorator_list]
    assert decorators == ["auth.require_admin"]


def test_the_route_reports_the_thirty_seconds():
    assert "em_identify.DURATION_S" in ast.unparse(_handler())


def test_the_capability_property_reads_the_register_list():
    with_it = em_controller.Device("d1", "10.0.0.1", ["identify_ring"], None)
    without = em_controller.Device("d2", "10.0.0.2", ["led_anim"], None)
    assert with_it.identify_ring_capable is True
    assert without.identify_ring_capable is False


def test_identify_sends_one_bare_identify_message():
    sent = []
    d = em_controller.Device("d1", "10.0.0.1", ["identify_ring"], None)

    async def send_control(msg):
        sent.append(msg)

    d.send_control = send_control
    asyncio.run(d.identify())
    assert sent == [{"type": "identify"}]
