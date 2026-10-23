"""
The events socket's snapshot: what a dashboard is given when it connects.

em_routes_events is where the link verdict (#301) reaches a page that has been
open a while — the snapshot on connect and the per-device pushes are the same
payload, so the flag on screen and the sentence behind it cannot drift apart.
Driven here with the socket, the session and the rows injected: the route is
the unit, and the merge it calls has its own readers.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import em_events
import em_routes_events as routes


VERDICT = {
    "level": "warn",
    "text": "12% of pings delayed over 200 ms, worst 4.8 s",
    "note": "2 of the last 6 hours above the 10% bar the rest of this fleet set",
    "ratePct": 12.0,
}


class FakeWS:
    """Duck-type of aiohttp's WebSocketResponse.

    Sends are recorded and the read loop ends at once, so the route's `async
    for` exits and its `finally` — the one that unregisters the socket — runs.
    """

    def __init__(self, **kw):
        self.sent = []

    async def prepare(self, request):
        pass

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _socket(monkeypatch, session):
    """A fake socket and session, and a merge that records what it was given."""
    ws = FakeWS()
    monkeypatch.setattr(routes, "web", SimpleNamespace(
        WebSocketResponse=lambda **kw: ws,
        HTTPUnauthorized=web.HTTPUnauthorized))

    async def resolve(request):
        return session
    monkeypatch.setattr(routes.auth, "ws_resolve_session", resolve)

    calls = []

    def merge(row, boot=None, link_verdict=None):
        calls.append((row["device_id"], link_verdict))
        return {"device_id": row["device_id"], "linkHealth": link_verdict}

    monkeypatch.setattr(routes.em_device_view, "merge_device", merge)
    return ws, calls


def test_the_snapshot_merges_each_device_with_its_own_verdict(monkeypatch):
    ws, calls = _socket(monkeypatch, {"username": "ada"})
    monkeypatch.setattr(routes.db, "get_all_devices",
                        lambda: [{"device_id": "kitchen"}, {"device_id": "study"}])
    monkeypatch.setattr(routes.em_device_view, "link_health",
                        lambda: {"kitchen": VERDICT})

    asyncio.run(routes._ws_events(make_mocked_request("GET", "/api/events")))

    assert calls == [("kitchen", VERDICT), ("study", None)], (
        "a row carries its own device's verdict, and a device with none says "
        "so rather than inheriting its neighbour's")
    assert ws.sent[0]["type"] == "snapshot"
    assert [d["linkHealth"] for d in ws.sent[0]["devices"]] == [VERDICT, None]


def test_the_socket_is_unregistered_when_it_ends(monkeypatch):
    _ws, _calls = _socket(monkeypatch, {"username": "ada"})
    monkeypatch.setattr(routes.db, "get_all_devices", list)
    monkeypatch.setattr(routes.em_device_view, "link_health", dict)

    asyncio.run(routes._ws_events(make_mocked_request("GET", "/api/events")))

    assert em_events.clients == set(), (
        "the client set is how every push finds its sockets; one left in it "
        "is a socket the broadcaster keeps writing to")


def test_a_socket_without_a_session_is_refused(monkeypatch):
    _socket(monkeypatch, None)
    with pytest.raises(web.HTTPUnauthorized):
        asyncio.run(routes._ws_events(make_mocked_request("GET", "/api/events")))
