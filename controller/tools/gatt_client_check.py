#!/usr/bin/env python3
"""
Check the Bluetooth connection handlers against Home Assistant's own client.

tests/test_ble_gatt.py drives em_ble_gatt with stand-in messages, which shows
it does what we THINK the ESPHome exchange is. This runs the real
`aioesphomeapi` client — encrypted, as an active proxy must be — against a
real listener using em_ble_gatt with the real protobufs, and a stand-in Echo
behind it:

    connect, list services, read, write with and without response,
    subscribe and receive a notification, a refused read, disconnect,
    the peer dropping the link, and a connect the Echo refuses

Two processes, for the reason tools/noise_client_check.py gives.

    docker run --rm -v "$PWD":/c -w /c python:3.12 sh -c \\
      'pip install -q aioesphomeapi && python tools/gatt_client_check.py'

With --real-proxy the listener is em_ble_proxy's own DeviceBleProxyServer
rather than a bare one, so the check also covers what the suite cannot
import: the encrypted satellite, its feature flags and the hand-off to
em_ble_gatt. That needs the controller's dependencies, so run it in the
controller image with this tree mounted over /app:

    docker run --rm -v "$PWD":/app -w /app --entrypoint sh \\
      ghcr.io/wilbowes/echomuse-controller:<tag> -c \\
      'pip install -q aioesphomeapi && python tools/gatt_client_check.py --real-proxy'

With --live the client is pointed at a RUNNING controller's proxy for one
Echo, so the whole path is real: this client, the controller, the Echo and a
Bluetooth device near it. It is how a connection is forced through a chosen
Echo, which Home Assistant offers no way to do (it picks the proxy itself).
The proxy takes one client at a time, so disable Home Assistant's entry for
that proxy while this runs. With no address it lists the connectable devices
the Echo hears for 15s and exits.

    docker run --rm --network host -v "$PWD":/c -w /c python:3.12 sh -c \\
      'pip install -q aioesphomeapi && python tools/gatt_client_check.py \\
         --live <controller host> <proxy port> <key, base64> [AA:BB:CC:DD:EE:FF]'
"""
import asyncio
import base64
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 16998
NAME = "gatt-check"
PEER = 0xC0000000000A
REFUSED = 0xC0000000000D
UUID_CUSTOM = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
CCCD = "00002902-0000-1000-8000-00805f9b34fb"


