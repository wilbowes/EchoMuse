"""
em_events: what every open dashboard is pushed. A device log line is
persisted once and pushed once, and the write never waits on the database.
"""

import asyncio
import json

import em_events


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))


def test_push_reaches_every_client():
    a, b = FakeWS(), FakeWS()
    em_events.clients.update({a, b})
    try:
        asyncio.run(em_events.push({"type": "device_update", "device_id": "d"}))
    finally:
        em_events.clients.clear()
    assert a.sent == b.sent == [{"type": "device_update", "device_id": "d"}]


def test_push_with_nobody_listening_does_nothing():
    asyncio.run(em_events.push({"type": "x"}))   # must not raise


def test_push_log_queues_one_write_and_pushes_one_event(monkeypatch):
    queued = []
    monkeypatch.setattr(em_events.em_dbwriter, "submit",
                        lambda fn, *args: queued.append((fn.__name__, args)))
    ws = FakeWS()
    em_events.clients.add(ws)
    try:
        asyncio.run(em_events.push_log("dev1", "warn", "device", "hello"))
    finally:
        em_events.clients.clear()
    assert queued == [("log_device", ("dev1", "warn", "device", "hello"))]
    assert len(ws.sent) == 1
    event = ws.sent[0]
    assert event["type"] == "device_log" and event["device_id"] == "dev1"
    assert {k: event["entry"][k] for k in ("level", "source", "message")} == \
        {"level": "warn", "source": "device", "message": "hello"}
    assert isinstance(event["entry"]["ts"], int)
