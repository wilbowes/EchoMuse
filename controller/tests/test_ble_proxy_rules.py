"""
The rules around Bluetooth connections that sit in modules the suite cannot
import (em_ble_proxy needs zeroconf and protobuf, em_api needs aiohttp), read
from their source by AST — and the key, tested against a real database.

The one that matters most: CONNECTIONS ARE NEVER OFFERED ON A PLAINTEXT PORT
(Wil, 2026-10-03). A connection can operate the device at the other end, and
the proxy's port has no other authentication.
"""

import ast
import base64
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import em_db as db

CONTROLLER = pathlib.Path(__file__).resolve().parents[1]


def _func(module: str, name: str, cls: str = "") -> ast.AST:
    tree = ast.parse((CONTROLLER / module).read_text())
    scope = tree
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return next(n for n in ast.walk(scope)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls(node: ast.AST) -> list:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "test.db"))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


# ── the key ───────────────────────────────────────────────────────────────────

def test_key_is_32_random_bytes_assigned_once(fresh_db):
    db.register_new_device("G090LF0000000001", "10.0.0.1", "v0")
    db.register_new_device("G090LF0000000002", "10.0.0.2", "v0")
    a = db.ensure_ble_proxy_key("G090LF0000000001")
    b = db.ensure_ble_proxy_key("G090LF0000000002")
    assert len(base64.b64decode(a)) == 32 and len(base64.b64decode(b)) == 32
    assert a != b
    # Home Assistant stores it; a key that changed would lock its entry out.
    assert db.ensure_ble_proxy_key("G090LF0000000001") == a
    assert db.ensure_ble_proxy_key("G090LF00000000XX") is None


def test_connections_default_off_and_live_in_the_bluetooth_section():
    import em_config_sections
    assert db.DEFAULT_DEVICE_CONFIG["bleProxyConnections"] is False
    assert "bleProxyConnections" in em_config_sections.SECTIONS["bluetooth"]["keys"]


# ── never offered in the clear ────────────────────────────────────────────────

def test_a_proxy_has_connections_exactly_when_it_has_a_key():
    init = _func("em_ble_proxy.py", "__init__", "DeviceBleProxyServer")
    assigned = {}
    for n in ast.walk(init):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute):
            assigned[n.targets[0].attr] = ast.unparse(n.value)
    assert assigned["connections"] == "key is not None", assigned.get("connections")


def test_the_handlers_exist_only_on_an_encrypted_connection():
    init = _func("em_ble_proxy.py", "__init__", "BluetoothProxySatellite")
    guarded = [n for n in ast.walk(init) if isinstance(n, ast.If)
               and ast.unparse(n.test) == "owning_server.connections"]
    assert len(guarded) == 1, "one branch decides both encryption and the handlers"
    calls = _calls(guarded[0])
    assert "set_encryption" in calls and "GattProxy" in calls
    assert calls.index("set_encryption") < calls.index("GattProxy")
    # And nowhere else in the module is a GattProxy made.
    tree = ast.parse((CONTROLLER / "em_ble_proxy.py").read_text())
    assert _calls(tree).count("GattProxy") == 1


def test_flags_and_encryption_follow_the_same_fact():
    handle = _func("em_ble_proxy.py", "handle_message", "BluetoothProxySatellite")
    info = next(n for n in ast.walk(handle) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "DeviceInfoResponse")
    kw = {k.arg: ast.unparse(k.value) for k in info.keywords}
    assert kw["api_encryption_supported"] == "self._gatt is not None"
    assert "BT_PROXY_FLAGS_ACTIVE if self._gatt is not None else BT_PROXY_FLAGS" in kw[
        "bluetooth_proxy_feature_flags"]


def test_pairing_is_not_advertised():
    tree = ast.parse((CONTROLLER / "em_ble_proxy.py").read_text())
    active = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "BT_PROXY_FLAGS_ACTIVE")
    names = {n.attr for n in ast.walk(active) if isinstance(n, ast.Attribute)}
    assert {"ACTIVE_CONNECTIONS", "REMOTE_CACHING", "CACHE_CLEARING"} <= names
    assert "PAIRING" not in names


# ── the key goes to one request and nowhere else ──────────────────────────────

def test_key_endpoint_is_admin_only_and_says_nothing_else():
    fn = _func("em_api.py", "_get_ble_proxy_key")
    assert [ast.unparse(d) for d in fn.decorator_list] == ["auth.require_admin"]
    calls = _calls(fn)
    for forbidden in ("info", "warning", "debug", "error", "log_device", "broadcast", "_push_event"):
        assert forbidden not in calls, f"the key handler calls {forbidden}"
    src = (CONTROLLER / "em_api.py").read_text()
    assert 'add_get("/api/devices/{id}/ble_proxy/key", _get_ble_proxy_key)' in src


def test_key_is_not_in_a_support_bundle_or_the_device_list():
    assert "ble_proxy_key" not in (CONTROLLER / "em_support.py").read_text()
    api = ast.parse((CONTROLLER / "em_api.py").read_text())
    users = [n.name for n in ast.walk(api)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and "ensure_ble_proxy_key" in ast.unparse(n)]
    assert users == ["_get_ble_proxy_key"], users


# ── negotiated both ways ──────────────────────────────────────────────────────

def test_controller_and_device_announce_the_same_name():
    ctrl = (CONTROLLER / "em_controller.py").read_text()
    tree = ast.parse(ctrl)
    features = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "CONTROLLER_FEATURES")
    assert "ble_connect" in ast.literal_eval(features.value)
    go = (CONTROLLER.parent / "device/internal/client/control.go").read_text()
    assert 'const FeatureBleConnect = "ble_connect"' in go
    assert '"ble_connect"}' in go.split("func capabilities()")[1].split("return caps")[0]


def test_frames_go_only_to_a_device_that_announced_it():
    send = _func("em_controller.py", "send_gatt", "Device")
    assert "self.ble_connect_capable" in ast.unparse(send)
    data = (CONTROLLER.parent / "device/internal/client/data.go").read_text()
    assert "frameTypeBleGatt = byte(0x08)" in data
    import em_ble_gatt
    assert em_ble_gatt.FRAME_BLE_GATT == 0x08


def test_a_rebuilt_proxy_asks_the_device_for_its_slots():
    # The device reports its slots when the setting reaches it, which is
    # before the controller has rebuilt the proxy that should hear it.
    fn = _func("em_ble_proxy.py", "reconcile")
    src = ast.unparse(fn)
    assert "sync_slots(device_id)" in src
    assert src.index("await proxy.start(_host)") < src.index("sync_slots(device_id)")
