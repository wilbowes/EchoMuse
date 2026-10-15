"""
The control-plane message handlers (#288), driven directly.

Each message a registered device sends on /control is handled by one
`_on_<type>(device, msg)` coroutine, looked up in CONTROL_HANDLERS. Until they
were split out of handle_control these were branches of a 1,140-line function
and nothing could call one on its own.

The table is held to docs/device-controller-interface.md, the spec a port is
built against, in both directions: a message the spec lists that the
controller drops is a feature that silently does nothing, and a handler the
spec does not list is a message no other implementation knows to send.
"""

import asyncio
import inspect
import re
from pathlib import Path

import pytest

import _oww_stub

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub

SPEC = Path(__file__).resolve().parents[2] / "docs" / "device-controller-interface.md"


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    # See test_device_speaking.py: a stub left in sys.modules makes other
    # tests' importorskip("openwakeword") succeed against it.
    yield
    _oww_stub.uninstall()


def _device(device_id="dev1"):
    return em_controller.Device(device_id, "10.0.0.1", [], None)


def _run(coro):
    return asyncio.run(coro)


# ── The table against the spec ──────────────────────────────────────────────

def _spec_section() -> str:
    text = SPEC.read_text()
    return text[text.index("**Device → Controller**"):
                text.index("**Controller → Device**")]


def _spec_device_to_controller() -> set[str]:
    # The header row is `type`, the column name.
    return set(re.findall(r"^\| `(\w+)` \|", _spec_section(), re.M)) - {"type"}


def test_the_spec_table_was_found():
    # Guards the guard: a reformatted spec must fail here, not pass on nothing.
    assert {"register", "button", "stats", "pong"} <= _spec_device_to_controller()


def test_every_message_in_the_spec_has_a_handler_and_nothing_else_does():
    # `register` is the handshake, handled before the loop, not by the table.
    spec = _spec_device_to_controller() - {"register"}
    table = set(em_controller.CONTROL_HANDLERS)
    assert spec - table == set(), f"in the spec, dropped by the controller: {spec - table}"
    assert table - spec == set(), f"handled, but not in the spec: {table - spec}"


def test_every_handler_takes_the_device_and_the_message():
    for name, fn in em_controller.CONTROL_HANDLERS.items():
        assert inspect.iscoroutinefunction(fn), name
        assert len(inspect.signature(fn).parameters) == 2, name


def test_the_spec_names_the_field_the_firmware_sends_for_ambient_light():
    # The spec said `value`; the firmware sends `lux` (device/internal/client/
    # control.go, SendAmbientLight) and the handler reads `lux`.
    row = next(l for l in _spec_section().splitlines()
               if l.startswith("| `ambient_light` |"))
    assert "`lux`" in row


# ── ambient_light ───────────────────────────────────────────────────────────

@pytest.fixture
def lux_pushes(monkeypatch):
    pushed = []
    monkeypatch.setattr(em_controller.esphome, "update_ambient_lux",
                        lambda device_id, lux: pushed.append((device_id, lux)))
    return pushed


def test_a_light_change_before_the_first_stats_report_still_reaches_ha(lux_pushes):
    """It raised on `device.stats` being None, and the #255 guard dropped it:
    a light switched on in the first ~30s after connect never reached HA."""
    d = _device()
    assert d.stats is None
    _run(em_controller._on_ambient_light(d, {"type": "ambient_light", "lux": 42}))
    assert lux_pushes == [("dev1", 42)]
    assert d.stats is None, "a lux reading is not a stats report"


def test_a_light_change_updates_the_held_stats(lux_pushes):
    d = _device()
    d.stats = {"cpu": 10, "ambientLux": 3}
    _run(em_controller._on_ambient_light(d, {"type": "ambient_light", "lux": 400}))
    assert d.stats == {"cpu": 10, "ambientLux": 400}
    assert lux_pushes == [("dev1", 400)]


