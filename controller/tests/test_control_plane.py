"""
The device control plane, driven the way a device drives it.

`handle_control` is the only way a device registers, and every rule that
protects it lives in this one coroutine: which device is admitted and which is
held for approval, what a refused or pairing device is told, one bad message
not costing the connection (#255), and what is torn down when the socket goes.

None of it was executed by the suite. The 1,140-line version was split into
`CONTROL_HANDLERS` in #288 and those handlers are tested one by one, which
leaves the part that decides *whether* a handler is reached — approval, auth,
dispatch, cleanup — with no test at all. That is where the bugs were: a
`FOREIGN KEY` error in this `finally` once skipped the device teardown with
nothing logged.

The websocket is a small object of the shape the code already uses
(`remote_address`, `recv`, `send`, `close`, `async for`) and the database is a
real one in `tmp_path`, so the approval path is a real row rather than a
stub's idea of one.
"""

import asyncio
import json

import pytest

import em_controller
import em_credentials
import em_db as db
import em_reconcile
import em_state

DEVICE = "G090LF0000000001"
IP = "10.0.0.9"


class FakeWS:
    """A device's /control socket, with the messages it will send queued."""

    def __init__(self, messages, headers=None):
        self.remote_address = (IP, 50000)
        self.request = type("Request", (), {"headers": headers or {}})()
        self.sent: list[dict] = []
        self.closed = False
        self._queued = list(messages)

    async def recv(self):
        return self._queued.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._queued:
            raise StopAsyncIteration
        return self._queued.pop(0)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


def register(**fields) -> str:
    message = {"type": "register", "device_id": DEVICE, "ip": IP,
               "version": "v2.9.6", "capabilities": ["mic", "speaker", "leds"]}
    message.update(fields)
    return json.dumps(message)


def connect(messages, headers=None):
    ws = FakeWS(messages, headers)
    asyncio.run(em_controller.handle_control(ws))
    return ws


def kinds(ws) -> list[str]:
    return [m["type"] for m in ws.sent]


def an_approved_device(**overrides):
    db.register_new_device(DEVICE, IP, "v2.9.6")
    db.approve_device(DEVICE, "Test Device")
    for key, value in overrides.items():
        setattr(db.get_device(DEVICE), key, value)


@pytest.fixture(autouse=True)
def control(tmp_path, monkeypatch):
    """A database of its own, and nothing that needs the outside world."""
    db.init(str(tmp_path / "control.db"))
    em_state.devices.clear()

    async def noop(*_args, **_kwargs):
        return None

    def idle_task(*_args, **_kwargs):
        return asyncio.ensure_future(asyncio.sleep(3600))

    # Each is a separate unit with its own tests; here they would shell into a
    # device that is not attached, import scipy, or run a wake loop forever.
    monkeypatch.setattr(em_controller, "_supervise_wake_listener", idle_task)
    monkeypatch.setattr(em_controller, "_release_device_services", noop)
    monkeypatch.setattr(em_reconcile, "on_connect", noop)
    monkeypatch.setattr(em_controller.em_eq, "preload", lambda: None)
    monkeypatch.setattr(em_credentials, "issue_credentials", noop)
    yield
    em_state.devices.clear()


# ── Admission ──────────────────────────────────────────────────────────────

def test_the_first_message_must_be_register():
    """
    Anything else is a client that does not know the protocol, and there is no
    device to attach it to. The connection is closed without a reply.
    """
    ws = connect([json.dumps({"type": "hello"})])
    assert ws.closed
    assert ws.sent == []


def test_an_unknown_device_is_held_for_approval_and_told_so():
    """
    In strict mode the device must be told "pending" or its ring shows "no
    controller" and the owner has no idea a human has to click approve.
    """
    ws = connect([register()])
    assert ws.sent == [{"type": "pending"}]
    assert ws.closed
    assert db.get_device(DEVICE)["approved"] == 0
    assert DEVICE not in em_state.devices


