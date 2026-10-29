"""Bluetooth speaker output, the controller's half (em_bt_audio)."""
import asyncio

import pytest

import em_bt_audio as bt


class _Live:
    """A connected Echo whose firmware answers bt_audio like the real one."""

    def __init__(self, caps=("bt_audio",), answer=True, reply=None):
        self.device_id = "G090XX0000001234"
        self.capabilities = list(caps)
        self.sent = []
        self._answer = answer
        self._reply = reply or {}

    async def send_control(self, msg):
        self.sent.append(msg)
        if self._answer:
            out = {"type": "bt_audio_result", "action": msg["action"], "id": msg["id"],
                   "status": {"state": "idle"}, "paired": [], **self._reply}
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(bt.on_result(self, out)))


@pytest.fixture(autouse=True)
def _clean():
    bt._state.clear()
    bt._waiters.clear()
    yield
    bt._state.clear()
    bt._waiters.clear()


@pytest.mark.parametrize("body,problem", [
    ([], "JSON object"),
    ({}, "action"),
    ({"action": "explode"}, "action"),
    ({"action": "pair"}, "Bluetooth address"),
    ({"action": "pair", "addr": "11:22:33:44:55"}, "Bluetooth address"),
    ({"action": "pair", "addr": "11:22:33:44:55:GG"}, "Bluetooth address"),
    ({"action": "forget", "addr": "112233445566"}, "Bluetooth address"),
    ({"action": "discover", "seconds": "soon"}, "number"),
])
def test_validate_refuses(body, problem):
    req, err = bt.validate(body)
    assert req is None and problem in err


def test_validate_cleans():
    req, _ = bt.validate({"action": "pair", "addr": " aa:bb:cc:dd:ee:ff ", "name": "x" * 100})
    assert req == {"action": "pair", "addr": "AA:BB:CC:DD:EE:FF", "name": "x" * 64}
    assert bt.validate({"action": "discover", "seconds": 999})[0]["seconds"] == 30
    assert bt.validate({"action": "discover", "seconds": 0})[0]["seconds"] == 10
    assert bt.validate({"action": "discover", "seconds": 1})[0]["seconds"] == 2
    assert bt.validate({"action": "status", "addr": "ignored"})[0] == {"action": "status"}


def test_capability_decides_never_a_version():
    assert bt.capable(_Live())
    assert not bt.capable(_Live(caps=("ble_connect",)))
    assert not bt.capable(object())


def test_request_waits_for_its_own_reply():
    live = _Live(reply={"results": [{"addr": "AA:BB:CC:DD:EE:FF", "name": "Kitchen"}],
                        "paired": [{"addr": "AA:BB:CC:DD:EE:FF"}]})
    msg = asyncio.run(bt.request(live, {"action": "discover", "seconds": 5}))
    assert msg["results"][0]["name"] == "Kitchen"
    assert live.sent[0]["type"] == "bt_audio" and live.sent[0]["seconds"] == 5
    assert bt.state(live.device_id)["paired"] == [{"addr": "AA:BB:CC:DD:EE:FF"}]
    assert not bt._waiters, "a finished request must not leave its waiter"


def test_request_times_out_and_cleans_up(monkeypatch):
    monkeypatch.setitem(bt._TIMEOUT_S, "status", 0.01)
    monkeypatch.setattr(bt, "_SLACK_S", 0.0)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bt.request(_Live(answer=False), {"action": "status"}))
    assert not bt._waiters


def test_unsolicited_results_update_state_and_buttons_do_not():
    live = _Live()
    asyncio.run(bt.on_result(live, {"action": "status", "status": {"state": "streaming"}}))
    assert bt.state(live.device_id) == {"status": {"state": "streaming"}}
    asyncio.run(bt.on_result(live, {"action": "button", "op": "play"}))
    assert bt.state(live.device_id) == {"status": {"state": "streaming"}}
    # A reply for nobody (a request that already timed out) is harmless.
    asyncio.run(bt.on_result(live, {"action": "pair", "id": "gone", "paired": [{"addr": "x"}]}))
    assert bt.state(live.device_id)["paired"] == [{"addr": "x"}]
    bt.forget_device(live.device_id)
    assert bt.state(live.device_id) is None


def test_routes(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import em_routes_devices as routes
    import em_state

    live = _Live(reply={"addr": "AA:BB:CC:DD:EE:FF", "paired": [{"addr": "AA:BB:CC:DD:EE:FF"}]})
    old = _Live(caps=())
    old.device_id = "OLD"
    monkeypatch.setattr(em_state, "devices", {live.device_id: live, "OLD": old})

    async def run():
        app = web.Application()
        app.router.add_get("/d/{id}", routes._get_bt_audio.__wrapped__)
        app.router.add_post("/d/{id}", routes._post_bt_audio.__wrapped__)
        out = {}
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/d/{live.device_id}")
            out["get"] = (r.status, await r.json())
            r = await c.get("/d/OLD")
            out["old"] = (r.status, await r.json())
            r = await c.get("/d/NOPE")
            out["offline"] = (r.status, await r.json())
            r = await c.post(f"/d/{live.device_id}", json={"action": "pair", "addr": "aa:bb:cc:dd:ee:ff"})
            out["pair"] = (r.status, await r.json())
            r = await c.post(f"/d/{live.device_id}", json={"action": "pair"})
            out["bad"] = (r.status, await r.json())
            r = await c.post("/d/OLD", json={"action": "status"})
            out["notcap"] = (r.status, await r.json())
        return out

    out = asyncio.run(run())
    assert out["get"][0] == 200 and out["get"][1]["capable"] is True
    assert out["old"][1] == {"capable": False, "reason": "firmware", "state": None}
    assert out["offline"][1]["capable"] is False and out["offline"][1]["reason"] == "offline"
    assert out["pair"][0] == 200 and out["pair"][1]["addr"] == "AA:BB:CC:DD:EE:FF"
    assert out["bad"][0] == 400 and out["bad"][1]["code"] == "bad_request"
    assert out["notcap"][0] == 409 and out["notcap"][1]["code"] == "not_capable"
