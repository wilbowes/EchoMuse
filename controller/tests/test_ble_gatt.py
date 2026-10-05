"""
em_ble_gatt: the Echo's connection bridge as the controller sees it, and
Home Assistant's Bluetooth messages mapped onto it.

The Echo here is a stand-in that answers the 0x08 messages as
device/internal/bluetooth/bridge.go does (that side is tested in Go against a
simulated HCI controller), and the ESPHome messages are plain objects with
the real class and field names. tools/gatt_client_check.py runs the same
module against the real protobufs and Home Assistant's own client.
"""

import asyncio
import base64
import json
import types

import pytest

import em_ble_gatt as G

A = "c0:00:00:00:00:0a"
B = "c0:00:00:00:00:0b"
A_INT = 0xC0000000000A
UUID_NAME = "00002a00-0000-1000-8000-00805f9b34fb"
UUID_CUSTOM = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


def _pb():
    names = """SubscribeBluetoothConnectionsFreeRequest BluetoothConnectionsFreeResponse
        BluetoothDeviceRequest BluetoothDeviceConnectionResponse
        BluetoothDevicePairingResponse BluetoothDeviceUnpairingResponse
        BluetoothDeviceClearCacheResponse BluetoothGATTGetServicesRequest
        BluetoothGATTGetServicesResponse BluetoothGATTGetServicesDoneResponse
        BluetoothGATTService BluetoothGATTCharacteristic BluetoothGATTDescriptor
        BluetoothGATTReadRequest BluetoothGATTReadDescriptorRequest BluetoothGATTReadResponse
        BluetoothGATTWriteRequest BluetoothGATTWriteDescriptorRequest BluetoothGATTWriteResponse
        BluetoothGATTNotifyRequest BluetoothGATTNotifyResponse BluetoothGATTNotifyDataResponse
        BluetoothGATTErrorResponse""".split()
    ns = types.SimpleNamespace()
    for name in names:
        def init(self, **kw):
            self.__dict__.update(kw)

        def rep(self):
            return f"{type(self).__name__}({self.__dict__})"
        setattr(ns, name, type(name, (), {"__init__": init, "__repr__": rep}))
    for i, name in enumerate(["CONNECT", "DISCONNECT", "PAIR", "UNPAIR",
                              "CONNECT_V3_WITH_CACHE", "CONNECT_V3_WITHOUT_CACHE", "CLEAR_CACHE"]):
        setattr(ns, "BLUETOOTH_DEVICE_REQUEST_TYPE_" + name, i)
    return ns


PB = _pb()


class Echo:
    """Answers bridge requests as the firmware does."""

    def __init__(self):
        self.link = None
        self.connected = {}
        self.values = {3: b"Chonky Monkey", 9: b""}
        self.log = []
        self.online = True
        self.silent = False
        self.refuse_connect = None
        self.events_first = False

    async def send(self, raw: bytes) -> bool:
        if not self.online:
            return False
        req = json.loads(raw)
        self.log.append(req)
        if not self.silent:
            asyncio.get_running_loop().call_soon(self._answer, req)
        return True

    def _emit(self, **msg):
        self.link.feed(json.dumps(msg).encode())

    def _slots(self):
        self._emit(t="slots", free=3 - len(self.connected), limit=3, addrs=sorted(self.connected))

    def _answer(self, req):
        t, n, addr = req["t"], req["req"], req.get("addr")
        if t == "connect":
            if self.refuse_connect:
                self._emit(t="result", req=n, ok=False, error=self.refuse_connect)
                return
            if addr in self.connected:
                self._emit(t="result", req=n, ok=False, error="already_connected")
                return
            self.connected[addr] = True
            self._emit(t="result", req=n, ok=True, mtu=185)
            self._slots()
        elif t == "disconnect":
            was = self.connected.pop(addr, None)
            def result(): self._emit(t="result", req=n, ok=True)
            def event():
                if was:
                    self._emit(t="disconnected", addr=addr, reason=0x16)
                    self._slots()
            for step in ((event, result) if self.events_first else (result, event)):
                step()
        elif t == "slots":
            self._emit(t="slots", req=n, free=3 - len(self.connected), limit=3,
                       addrs=sorted(self.connected))
        elif addr not in self.connected:
            self._emit(t="result", req=n, ok=False, error="not_connected")
        elif t == "services":
            self._emit(t="result", req=n, ok=True, services=[
                {"uuid": "00001800-0000-1000-8000-00805f9b34fb", "start": 1, "end": 5, "chars": [
                    {"uuid": UUID_NAME, "handle": 2, "value_handle": 3, "props": 2, "descs": []}]},
                {"uuid": UUID_CUSTOM, "start": 6, "end": 65535, "chars": [
                    {"uuid": UUID_CUSTOM, "handle": 8, "value_handle": 9, "props": 0x1A, "descs": [
                        {"uuid": "00002902-0000-1000-8000-00805f9b34fb", "handle": 10}]}]}])
        elif t == "read":
            if req["handle"] not in self.values:
                self._emit(t="result", req=n, ok=False, error="att", att=1)
            else:
                self._emit(t="result", req=n, ok=True,
                           value=base64.b64encode(self.values[req["handle"]]).decode())
        elif t == "write":
            if req["handle"] == 99:
                self._emit(t="result", req=n, ok=False, error="att", att=3)
                return
            self.values[req["handle"]] = base64.b64decode(req.get("value") or "")
            self._emit(t="result", req=n, ok=True)


