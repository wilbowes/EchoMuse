"""
Tests for em_identify: the dashboard's Identify action (#860).

The decision is a pure function, so the table is tested whole. The route is
checked by reading its source: it must ask before it sends, and send only on OK.
"""

import ast
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import _sources
import em_identify as ident

FREE = dict(connected=True, led_anim_capable=True, muted=False,
            turn_running=False, speaking=False, alarm_ringing=False)


def test_a_free_connected_echo_may_be_identified():
    assert ident.decide(**FREE) == ident.OK


def test_an_offline_echo_cannot_be_identified():
    assert ident.decide(**{**FREE, "connected": False}) == ident.NOT_CONNECTED


def test_firmware_without_led_anim_is_refused_with_its_own_reason():
    assert ident.decide(**{**FREE, "led_anim_capable": False}) == ident.NO_ANIM


def test_a_muted_echo_is_refused_because_it_would_paint_nothing():
    assert ident.decide(**{**FREE, "muted": True}) == ident.MUTED


@pytest.mark.parametrize("busy", ["turn_running", "speaking", "alarm_ringing"])
def test_an_echo_whose_ring_is_in_use_is_busy(busy):
    assert ident.decide(**{**FREE, busy: True}) == ident.BUSY


def test_offline_outranks_everything_and_muted_outranks_busy():
    assert ident.decide(connected=False, led_anim_capable=False, muted=True,
                        turn_running=True, speaking=True, alarm_ringing=True) == ident.NOT_CONNECTED
    assert ident.decide(**{**FREE, "muted": True, "speaking": True}) == ident.MUTED


def test_only_one_combination_of_inputs_is_ok():
    oks = [c for c in itertools.product([True, False], repeat=6)
           if ident.decide(**dict(zip(FREE, c))) == ident.OK]
    assert oks == [(True, True, False, False, False, False)]


def test_every_refusal_has_a_message_and_a_status():
    for verdict in (ident.NOT_CONNECTED, ident.NO_ANIM, ident.MUTED, ident.BUSY):
        assert ident.MESSAGES[verdict]
        assert ident.STATUS[verdict] == 409
    assert ident.STATUS[ident.OK] == 200


def test_the_animation_is_a_self_clearing_violet_pulse():
    a = ident.IDENTIFY_ANIM
    assert a["pattern"] == "pulse"
    assert a["ttlSec"] == ident.DURATION_S and a["ttlSec"] > 0   # the dead-man
    r, g, b = a["colors"][0]
    assert b > g and r > g                                       # violet


def test_the_colour_is_not_one_the_ring_already_means_something_by():
    def near(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) < 160
    taken = [(180, 0, 0),      # mute red
             (255, 40, 0),     # link orange
             (0, 200, 200),    # volume cyan
             (255, 255, 255),  # pending approval
             (255, 170, 0)]    # ringing timer amber
    assert not any(near(ident.IDENTIFY_COLOUR, t) for t in taken)


def _handler():
    _path, fn = _sources.find_def("_post_identify")
    return fn


def test_the_route_asks_before_it_sends_and_sends_only_on_ok():
    fn = _handler()
    names = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    called = [ast.unparse(c.func) for c in names]
    assert called.index("em_identify.decide") < called.index("live.send_led_anim")
    src = ast.unparse(fn)
    assert "em_identify.OK" in src and "return em_http.error" in src


def test_the_route_is_admin_only():
    fn = _handler()
    decorators = [ast.unparse(d) for d in fn.decorator_list]
    assert decorators == ["auth.require_admin"]