def test_auto_approval_admits_a_new_device_and_pushes_its_config():
    """
    The ack carries the features the controller implements and the device's
    wall clock; the config push carries every setting the device needs to be
    useful. A device that registers and gets neither looks connected and does
    nothing.
    """
    db.set_config("device_approval", "auto")
    ws = connect([register()])

    assert kinds(ws)[:2] == ["ack", "config"]
    ack = ws.sent[0]
    assert ack["device_id"] == DEVICE
    assert "time_ms" in ack
    assert {"owwThreshold", "owwModel", "startupVolume"} <= ws.sent[1].keys()
    assert db.get_device(DEVICE)["approved"] == 1


def test_an_approved_device_connects_and_leaves_the_registry_on_disconnect():
    """
    The `finally` is the path a bug lived in: a raise there skipped the pop
    and the services stayed assigned to a device that had gone.
    """
    an_approved_device()
    ws = connect([register()])
    assert not ws.closed
    assert "ack" in kinds(ws)


def test_the_device_is_out_of_the_registry_once_the_socket_closes():
    an_approved_device()
    connect([register()])
    assert DEVICE not in em_state.devices


# ── Auth ───────────────────────────────────────────────────────────────────

def test_a_wrong_link_token_is_refused():
    """
    A device holding a token that does not match the stored one is refused on
    every plane; admitting it on this one would be showing its logs and
    configuration to whatever the id leaked to.
    """
    an_approved_device()
    db.set_device_token(DEVICE, "the-real-token")
    ws = connect([register()],
                 headers={"X-EM-Token": "a-stolen-token"})
    assert ws.sent == [{"type": "refused"}]
    assert ws.closed


def test_a_pairing_device_is_told_pending_rather_than_refused(monkeypatch):
    """
    "refused" reads as "no controller"; "pending" is what makes the device
    show the hold-the-button instruction. Only the pairing flag tells them
    apart, so it must survive a rejection.
    """
    monkeypatch.setattr(em_controller, "REQUIRE_DEVICE_TLS", True)
    ws = connect([register(pairing=True)])
    assert ws.sent == [{"type": "pending", "pairing": True}]
    assert ws.closed


# ── Dispatch ───────────────────────────────────────────────────────────────

def test_one_bad_message_does_not_cost_the_connection(monkeypatch):
    """
    #255: the least important message in the protocol took the ESPHome
    satellite, the BLE proxy and the data plane down with it, and the device
    reconnected on every barge-in. A handler that raises drops its message and
    nothing else.
    """
    an_approved_device()
    handled = []

    async def raising(_device, _msg):
        raise ValueError("a handler bug")

    async def after(_device, msg):
        handled.append(msg["type"])

    monkeypatch.setitem(em_controller.CONTROL_HANDLERS, "stats", raising)
    monkeypatch.setitem(em_controller.CONTROL_HANDLERS, "pong", after)
    ws = connect([register(),
                  json.dumps({"type": "stats"}),
                  json.dumps({"type": "pong", "id": 1})])

    assert handled == ["pong"]
    assert not ws.closed


def test_a_message_that_is_not_json_is_skipped(monkeypatch):
    """A truncated frame must not be read as a handler fault either."""
    an_approved_device()
    handled = []

    async def after(_device, msg):
        handled.append(msg["type"])

    monkeypatch.setitem(em_controller.CONTROL_HANDLERS, "pong", after)
    connect([register(), "{not json", json.dumps({"type": "pong", "id": 1})])
    assert handled == ["pong"]


def test_an_unknown_control_message_is_ignored(monkeypatch):
    """Newer firmware sends messages this controller has never heard of."""
    an_approved_device()
    handled = []

    async def after(_device, msg):
        handled.append(msg["type"])

    monkeypatch.setitem(em_controller.CONTROL_HANDLERS, "pong", after)
    ws = connect([register(), json.dumps({"type": "from_the_future"}),
                  json.dumps({"type": "pong", "id": 1})])
    assert handled == ["pong"]
    assert not ws.closed