@pytest.mark.parametrize("lux", [None, "42", 4.2])
def test_a_reading_that_is_not_an_integer_is_ignored(lux_pushes, lux):
    d = _device()
    d.stats = {}
    msg = {"type": "ambient_light"} if lux is None else {"type": "ambient_light", "lux": lux}
    _run(em_controller._on_ambient_light(d, msg))
    assert lux_pushes == [] and d.stats == {}


# ── answers to a request the controller is waiting on ───────────────────────

@pytest.mark.parametrize("handler, attr", [
    ("_on_sendspin_token", "sendspin_token_waiter"),
    ("_on_wifi_scan_result", "wifi_scan_future"),
])
def test_an_answer_resolves_the_waiting_request(handler, attr):
    async def go():
        d = _device()
        fut = asyncio.get_running_loop().create_future()
        setattr(d, attr, fut)
        msg = {"type": "x", "payload": 1}
        await getattr(em_controller, handler)(d, msg)
        assert fut.result() is msg
    _run(go())


@pytest.mark.parametrize("handler, attr", [
    ("_on_sendspin_token", "sendspin_token_waiter"),
    ("_on_wifi_scan_result", "wifi_scan_future"),
])
def test_an_answer_nobody_is_waiting_for_is_dropped(handler, attr):
    async def go():
        d = _device()
        setattr(d, attr, None)
        await getattr(em_controller, handler)(d, {"type": "x"})   # must not raise
        done = asyncio.get_running_loop().create_future()
        done.set_result("first")
        setattr(d, attr, done)
        await getattr(em_controller, handler)(d, {"type": "x"})   # nor here
        assert done.result() == "first"
    _run(go())


# ── log, sendspin_status, pair_request ──────────────────────────────────────

def test_a_device_log_line_is_relayed_once_with_its_level(monkeypatch):
    sent = []

    async def push(device_id, level, source, message):
        sent.append((device_id, level, source, message))
    monkeypatch.setattr(em_controller.api, "_push_log_event", push)
    _run(em_controller._on_log(_device(), {"type": "log", "level": "warn", "message": "m"}))
    _run(em_controller._on_log(_device(), {"type": "log"}))
    assert sent == [("dev1", "warn", "device", "m"), ("dev1", "info", "device", "")]


def test_sendspin_status_is_held_and_pushed(monkeypatch):
    events = []

    async def push(event):
        events.append(event)
    monkeypatch.setattr(em_controller.api, "_push_event", push)
    d = _device()
    _run(em_controller._on_sendspin_status(d, {"type": "sendspin_status",
                                               "status": {"state": "playing"}}))
    assert d.sendspin == {"state": "playing"}
    assert events == [{"type": "device_update", "device_id": "dev1",
                       "state": {"sendspin": {"state": "playing"}}}]
    _run(em_controller._on_sendspin_status(d, {"type": "sendspin_status", "status": None}))
    assert d.sendspin is None, "null means the player stopped"


def test_a_pair_request_is_shown_to_the_admin(monkeypatch):
    seen = []

    async def notify(device_id, via):
        seen.append((device_id, via))
    monkeypatch.setattr(em_controller.api, "notify_pair_request", notify)
    _run(em_controller._on_pair_request(_device(), {"type": "pair_request"}))
    assert seen == [("dev1", "link")]


# ── pong ────────────────────────────────────────────────────────────────────

def test_a_solicited_pong_records_one_rtt_sample():
    async def go():
        d = _device()
        d.ping_sent[7] = asyncio.get_running_loop().time() - 0.05
        d.ping_busy[7] = False
        await em_controller._on_pong(d, {"type": "pong", "id": 7})
        assert 7 not in d.ping_sent and 7 not in d.ping_busy
        return d.drain_rtt()
    window = _run(go())
    assert window["rttSamples"] == 1
    assert 50 <= window["rttMinMs"] < 1000


def test_an_unsolicited_pong_is_not_a_measurement():
    """Pairing a keepalive pong with whatever ping is outstanding would invent
    an RTT."""
    d = _device()
    d.ping_sent[7] = 0.0
    _run(em_controller._on_pong(d, {"type": "pong"}))
    _run(em_controller._on_pong(d, {"type": "pong", "id": 8}))
    assert d.ping_sent == {7: 0.0}
