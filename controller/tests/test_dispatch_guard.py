"""
#255: one bad message must not cost the device its connection.

The least important message in the protocol used to be able to disconnect
the whole per-device stack: an exception anywhere in the control-plane
dispatch escaped to the handler's `except Exception`, which logged one line
(no type, no traceback) and let the connection close — ESPHome satellite,
BLE proxy and data plane all churned with every reconnect. Observed
2026-08-20: a missing attribute while handling a playback_stats telemetry
report reconnected the device on every barge-in.

em_controller imports openwakeword and aiohttp, so it is deliberately not
importable here (see conftest). These are shape guards on the shipped
source: they pin the structural properties that make the guarantee hold,
the same way test_deploy.py guards its deployment shapes.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _control_loop_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index('                if msg_type == "button":')
    return src[start:start + 60_000]


def test_control_dispatch_survives_a_handler_exception():
    src = _control_loop_src()
    guard = src[:src.index("        finally:")]
    assert guard.count("except asyncio.CancelledError:") == 1, \
        "teardown cancellation must propagate, not read as a message fault"
    assert "log.exception" in guard, \
        "a recurrence needs the traceback, not one bare line"
    assert "msg_type!r" in guard, \
        "the failing message type must be in the log line"
    assert "connection kept" in guard, \
        "the log must say what happened to the link"


def test_data_plane_survives_a_bad_frame_without_flooding_the_log():
    src = (CONTROLLER / "em_controller.py").read_text()
    # newline-anchored: an unanchored indent prefix also matches deeper-
    # indented lines (the control plane's own async-for is 12 spaces).
    start = src.index("\n        async for raw in ws:")
    loop = src[start:src.index("\n    except asyncio.TimeoutError:", start)]
    assert loop.count("try:") >= 1, \
        "per-frame handling must sit inside its own try"
    assert "except asyncio.CancelledError:" in loop, \
        "teardown cancellation must propagate"
    assert "_frame_err_suppressed" in loop and ">= 5.0" in loop, \
        "this loop runs at frame rate - a persistent fault must be \
rate-limited, not logged 23 times a second"


def test_register_failure_stays_fatal():
    """
    The issue asks whether register should stay fatal: yes — it runs before
    the guarded loop, because arriving into an unknown state is different
    from failing to record a statistic. This pins that ordering.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    register = src.index('msg.get("type") != "register"')
    guarded = src.index('                if msg_type == "button":')
    assert register < guarded, \
        "register must be handled before the per-message guard starts"
