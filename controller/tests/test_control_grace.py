"""
#315: a control-plane drop used to tear down a device's services
immediately — HA entities deregistered, BLE proxy dropped, media session
killed — and rebuilt all of it when the device returned seconds later.
The data plane has had DATA_RECONNECT_GRACE_S for exactly this reason;
the control plane never got the equivalent.

em_controller is deliberately not importable here (see conftest); these
are shape guards on the shipped source.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _finally_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("log.info(f\"[control] Device disconnected")
    end = src.index("# ─── Data plane handler", start)
    return src[start:end]


def test_teardown_is_deferred_not_immediate():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index('log.info(f"[control] Device disconnected')
    seg = src[start:start + 1200]
    task_call = seg.index("asyncio.create_task(")
    sync_path = seg[:task_call]
    for call in ("esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected",
                 "device_gone", "notify_device_disconnected"):
        assert call not in sync_path, \
            f"{call} must move into the grace task, not run on close"
    assert "_release_device_services" in seg[task_call:], \
        "the close path must hand over to the grace task"


def test_the_grace_task_checks_for_a_replacement():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("async def _release_device_services")
    task = src[start:start + 2500]
    assert "CONTROL_RECONNECT_GRACE_S" in task
    assert "_devices.get(device.device_id)" in task, \
        "the task must check whether a replacement registered"
    for call in ("notify_device_disconnected", "esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected", "device_gone"):
        assert call in task, f"{call} belongs in the deferred release"


def test_the_grace_window_exists_and_is_documented():
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "CONTROL_RECONNECT_GRACE_S" in src
    # The data-plane constant this mirrors:
    assert "DATA_RECONNECT_GRACE_S = 3.0" in src


def test_the_stale_connection_guard_survives():
    """
    The 2026-07-14 guard solves a different ordering problem (close arriving
    AFTER a replacement registered) and must stay untouched.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # the message wraps across two f-string lines — match the fragments
    assert "replacement is active" in src and "services up" in src, \
        "the out-of-order stale guard is still needed alongside the grace"