def setup():
    echo = Echo()
    link = G.GattLink(echo.send)
    echo.link = link
    sent, tasks = [], []
    proxy = G.GattProxy(link, PB, sent.extend,
                        lambda coro: tasks.append(asyncio.ensure_future(coro)))
    return echo, link, proxy, sent, tasks


async def settle(tasks):
    while tasks:
        batch, tasks[:] = list(tasks), []
        await asyncio.gather(*batch)
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


def kinds(sent):
    return [type(m).__name__ for m in sent]


def connect_msg(address=A_INT, kind=5, addr_type=1):
    return PB.BluetoothDeviceRequest(address=address, request_type=kind,
                                     has_address_type=True, address_type=addr_type)


# ── conversions ───────────────────────────────────────────────────────────────

def test_address_conversions_round_trip():
    assert G.addr_to_int("c0:00:00:00:00:0a") == A_INT
    assert G.int_to_addr(A_INT) == A
    assert G.int_to_addr(0) == "00:00:00:00:00:00"
    assert G.int_to_addr((1 << 48) - 1) == "ff:ff:ff:ff:ff:ff"
    for bad in ("", "c0:00", "c0000000000a", "c0:00:00:00:00:0a:0b", "c:0:0:0:0:a"):
        with pytest.raises(ValueError):
            G.addr_to_int(bad)
    for bad in (-1, 1 << 48):
        with pytest.raises(ValueError):
            G.int_to_addr(bad)


def test_uuid_is_split_high_then_low():
    assert G.uuid_to_pair(UUID_NAME) == [0x00002A0000001000, 0x800000805F9B34FB]
    assert G.uuid_to_pair(UUID_CUSTOM) == [0x6E400001B5A3F393, 0xE0A9E50E24DCCA9E]
    with pytest.raises(ValueError):
        G.uuid_to_pair("2a00")


def test_errors_map_to_what_home_assistant_decodes():
    assert G.GattError("att", 5).esp_error == 5          # insufficient authentication
    assert G.GattError("no_slots").esp_error == 0x80
    assert G.GattError("timeout").esp_error == 0x08
    assert G.GattError("not_connected").esp_error == -1
    assert G.GattError("something new").esp_error == 0x85


# ── the link ──────────────────────────────────────────────────────────────────

def test_link_matches_results_to_requests_and_encodes_values():
    async def go():
        echo, link, *_ = setup()
        r = await link.request("connect", addr=A, addr_type=1)
        assert r["mtu"] == 185 and link.mtu[A] == 185
        await asyncio.sleep(0)               # the slots event is queued behind the result
        assert (link.free, link.limit, link.addrs) == (2, 3, {A})
        await link.request("write", addr=A, handle=9, value=b"\x00\xff", response=True)
        assert echo.log[-1]["value"] == "AP8=" and echo.values[9] == b"\x00\xff"
        reqs = [m["req"] for m in echo.log]
        assert reqs == sorted(set(reqs))
        with pytest.raises(G.GattError) as e:
            await link.request("read", addr=A, handle=77)
        assert (e.value.code, e.value.att) == ("att", 1)
    run(go())


def test_a_slots_query_is_answered_by_the_slots_event():
    async def go():
        echo, link, *_ = setup()
        echo.connected[A] = True          # a link the Echo already held
        await link.request("slots", timeout=1.0)
        await asyncio.sleep(0)
        assert (link.free, link.limit, link.addrs) == (2, 3, {A})
    run(go())


def test_link_times_out_and_refuses_when_the_echo_is_gone():
    async def go():
        echo, link, *_ = setup()
        echo.silent = True
        with pytest.raises(G.GattError) as e:
            await link.request("read", timeout=0.02, addr=A, handle=3)
        assert e.value.code == "timeout" and link._pending == {}
        echo.online = False
        with pytest.raises(G.GattError) as e:
            await link.request("read", addr=A, handle=3)
        assert e.value.code == "not_running" and link._pending == {}
    run(go())


