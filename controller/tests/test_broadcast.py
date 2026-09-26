"""
em_broadcast.broadcast: dashboards may connect and disconnect mid-send.

The loop it replaced iterated the live client set across awaits and raised
`RuntimeError: Set changed size during iteration` whenever that happened.
"""

import asyncio

import em_broadcast


class Client:
    def __init__(self, on_send=None, fail=False):
        self.got = []
        self.on_send = on_send
        self.fail = fail

    async def send_str(self, payload):
        await asyncio.sleep(0)  # yield, as a real send does under back-pressure
        if self.on_send:
            self.on_send()
        if self.fail:
            raise ConnectionResetError
        self.got.append(payload)


def test_a_client_connecting_mid_broadcast_does_not_raise():
    clients = set()
    late = Client()
    first = Client(on_send=lambda: clients.add(late))
    clients.update({first, Client()})

    asyncio.run(em_broadcast.broadcast(clients, "x"))

    assert first.got == ["x"]
    assert late in clients      # kept for the next event
    assert late.got == []       # and not sent this one; it gets a snapshot on connect


def test_a_client_disconnecting_mid_broadcast_does_not_raise():
    clients = set()
    leaving = Client()
    first = Client(on_send=lambda: clients.discard(leaving))
    clients.update({first, leaving})

    asyncio.run(em_broadcast.broadcast(clients, "x"))

    assert first.got == ["x"]
    assert leaving not in clients


def test_failed_sends_are_removed_and_the_rest_still_receive():
    ok, bad = Client(), Client(fail=True)
    clients = {ok, bad}

    asyncio.run(em_broadcast.broadcast(clients, "x"))

    assert clients == {ok}
    assert ok.got == ["x"]


def test_the_old_loop_did_raise():
    # Pins the failure this module exists to prevent, so the tests above are
    # known to exercise it rather than passing on a set nobody changed.
    clients = set()
    first = Client(on_send=lambda: clients.add(Client()))
    clients.add(first)

    async def old(clients, payload):
        for ws in clients:
            await ws.send_str(payload)

    try:
        asyncio.run(old(clients, "x"))
    except RuntimeError as e:
        assert "changed size" in str(e)
    else:
        raise AssertionError("the unsnapshotted loop should have raised")