def serve(key_hex: str, real_proxy: bool = False) -> None:
    sys.path.insert(0, HERE)
    import em_ble_gatt as G
    from esphome.satellite_server import SatelliteServerProtocol, _HANDLED
    from esphome.vendor import api_pb2

    class Echo:
        """Answers as device/internal/bluetooth/bridge.go does."""

        def __init__(self):
            self.link = None
            self.up = set()
            self.values = {3: b"Chonky Monkey", 9: b""}

        async def send(self, raw):
            asyncio.get_running_loop().call_soon(self.answer, json.loads(raw))
            return True

        def emit(self, **m):
            self.link.feed(json.dumps(m).encode())

        def slots(self):
            self.emit(t="slots", free=3 - len(self.up), limit=3, addrs=sorted(self.up))

        def answer(self, r):
            t, n, a = r["t"], r["req"], r.get("addr")
            if t == "connect":
                if a == G.int_to_addr(REFUSED):
                    return self.emit(t="result", req=n, ok=False, error="timeout")
                self.up.add(a)
                self.emit(t="result", req=n, ok=True, mtu=185)
                return self.slots()
            if t == "disconnect":
                self.emit(t="result", req=n, ok=True)
                if a in self.up:
                    self.up.discard(a)
                    self.emit(t="disconnected", addr=a, reason=0x16)
                    self.slots()
                return
            if t == "services":
                return self.emit(t="result", req=n, ok=True, services=[
                    {"uuid": "00001800-0000-1000-8000-00805f9b34fb", "start": 1, "end": 5, "chars": [
                        {"uuid": "00002a00-0000-1000-8000-00805f9b34fb", "handle": 2,
                         "value_handle": 3, "props": 2, "descs": []}]},
                    {"uuid": UUID_CUSTOM, "start": 6, "end": 65535, "chars": [
                        {"uuid": UUID_CUSTOM, "handle": 8, "value_handle": 9, "props": 0x1E,
                         "descs": [{"uuid": CCCD, "handle": 10}]}]}])
            if t == "read":
                if r["handle"] not in self.values:
                    return self.emit(t="result", req=n, ok=False, error="att", att=5)
                return self.emit(t="result", req=n, ok=True,
                                 value=base64.b64encode(self.values[r["handle"]]).decode())
            if t == "write":
                value = base64.b64decode(r.get("value") or "")
                self.values[r["handle"]] = value
                self.emit(t="result", req=n, ok=True)
                if r["handle"] == 10 and value == b"\x01\x00":
                    # Subscribed: the peer notifies.
                    self.emit(t="notify", addr=a, handle=9,
                              value=base64.b64encode(b"tick").decode(), ind=False)
                if value == b"drop me":
                    self.up.discard(a)
                    self.emit(t="disconnected", addr=a, reason=0x08)
                    self.slots()

    echo = Echo()

    if real_proxy:
        import em_ble_proxy

        async def to_echo(device_id, payload):
            return await echo.send(payload)

        em_ble_proxy.set_gatt_sender(to_echo)
        server = em_ble_proxy.DeviceBleProxyServer(
            "G090LF0000000001", "Check", "02:EC:00:00:00:02", PORT, bytes.fromhex(key_hex))
        echo.link = server.link
        server.link.free, server.link.limit = 3, 3

        async def main_real():
            await server.start("127.0.0.1")
            print("listening", flush=True)
            await asyncio.Event().wait()

        asyncio.run(main_real())
        return

    link = G.GattLink(echo.send)
    echo.link = link
    link.free, link.limit = 3, 3

    class Proxy(SatelliteServerProtocol):
        def __init__(self):
            super().__init__(server_name=NAME, log_name="check")
            self.set_encryption(bytes.fromhex(key_hex), NAME, "02ec00000002")
            self.gatt = G.GattProxy(link, api_pb2, self._send_many, asyncio.ensure_future)

        def handle_message(self, msg):
            if isinstance(msg, api_pb2.DeviceInfoRequest):
                yield api_pb2.DeviceInfoResponse(
                    name=NAME, mac_address="02:EC:00:00:00:02",
                    bluetooth_proxy_feature_flags=0b110111)
                return
            if self.gatt.handle(msg):
                yield _HANDLED

    async def main():
        server = await asyncio.get_running_loop().create_server(Proxy, "127.0.0.1", PORT)
        print("listening", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


async def client(key: bytes) -> int:
    import aioesphomeapi
    from aioesphomeapi import BluetoothProxyFeature
    from aioesphomeapi.core import BluetoothGATTAPIError

    failed = 0

    def check(label, ok, detail=""):
        nonlocal failed
        print("PASS" if ok else "FAIL", label, detail if not ok else "")
        failed += not ok

    c = aioesphomeapi.APIClient("127.0.0.1", PORT, None,
                                noise_psk=base64.b64encode(key).decode())
    await c.connect(login=True)
    info = await c.device_info()
    print("device:", info.name, "| api_encryption_supported:",
          getattr(info, "api_encryption_supported", "?"))
    flags = info.bluetooth_proxy_feature_flags_compat(c.api_version)
    check("feature flags offer connections",
          bool(flags & BluetoothProxyFeature.ACTIVE_CONNECTIONS))

    slots = []
    c.subscribe_bluetooth_connections_free(lambda *a: slots.append(a))
    states = []
    unsub = await c.bluetooth_device_connect(
        PEER, lambda connected, mtu, error: states.append((connected, mtu, error)),
        feature_flags=flags, has_cache=False, address_type=1, timeout=10)
    check("connect", states == [(True, 185, 0)], states)

    services = (await c.bluetooth_gatt_get_services(PEER)).services
    chars = {ch.uuid: ch for s in services for ch in s.characteristics}
    custom = chars.get(UUID_CUSTOM)
    check("services", len(services) == 2 and custom is not None and custom.handle == 9
          and custom.properties == 0x1E and custom.descriptors[0].uuid == CCCD
          and custom.descriptors[0].handle == 10,
          [(s.uuid, [(ch.uuid, ch.handle) for ch in s.characteristics]) for s in services])

    check("read", bytes(await c.bluetooth_gatt_read(PEER, 3)) == b"Chonky Monkey")
    await c.bluetooth_gatt_write(PEER, 9, b"Good golly", True)
    check("write with response, read back", bytes(await c.bluetooth_gatt_read(PEER, 9)) == b"Good golly")
    await c.bluetooth_gatt_write(PEER, 9, b"quietly", False)
    check("write without response, read back", bytes(await c.bluetooth_gatt_read(PEER, 9)) == b"quietly")

    notes = []
    await c.bluetooth_gatt_start_notify(PEER, 9, lambda handle, data: notes.append((handle, bytes(data))))
    await c.bluetooth_gatt_write_descriptor(PEER, 10, b"\x01\x00")
    await asyncio.sleep(0.2)
    check("notification after subscribing", notes == [(9, b"tick")], notes)

    try:
        await c.bluetooth_gatt_read(PEER, 77)
        check("refused read raises", False, "no error")
    except BluetoothGATTAPIError as e:
        check("refused read raises with the peer's code", e.error.error == 5, e)

    await c.bluetooth_device_disconnect(PEER)
    await asyncio.sleep(0.1)
    check("disconnect", states == [(True, 185, 0), (False, 0, 0x16)], states)
    unsub()

    del states[:]
    unsub = await c.bluetooth_device_connect(
        PEER, lambda connected, mtu, error: states.append((connected, mtu, error)),
        feature_flags=flags, has_cache=False, address_type=1, timeout=10)
    # The write is answered and THEN the link drops: the write must succeed.
    await c.bluetooth_gatt_write(PEER, 9, b"drop me", True)
    await asyncio.sleep(0.2)
    check("write answered, then the peer dropping reaches the client",
          states == [(True, 185, 0), (False, 0, 8)], states)
    unsub()

    # The client does not raise for a failed connect: it reports it through
    # the state callback, which is what Home Assistant's Bluetooth layer reads.
    del states[:]
    unsub = await c.bluetooth_device_connect(
        REFUSED, lambda connected, mtu, error: states.append((connected, mtu, error)),
        feature_flags=flags, has_cache=False, address_type=1, timeout=10)
    check("a refused connect is reported with the reason", states == [(False, 0, 8)], states)
    unsub()

    check("free slots were reported", len(slots) >= 2 and slots[0][:2] == (3, 3), slots)
    await c.disconnect(force=True)
    return failed


DEVICE_NAME = "00002a00-0000-1000-8000-00805f9b34fb"


async def live(host: str, port: int, psk: str, address: str | None) -> int:
    import aioesphomeapi
    from aioesphomeapi import BluetoothProxyFeature

    failed = 0

    def check(label, ok, detail=""):
        nonlocal failed
        print("PASS" if ok else "FAIL", label, detail)
        failed += not ok

    c = aioesphomeapi.APIClient(host, port, None, noise_psk=psk)
    await c.connect(login=True)
    info = await c.device_info()
    flags = info.bluetooth_proxy_feature_flags_compat(c.api_version)
    print(f"proxy: {info.name} ({info.mac_address}), firmware {info.esphome_version}")
    check("feature flags offer connections",
          bool(flags & BluetoothProxyFeature.ACTIVE_CONNECTIONS), hex(flags))

    slots = []
    c.subscribe_bluetooth_connections_free(lambda *a: slots.append(a))

    # What the Echo hears, with the address type each device advertises: a
    # connect needs the type, and a random address asked for as public fails.
    heard = {}

    names = {}

    def local_name(data: bytes) -> str:
        # AD structures: length, type, value. 0x08/0x09 are the local name
        # and 0x02/0x03 the 16-bit service UUIDs. A passive scan never sees
        # the scan response, where a phone puts its name, so the services
        # are often the only way to tell which device is which.
        i, out = 0, []
        while i + 1 < len(data) and data[i]:
            n, kind = data[i], data[i + 1]
            value = data[i + 2:i + 1 + n]
            if kind in (0x08, 0x09):
                out.append(value.decode("utf-8", "replace"))
            elif kind in (0x02, 0x03):
                out += [f"svc {value[j + 1]:02x}{value[j]:02x}"
                        for j in range(0, len(value) - 1, 2)]
            i += 1 + n
        return " ".join(out)

    def on_adverts(resp):
        for a in resp.advertisements:
            heard[a.address] = (a.address_type, a.rssi)
            name = local_name(bytes(a.data))
            if name:
                names[a.address] = name

    stop = c.subscribe_bluetooth_le_raw_advertisements(on_adverts)
    want = int(address.replace(":", ""), 16) if address else None
    for _ in range(150):
        await asyncio.sleep(0.1)
        if want is not None and want in heard:
            break
    if want is None:
        stop()
        for addr, (kind, rssi) in sorted(heard.items(), key=lambda kv: -kv[1][1]):
            mac = ":".join(f"{addr:012X}"[i:i + 2] for i in range(0, 12, 2))
            print(f"  {mac}  {'random' if kind else 'public'}  {rssi}dBm  {names.get(addr, '')}")
        print(f"{len(heard)} devices heard in 15s; pass one as the address")
        await c.disconnect(force=True)
        return 0
    check("the Echo hears the device", want in heard, f"{len(heard)} others heard")
    kind = heard.get(want, (1, 0))[0]
    stop()

    states = []
    t0 = asyncio.get_running_loop().time()
    unsub = await c.bluetooth_device_connect(
        want, lambda connected, mtu, error: states.append((connected, mtu, error)),
        feature_flags=flags, has_cache=False, address_type=kind, timeout=30)
    took = asyncio.get_running_loop().time() - t0
    connected = bool(states) and states[0][0]
    check("connect", connected, f"{states} in {took:.1f}s")
    if connected:
        services = (await c.bluetooth_gatt_get_services(want)).services
        chars = [ch for s in services for ch in s.characteristics]
        check("services", len(services) > 0,
              f"{len(services)} services, {len(chars)} characteristics")
        for s in services:
            print("   ", s.uuid, [ch.uuid[4:8] if ch.uuid.endswith("-0000-1000-8000-00805f9b34fb")
                                  else ch.uuid for ch in s.characteristics])
        name = next((ch for ch in chars if ch.uuid == DEVICE_NAME), None)
        if name is not None:
            try:
                value = bytes(await c.bluetooth_gatt_read(want, name.handle))
                check("read Device Name", True, repr(value))
            except Exception as e:  # the peer may refuse; report what it said
                check("read Device Name", False, repr(e))
        await c.bluetooth_device_disconnect(want)
        await asyncio.sleep(0.5)
        check("disconnect", len(states) == 2 and not states[1][0], states)
    unsub()
    print("slot reports:", [s[:2] for s in slots])
    check("free slots were reported", len(slots) >= 1, "")
    await c.disconnect(force=True)
    return failed


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--live":
        sys.exit(1 if asyncio.run(live(
            sys.argv[2], int(sys.argv[3]), sys.argv[4],
            sys.argv[5] if len(sys.argv) > 5 else None)) else 0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--serve":
        serve(sys.argv[2], real_proxy="--real-proxy" in sys.argv)
        sys.exit(0)
    key = os.urandom(32)
    proc = subprocess.Popen([sys.executable, __file__, "--serve", key.hex()]
                            + [a for a in sys.argv[1:] if a == "--real-proxy"],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "listening"
        sys.exit(1 if asyncio.run(client(key)) else 0)
    finally:
        proc.terminate()