def test_link_ignores_garbage_and_late_results():
    async def go():
        echo, link, *_ = setup()
        for junk in (b"", b"{", b"[]", b'{"t":"result"}', b'{"t":"result","req":999,"ok":true}',
                     b'{"t":"notify"}', b'{"t":"disconnected"}', b'{"t":"slots","free":"x"}',
                     b'{"no":"type"}', b'{"t":"mystery"}'):
            link.feed(junk)
        await asyncio.sleep(0)
        assert (await link.request("connect", addr=A, addr_type=1))["ok"]
    run(go())


def test_reset_fails_pending_and_reports_every_link_down():
    async def go():
        echo, link, *_ = setup()
        await link.request("connect", addr=A, addr_type=1)
        await link.request("connect", addr=B, addr_type=0)
        down = []
        link.on_disconnected = lambda addr, reason: down.append((addr, reason))
        echo.silent = True
        pending = asyncio.ensure_future(link.request("read", addr=A, handle=3))
        await asyncio.sleep(0)
        link.reset()
        with pytest.raises(G.GattError) as e:
            await pending
        assert e.value.code == "not_running"
        assert down == [(A, 0x16), (B, 0x16)]
        assert (link.free, link.addrs) == (0, set())
    run(go())


# ── Home Assistant's messages ─────────────────────────────────────────────────

def test_a_whole_conversation():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        assert proxy.handle(PB.SubscribeBluetoothConnectionsFreeRequest())
        assert (sent[0].free, sent[0].limit, sent[0].allocated) == (0, 0, [])
        await settle(tasks)                  # it asks the Echo, which answers 3 of 3
        await asyncio.sleep(0)
        del sent[:], echo.log[:]

        assert proxy.handle(connect_msg())
        await settle(tasks)
        conn = [m for m in sent if isinstance(m, PB.BluetoothDeviceConnectionResponse)]
        assert [(m.address, m.connected, m.mtu, m.error) for m in conn] == [(A_INT, True, 185, 0)]
        slots = [m for m in sent if isinstance(m, PB.BluetoothConnectionsFreeResponse)]
        assert (slots[-1].free, slots[-1].limit, slots[-1].allocated) == (2, 3, [A_INT])
        assert echo.log[0] == {"t": "connect", "req": 2, "addr": A, "addr_type": 1}
        del sent[:]

        proxy.handle(PB.BluetoothGATTGetServicesRequest(address=A_INT))
        await settle(tasks)
        assert kinds(sent) == ["BluetoothGATTGetServicesResponse"] * 2 + ["BluetoothGATTGetServicesDoneResponse"]
        gap, custom = sent[0].services[0], sent[1].services[0]
        assert gap.uuid == G.uuid_to_pair("00001800-0000-1000-8000-00805f9b34fb") and gap.handle == 1
        # The characteristic's handle is its VALUE handle: 3, not the declaration at 2.
        assert (gap.characteristics[0].handle, gap.characteristics[0].properties) == (3, 2)
        ch = custom.characteristics[0]
        assert (ch.handle, ch.properties, ch.uuid) == (9, 0x1A, G.uuid_to_pair(UUID_CUSTOM))
        assert [(d.handle, d.uuid) for d in ch.descriptors] == [
            (10, G.uuid_to_pair("00002902-0000-1000-8000-00805f9b34fb"))]
        del sent[:]

        proxy.handle(PB.BluetoothGATTReadRequest(address=A_INT, handle=3))
        proxy.handle(PB.BluetoothGATTWriteRequest(address=A_INT, handle=9, response=True, data=b"Good golly"))
        proxy.handle(PB.BluetoothGATTReadDescriptorRequest(address=A_INT, handle=9))
        proxy.handle(PB.BluetoothGATTWriteDescriptorRequest(address=A_INT, handle=10, data=b"\x01\x00"))
        await settle(tasks)
        assert kinds(sent) == ["BluetoothGATTReadResponse", "BluetoothGATTWriteResponse",
                               "BluetoothGATTReadResponse", "BluetoothGATTWriteResponse"]
        assert sent[0].data == b"Chonky Monkey" and sent[2].data == b"Good golly"
        assert echo.log[-1]["response"] is True and echo.values[10] == b"\x01\x00"
        del sent[:]

        # Notifications are forwarded only for handles Home Assistant asked for.
        link.feed(json.dumps({"t": "notify", "addr": A, "handle": 9, "value": "aGk=", "ind": False}).encode())
        await asyncio.sleep(0)
        assert sent == []
        proxy.handle(PB.BluetoothGATTNotifyRequest(address=A_INT, handle=9, enable=True))
        assert kinds(sent) == ["BluetoothGATTNotifyResponse"]
        link.feed(json.dumps({"t": "notify", "addr": A, "handle": 9, "value": "aGk=", "ind": True}).encode())
        await asyncio.sleep(0)
        link.feed(json.dumps({"t": "notify", "addr": A, "handle": 3, "value": "aGk=", "ind": False}).encode())
        await asyncio.sleep(0)
        assert kinds(sent) == ["BluetoothGATTNotifyResponse", "BluetoothGATTNotifyDataResponse"]
        assert (sent[1].address, sent[1].handle, sent[1].data) == (A_INT, 9, b"hi")
        proxy.handle(PB.BluetoothGATTNotifyRequest(address=A_INT, handle=9, enable=False))
        del sent[:]
        link.feed(json.dumps({"t": "notify", "addr": A, "handle": 9, "value": "aGk="}).encode())
        await asyncio.sleep(0)
        assert sent == []
    run(go())


