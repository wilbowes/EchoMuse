"""
Intercom sessions and the controller's switchboard (Phase 1).

The registry rules are tested against the pure module; the controller
wiring is tested against em_controller with fake devices, the way
test_control_handlers.py drives the control-plane handlers.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import _oww_stub

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub
import em_intercom  # noqa: E402
import em_state  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    # See test_device_speaking.py: a stub left in sys.modules makes other
    # tests' importorskip("openwakeword") succeed against it.
    yield
    _oww_stub.uninstall()


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    # A test must not see another test's calls.
    monkeypatch.setattr(em_intercom, "INTERCOM", em_intercom.IntercomRegistry())


def _run(coro):
    return asyncio.run(coro)


def _device(device_id="dev1", capable=True):
    caps = ["intercom"] if capable else []
    return em_controller.Device(device_id, "10.0.0.1", caps, None)


# ── the registry ────────────────────────────────────────────────────────────

def test_create_gives_each_call_its_own_id_and_token():
    reg = em_intercom.IntercomRegistry()
    a = reg.create("kitchen", "bedroom")
    b = reg.create("office", "garage")
    assert a.session_id != b.session_id
    assert a.token != b.token
    assert a.state == em_intercom.STATE_ACTIVE
    assert a.peer("kitchen") == "bedroom"
    assert a.peer("bedroom") == "kitchen"
    assert a.peer("office") is None


def test_a_device_cannot_call_itself_or_be_in_two_calls():
    reg = em_intercom.IntercomRegistry()
    with pytest.raises(em_intercom.IntercomError):
        reg.create("kitchen", "kitchen")
    with pytest.raises(em_intercom.IntercomError):
        reg.create("", "bedroom")
    reg.create("kitchen", "bedroom")
    with pytest.raises(em_intercom.IntercomError):
        reg.create("kitchen", "office")
    with pytest.raises(em_intercom.IntercomError):
        reg.create("office", "bedroom")


def test_end_is_idempotent_and_records_the_reason():
    reg = em_intercom.IntercomRegistry()
    s = reg.create("kitchen", "bedroom")
    assert reg.end("nope") is None, "ending an unknown session is a no-op"
    ended = reg.end(s.session_id, "button")
    assert ended.state == em_intercom.STATE_ENDED
    assert ended.ended_reason == "button"
    assert reg.active_for("kitchen") is None
    again = reg.end(s.session_id, "hangup")
    assert again is ended, "a second end returns the same session"
    assert again.ended_reason == "button", "the first reason stands"
    assert reg.active_sessions() == []


def test_active_for_finds_the_call_and_nothing_else():
    reg = em_intercom.IntercomRegistry()
    s = reg.create("kitchen", "bedroom")
    assert reg.active_for("kitchen") is s
    assert reg.active_for("office") is None
    assert reg.active_sessions() == [s]


# ── the state handler ───────────────────────────────────────────────────────

@pytest.fixture
def _no_events(monkeypatch):
    async def push(event):
        pass
    monkeypatch.setattr(em_controller.em_events, "push", push)


def test_intercom_state_is_held_from_the_device_report(_no_events):
    session = em_intercom.INTERCOM.create("dev1", "dev2")
    d = _device()
    _run(em_controller._on_intercom_state(d, {
        "type": "intercom_state", "state": "in_call",
        "session": session.session_id}))
    assert d.intercom_state == "in_call"
    assert d.intercom_session == session.session_id
    assert d.intercom_peer == "dev2"
    _run(em_controller._on_intercom_state(d, {
        "type": "intercom_state", "state": "idle",
        "session": session.session_id}))
    assert d.intercom_state == "idle"
    assert d.intercom_session is None
    assert d.intercom_peer is None


def test_a_state_for_an_unknown_session_is_ignored(_no_events):
    d = _device()
    _run(em_controller._on_intercom_state(d, {
        "type": "intercom_state", "state": "in_call", "session": "nope"}))
    assert d.intercom_state is None, "a stale device must not start a call"
    _run(em_controller._on_intercom_state(d, {
        "type": "intercom_state", "state": "loud"}))
    assert d.intercom_state is None, "an unknown state is ignored"


def test_intercom_state_is_in_the_control_table():
    assert em_controller.CONTROL_HANDLERS["intercom_state"] is \
        em_controller._on_intercom_state


# ── starting and ending calls ───────────────────────────────────────────────

@pytest.fixture
def _two_devices(monkeypatch):
    a = _device("kitchen")
    b = _device("bedroom")
    a.send_control = AsyncMock()
    b.send_control = AsyncMock()
    monkeypatch.setitem(em_state.devices, "kitchen", a)
    monkeypatch.setitem(em_state.devices, "bedroom", b)
    monkeypatch.setattr(em_intercom.db, "get_device",
                        lambda device_id: {"label": device_id + "-label"})
    return a, b


def test_start_offers_both_devices_and_end_fans_out(_two_devices, _no_events):
    a, b = _two_devices

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        assert session.state == em_intercom.STATE_ACTIVE
        for device, peer in ((a, "bedroom"), (b, "kitchen")):
            device.send_control.assert_called_once()
            offer = device.send_control.call_args.args[0]
            assert offer["type"] == "intercom_offer"
            assert offer["session"] == session.session_id
            assert offer["peer_id"] == peer
            assert offer["peer_label"] == peer + "-label"
            assert offer["token"] == session.token
        ended = await em_intercom.end_intercom(session.session_id, "hangup")
        assert ended.state == em_intercom.STATE_ENDED
        for device in (a, b):
            assert device.send_control.call_count == 2
            end = device.send_control.call_args.args[0]
            assert end == {"type": "intercom_end",
                           "session": session.session_id, "reason": "hangup"}
    _run(go())


def test_start_refuses_anything_that_cannot_call(_two_devices, _no_events):
    a, b = _two_devices

    async def go():
        with pytest.raises(em_intercom.IntercomError):
            await em_intercom.start_intercom("kitchen", "attic")
        with pytest.raises(em_intercom.IntercomError):
            await em_intercom.start_intercom("kitchen", "kitchen")
        b.muted = True
        with pytest.raises(em_intercom.IntercomError):
            await em_intercom.start_intercom("kitchen", "bedroom")
        b.muted = False
        old = _device("office", capable=False)
        em_state.devices["office"] = old
        with pytest.raises(em_intercom.IntercomError):
            await em_intercom.start_intercom("kitchen", "office")
        em_state.devices.pop("office", None)
        async with a.voice_lock:
            with pytest.raises(em_intercom.IntercomError):
                await em_intercom.start_intercom("kitchen", "bedroom")
        await em_intercom.start_intercom("kitchen", "bedroom")
        with pytest.raises(em_intercom.IntercomError):
            await em_intercom.start_intercom("kitchen", "bedroom")
    _run(go())
    assert a.send_control.call_count == 1, "a refused start sends no offer"


def test_end_is_idempotent_from_the_api_side(_two_devices, _no_events):
    a, b = _two_devices

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        first = await em_intercom.end_intercom(session.session_id)
        second = await em_intercom.end_intercom(session.session_id)
        assert first is second
        assert await em_intercom.end_intercom("nope") is None
        # Each device heard the end once; the duplicate asked nothing.
        for device in (a, b):
            ends = [c.args[0] for c in device.send_control.call_args_list
                    if c.args[0]["type"] == "intercom_end"]
            assert len(ends) == 1
    _run(go())


def test_button_hangup_ends_the_call(_two_devices, _no_events):
    a, b = _two_devices

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        a.intercom_state = "in_call"
        a.intercom_session = session.session_id
        await em_controller.handle_button_event(a, {
            "clickType": 138, "down": False, "heldMs": 0, "muted": False})
        assert em_intercom.INTERCOM.active_for("kitchen") is None
    _run(go())


def test_a_failed_offer_ends_the_half_started_call(_two_devices, _no_events):
    a, b = _two_devices
    b.send_control = AsyncMock(side_effect=Exception("link down"))

    async def go():
        with pytest.raises(Exception, match="link down"):
            await em_intercom.start_intercom("kitchen", "bedroom")
        # The caller heard the offer and then the cleanup end; the
        # callee heard nothing it could act on.
        types = [c.args[0]["type"] for c in a.send_control.call_args_list]
        assert types == ["intercom_offer", "intercom_end"]
        assert a.send_control.call_args.args[0]["reason"] == "offer_failed"
        assert em_intercom.INTERCOM.active_sessions() == []
    _run(go())


def test_end_reaches_the_device_that_is_still_there(_two_devices, _no_events):
    a, b = _two_devices

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        em_state.devices.pop("bedroom")
        ended = await em_intercom.end_intercom(session.session_id)
        assert ended is session
        types = [c.args[0]["type"] for c in a.send_control.call_args_list]
        assert types == ["intercom_offer", "intercom_end"]
    _run(go())


def test_a_failed_end_still_clears_the_call(_two_devices, _no_events):
    a, b = _two_devices
    calls = {"n": 0}

    async def flaky(msg):
        calls["n"] += 1
        if msg["type"] == "intercom_end":
            raise Exception("link down")
    a.send_control = flaky

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        a.intercom_state = "in_call"
        a.intercom_session = session.session_id
        ended = await em_intercom.end_intercom(session.session_id)
        assert ended is session
        assert a.intercom_state == "idle", "a failed end must not fail the hangup"
        assert a.intercom_session is None
    _run(go())


def test_ending_no_call_is_none(_two_devices):
    a, b = _two_devices

    async def go():
        assert await em_intercom.end_intercom_for_device(a) is None
    _run(go())


def test_label_falls_back_to_the_device_id(_two_devices, _no_events, monkeypatch):
    a, b = _two_devices

    def _db_down(device_id):
        raise Exception("db down")

    monkeypatch.setattr(em_intercom.db, "get_device", _db_down)

    async def go():
        session = await em_intercom.start_intercom("kitchen", "bedroom")
        offer = a.send_control.call_args.args[0]
        assert offer["peer_label"] == "bedroom"
        assert session.state == em_intercom.STATE_ACTIVE
    _run(go())
