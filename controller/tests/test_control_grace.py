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
    task_call = seg.index("em_tasks.spawn(")
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
    assert "em_state.devices.get(device.device_id)" in task, \
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


def _handle_data_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("replaced = device.data_ws")
    return src[start:start + 1600]


def test_a_replacing_data_connection_closes_the_one_it_replaces():
    """
    #751: a device that reconnects registers the new socket before the
    controller notices the old one is dead, so the old one used to be
    abandoned rather than closed — it then lived until WS_PING_TIMEOUT_S
    reaped it, holding a handler task and a half-open TCP connection.
    """
    seg = _handle_data_src()
    # The reference must be taken BEFORE the assignment replaces it, or the
    # socket to close is the new one.
    assert seg.index("replaced = device.data_ws") < seg.index("device.data_ws = ws")
    assert "replaced.close()" in seg, "the replaced socket is never closed"
    # Closing is a no-op when there is nothing to replace, and never closes
    # the socket that just arrived.
    assert "replaced is not None and replaced is not ws" in seg


def test_the_replaced_socket_is_closed_off_the_registration_path():
    """
    The peer is already beyond reach by the time we get here — a device only
    reconnects once its old socket is dead — so the close handshake cannot be
    waited on, and the new socket must be wired up before anything is awaited.
    """
    seg = _handle_data_src()
    assert "em_tasks.spawn(" in seg and "replaced.close()" in seg, \
        "the close must be spawned, not awaited inline on the registration path"
    assert "device.data_ready.set()" in seg, \
        "the replacement must be live before the old socket is reaped"
    assert "em_player.device_gone" not in seg, \
        ("a replacement must NOT abandon the playback session — a stream is "
         "allowed to ride out the bounce on the new socket (em_player re-"
         "resolves the device per chunk)")