def test_home_assistant_is_never_left_believing_there_are_no_slots():
    # The first real run: the Echo's slot report went to a proxy that was
    # then rebuilt, the new link started at 0 of 0, and Home Assistant never
    # sent a connection its way. A subscriber that finds nothing known asks.
    async def go():
        echo, link, proxy, sent, tasks = setup()
        assert (link.free, link.limit) == (0, 0)
        proxy.handle(PB.SubscribeBluetoothConnectionsFreeRequest())
        await settle(tasks)
        await asyncio.sleep(0)
        slots = [(m.free, m.limit) for m in sent if isinstance(m, PB.BluetoothConnectionsFreeResponse)]
        assert slots == [(0, 0), (3, 3)]
        assert [m["t"] for m in echo.log] == ["slots"]
        # Once known it is not asked again.
        proxy.handle(PB.SubscribeBluetoothConnectionsFreeRequest())
        await settle(tasks)
        assert [m["t"] for m in echo.log] == ["slots"]
    run(go())


def test_requests_for_one_peer_reach_the_echo_in_order():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        for i in range(8):
            proxy.handle(PB.BluetoothGATTWriteRequest(address=A_INT, handle=9, response=i % 2 == 0,
                                                      data=bytes([i])))
            proxy.handle(PB.BluetoothGATTReadRequest(address=A_INT, handle=9))
        await settle(tasks)
        assert [m["t"] for m in echo.log] == ["connect"] + ["write", "read"] * 8
        reads = [m.data for m in sent if isinstance(m, PB.BluetoothGATTReadResponse)]
        assert reads == [bytes([i]) for i in range(8)]
        # Only the writes that asked for a response got one.
        assert kinds(sent).count("BluetoothGATTWriteResponse") == 4
    run(go())


@pytest.mark.parametrize("refusal,error", [
    ("no_slots", 0x80), ("timeout", 0x08), ("disabled", 0x85), ("not_running", 0x85)])
def test_a_failed_connect_says_why(refusal, error):
    async def go():
        echo, link, proxy, sent, tasks = setup()
        echo.refuse_connect = refusal
        proxy.handle(connect_msg())
        await settle(tasks)
        assert [(m.connected, m.mtu, m.error) for m in sent] == [(False, 0, error)]
    run(go())


def test_gatt_errors_carry_the_peers_code_and_the_handle():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        await settle(tasks)
        del sent[:]
        proxy.handle(PB.BluetoothGATTReadRequest(address=A_INT, handle=77))
        proxy.handle(PB.BluetoothGATTWriteRequest(address=A_INT, handle=99, response=True, data=b"x"))
        await settle(tasks)
        assert [(type(m).__name__, m.handle, m.error) for m in sent] == [
            ("BluetoothGATTErrorResponse", 77, 1), ("BluetoothGATTErrorResponse", 99, 3)]
        # Nothing connected at that address: ESPHome's own "not connected".
        del sent[:]
        proxy.handle(PB.BluetoothGATTReadRequest(address=0xC0000000000B, handle=3))
        proxy.handle(PB.BluetoothGATTGetServicesRequest(address=0xC0000000000B))
        await settle(tasks)
        assert [(m.handle, m.error) for m in sent] == [(3, -1), (0, -1)]
    run(go())


def test_a_failed_write_without_response_is_not_answered():
    # An error now would be taken by the next request on this handle.
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        await settle(tasks)
        del sent[:]
        proxy.handle(PB.BluetoothGATTWriteRequest(address=A_INT, handle=99, response=False, data=b"x"))
        await settle(tasks)
        assert sent == []
    run(go())


@pytest.mark.parametrize("events_first", [False, True])
def test_disconnect_is_confirmed_exactly_once(events_first):
    async def go():
        echo, link, proxy, sent, tasks = setup()
        echo.events_first = events_first
        proxy.handle(connect_msg())
        await settle(tasks)
        del sent[:]
        proxy.handle(connect_msg(kind=PB.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT))
        await settle(tasks)
        down = [m for m in sent if isinstance(m, PB.BluetoothDeviceConnectionResponse)]
        assert [(m.connected, m.error) for m in down] == [(False, 0x16)]
        # A link that was never up still gets its one answer.
        del sent[:]
        proxy.handle(connect_msg(address=0xC0000000000B, kind=PB.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT))
        await settle(tasks)
        assert [(m.address, m.connected, m.error) for m in sent] == [(0xC0000000000B, False, 0)]
    run(go())


def test_disconnect_answers_when_the_echo_sends_no_event(monkeypatch):
    monkeypatch.setattr(G, "DISCONNECT_EVENT_WAIT_S", 0.1)

    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        await settle(tasks)
        del sent[:]
        echo.connected.clear()        # the Echo lost it without telling us
        proxy.handle(connect_msg(kind=PB.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT))
        await settle(tasks)
        assert [(m.connected, m.error) for m in sent] == [(False, 0)]
    run(go())


def test_a_peer_dropping_is_reported_and_stops_its_notifications():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        await settle(tasks)
        proxy.handle(PB.BluetoothGATTNotifyRequest(address=A_INT, handle=9, enable=True))
        del sent[:]
        link.feed(json.dumps({"t": "disconnected", "addr": A, "reason": 8}).encode())
        await asyncio.sleep(0)
        assert [(m.address, m.connected, m.error) for m in sent] == [(A_INT, False, 8)]
        # A reconnect does not inherit the old subscription.
        del sent[:]
        link.feed(json.dumps({"t": "notify", "addr": A, "handle": 9, "value": "aGk="}).encode())
        await asyncio.sleep(0)
        assert sent == []
        # And a second report of the same drop says nothing more.
        link.feed(json.dumps({"t": "disconnected", "addr": A, "reason": 8}).encode())
        await asyncio.sleep(0)
        assert sent == []
    run(go())


def test_pairing_is_refused_and_cache_clear_succeeds():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        for kind in (PB.BLUETOOTH_DEVICE_REQUEST_TYPE_PAIR, PB.BLUETOOTH_DEVICE_REQUEST_TYPE_UNPAIR,
                     PB.BLUETOOTH_DEVICE_REQUEST_TYPE_CLEAR_CACHE):
            proxy.handle(connect_msg(kind=kind))
        await settle(tasks)
        assert kinds(sent) == ["BluetoothDevicePairingResponse", "BluetoothDeviceUnpairingResponse",
                               "BluetoothDeviceClearCacheResponse"]
        assert (sent[0].paired, sent[1].success, sent[2].success) == (False, False, True)
        assert echo.log == []          # none of it reaches the Echo
    run(go())


def test_home_assistant_leaving_drops_its_links():
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        proxy.handle(connect_msg(address=0xC0000000000B, addr_type=0))
        await settle(tasks)
        del sent[:]
        proxy.close()
        await settle(tasks)
        assert echo.connected == {} and sent == []
        assert link.on_notify is None and link.on_disconnected is None
    run(go())


def test_an_event_never_overtakes_the_result_sent_before_it():
    # The Echo answers a write and then reports the link dropping. Home
    # Assistant must hear them in that order: told of the drop first, its
    # client fails the write it is still waiting on.
    async def go():
        echo, link, proxy, sent, tasks = setup()
        proxy.handle(connect_msg())
        await settle(tasks)
        del sent[:]
        answer = echo._answer

        def answer_then_drop(req):
            answer(req)
            if req["t"] == "write":
                echo._emit(t="disconnected", addr=A, reason=8)
                echo._emit(t="notify", addr=A, handle=9, value="aGk=")
        echo._answer = answer_then_drop
        proxy.handle(PB.BluetoothGATTWriteRequest(address=A_INT, handle=9, response=True, data=b"x"))
        await settle(tasks)
        await asyncio.sleep(0)
        assert kinds(sent) == ["BluetoothGATTWriteResponse", "BluetoothDeviceConnectionResponse"]
    run(go())


def test_messages_that_are_not_ours_are_left_alone():
    echo, link, proxy, sent, tasks = setup()
    assert proxy.handle(object()) is False and sent == [] and tasks == []
