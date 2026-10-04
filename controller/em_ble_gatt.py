"""
em_ble_gatt.py — Bluetooth connections through an Echo, controller side (#656)
==============================================================================

Home Assistant asks an ESPHome Bluetooth proxy to connect to a peripheral and
read, write and subscribe on it. The Echo does the radio work
(device/internal/bluetooth); this is the part in between:

  GattLink   one per Echo. Speaks the 0x08 data-plane messages in
             docs/device-controller-interface.md: numbers requests, matches
             results to them, and tracks the Echo's connection slots.
  GattProxy  one per Home Assistant connection to that Echo's proxy port.
             Turns ESPHome's Bluetooth messages into GattLink requests and
             the answers back into ESPHome's.

Neither imports aiohttp, zeroconf or the protobuf module: the message classes
are handed in, so the suite drives both with plain stand-ins and
tools/gatt_client_check.py drives them with the real ones and Home
Assistant's own client.

Three things here are Home Assistant's rules rather than ours, read out of
aioesphomeapi's client:

- A request that waits for an answer matches on address AND handle, and takes
  a BluetoothGATTErrorResponse for that pair as its failure. So a write sent
  WITHOUT response must never be answered with an error: nothing is waiting,
  and the error would fail the next request on that handle instead.
- A disconnect is confirmed by BluetoothDeviceConnectionResponse with
  connected=false, exactly once, whoever ended the link.
- Subscribing is Home Assistant's job on this kind of connection: it writes
  the CCCD descriptor itself. A notify request only decides which handles'
  notifications are forwarded.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("echomuse.blegatt")

# The data-plane frame code, both directions.
FRAME_BLE_GATT = 0x08

# Both sit just past the device's own limits (20s to connect, 30s for an ATT
# transaction), so the device's answer is the one that normally arrives.
CONNECT_TIMEOUT_S = 25.0
REQUEST_TIMEOUT_S = 35.0
# How long a disconnect waits for the Echo's own event before answering itself.
DISCONNECT_EVENT_WAIT_S = 2.0

# What Home Assistant is told when the Echo cannot say more. These are
# esp-idf's GATT status values, which is the vocabulary its client decodes.
ESP_GATT_NO_RESOURCES = 0x80
ESP_GATT_BUSY = 0x84
ESP_GATT_ERROR = 0x85
ESP_GATT_ILLEGAL_PARAMETER = 0x87
ESP_GATT_INVALID_ATTR_LEN = 0x0D
ESP_GATT_CONN_TIMEOUT = 0x08
ESP_GATT_NOT_CONNECTED = -1          # ESPHome's own value for "no such link"

_ESP_ERRORS = {
    "no_slots": ESP_GATT_NO_RESOURCES,
    "timeout": ESP_GATT_CONN_TIMEOUT,
    "not_connected": ESP_GATT_NOT_CONNECTED,
    "disconnected": ESP_GATT_NOT_CONNECTED,
    "busy": ESP_GATT_BUSY,
    "too_long": ESP_GATT_INVALID_ATTR_LEN,
    "bad_request": ESP_GATT_ILLEGAL_PARAMETER,
}


_TIMED_OUT = object()


class GattError(Exception):
    """A request the Echo refused or could not carry out."""

    def __init__(self, code: str, att: Optional[int] = None, detail: str = "") -> None:
        super().__init__(f"{code}" + (f" (att 0x{att:02x})" if att is not None else "")
                         + (f": {detail}" if detail else ""))
        self.code = code
        self.att = att

    @property
    def esp_error(self) -> int:
        """The status Home Assistant is given for this failure."""
        if self.code == "att" and self.att is not None:
            return self.att          # ATT error codes are esp-idf's low values
        return _ESP_ERRORS.get(self.code, ESP_GATT_ERROR)


def addr_to_int(addr: str) -> int:
    """'aa:bb:cc:dd:ee:ff' → the uint64 ESPHome carries."""
    parts = addr.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"not a Bluetooth address: {addr!r}")
    return int("".join(parts), 16)


def int_to_addr(value: int) -> str:
    if not 0 <= value < 1 << 48:
        raise ValueError(f"not a Bluetooth address: {value!r}")
    raw = f"{value:012x}"
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def uuid_to_pair(uuid: str) -> list:
    """A 128-bit UUID as ESPHome carries it: [high 64 bits, low 64 bits]."""
    raw = uuid.replace("-", "")
    if len(raw) != 32:
        raise ValueError(f"not a 128-bit UUID: {uuid!r}")
    value = int(raw, 16)
    return [value >> 64, value & 0xFFFFFFFFFFFFFFFF]


class GattLink:
    """
    One Echo's connection bridge.

    `send` writes one message's bytes to the Echo and returns False if there
    is nowhere to write it. Callbacks: on_notify(addr, handle, value,
    indication), on_disconnected(addr, reason), on_slots().
    """

    def __init__(self, send: Callable[[bytes], Awaitable[bool]]) -> None:
        self._send = send
        self._next_req = 1
        self._pending: dict = {}
        self.free = 0
        self.limit = 0
        self.addrs: set = set()
        self.mtu: dict = {}
        self.on_notify: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None
        self.on_slots: Optional[Callable] = None

    async def request(self, kind: str, timeout: float = REQUEST_TIMEOUT_S, **fields) -> dict:
        req = self._next_req
        self._next_req = req % 0xFFFFFFFF + 1
        if isinstance(fields.get("value"), (bytes, bytearray)):
            fields["value"] = base64.b64encode(bytes(fields["value"])).decode()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req] = future
        # Not asyncio.wait_for: the future is awaited directly so this task
        # resumes exactly one loop step after the result is fed, which is
        # what keeps a result ahead of an event the Echo sent after it (see
        # feed).
        timer = loop.call_later(
            timeout, lambda: future.done() or future.set_result(_TIMED_OUT))
        try:
            if not await self._send(json.dumps({"t": kind, "req": req, **fields}).encode()):
                raise GattError("not_running", detail="the Echo is not connected")
            result = await future
            if result is _TIMED_OUT:
                raise GattError("timeout", detail=f"no answer to {kind}")
        finally:
            timer.cancel()
            self._pending.pop(req, None)
        if not result.get("ok"):
            raise GattError(str(result.get("error") or "failed"), result.get("att"),
                            str(result.get("detail") or ""))
        if kind == "connect":
            self.mtu[fields.get("addr")] = int(result.get("mtu") or 23)
        return result

    def feed(self, payload: bytes) -> None:
        """
        One message from the Echo. Anything malformed is dropped.

        ORDER IS KEPT. A result wakes the task that asked, which runs on a
        later loop step; an event handled here and now would overtake it. The
        Echo sends "write ok" and then "disconnected", and Home Assistant told
        the link dropped while it still waits for the write treats the write
        as failed — found by running its real client (tools/
        gatt_client_check.py). So events are queued behind the wake-up.
        """
        try:
            msg = json.loads(payload)
            kind = msg["t"]
        except (ValueError, TypeError, KeyError):
            return
        if kind == "result":
            future = self._pending.get(msg.get("req"))
            if future is not None and not future.done():
                future.set_result(msg)
            return
        if kind == "slots" and "req" in msg:
            # The answer to a slots query is the event itself, with the id.
            future = self._pending.get(msg.get("req"))
            if future is not None and not future.done():
                future.set_result({"ok": True})
        asyncio.get_running_loop().call_soon(self._event, kind, msg)

    def _event(self, kind: str, msg: dict) -> None:
        try:
            if kind == "notify":
                if self.on_notify:
                    self.on_notify(msg["addr"], int(msg["handle"]),
                                   base64.b64decode(msg.get("value") or ""),
                                   bool(msg.get("ind")))
            elif kind == "disconnected":
                addr = msg["addr"]
                self.addrs.discard(addr)
                self.mtu.pop(addr, None)
                if self.on_disconnected:
                    self.on_disconnected(addr, int(msg.get("reason") or 0))
            elif kind == "slots":
                self.free = int(msg.get("free") or 0)
                self.limit = int(msg.get("limit") or 0)
                self.addrs = set(msg.get("addrs") or [])
                if self.on_slots:
                    self.on_slots()
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            log.warning(f"malformed {kind!r} from the Echo dropped: {e}")

    def reset(self) -> None:
        """The Echo went away: every link is gone and every request is dead."""
        for future in self._pending.values():
            if not future.done():
                future.set_result({"ok": False, "error": "not_running"})
        gone, self.addrs = self.addrs, set()
        self.mtu.clear()
        self.free = 0
        for addr in sorted(gone):
            if self.on_disconnected:
                self.on_disconnected(addr, 0x16)
        if self.on_slots:
            self.on_slots()


class GattProxy:
    """
    One Home Assistant connection's use of a GattLink.

    `pb` is the ESPHome protobuf module (or a stand-in with the same class
    names), `send` writes a list of its messages to Home Assistant, and
    `spawn` runs a coroutine in the background.
    """

    def __init__(self, link: GattLink, pb, send: Callable[[list], None],
                 spawn: Callable[[Awaitable], object], log_name: str = "blegatt") -> None:
        self.link = link
        self.pb = pb
        self._send = send
        self._spawn = spawn
        self._log_name = log_name
        self._connected: set = set()         # addresses HA has been told are up
        self._notify: set = set()            # (addr, handle) HA asked to hear
        self._locks: dict = {}
        self._slots_subscribed = False
        self._closed = False
        link.on_notify = self._on_notify
        link.on_disconnected = self._on_disconnected
        link.on_slots = self._on_slots

    # ── from Home Assistant ───────────────────────────────────────────────

    def handle(self, msg) -> bool:
        """True if `msg` was a Bluetooth connection message and is dealt with."""
        pb = self.pb
        if isinstance(msg, pb.SubscribeBluetoothConnectionsFreeRequest):
            self._slots_subscribed = True
            self._send([self._slots_msg()])
            if self.link.limit == 0:
                # Nothing heard from the Echo yet. Home Assistant treats 0 of
                # 0 as "no connections here", so ask rather than leave it.
                self._spawn(self._ask_slots())
            return True
        if isinstance(msg, pb.BluetoothDeviceRequest):
            self._run(msg.address, self._device_request(msg))
            return True
        if isinstance(msg, pb.BluetoothGATTGetServicesRequest):
            self._run(msg.address, self._services(msg.address))
            return True
        if isinstance(msg, (pb.BluetoothGATTReadRequest, pb.BluetoothGATTReadDescriptorRequest)):
            self._run(msg.address, self._read(msg.address, msg.handle))
            return True
        if isinstance(msg, pb.BluetoothGATTWriteRequest):
            self._run(msg.address, self._write(msg.address, msg.handle, bytes(msg.data),
                                              bool(msg.response)))
            return True
        if isinstance(msg, pb.BluetoothGATTWriteDescriptorRequest):
            self._run(msg.address, self._write(msg.address, msg.handle, bytes(msg.data), True))
            return True
        if isinstance(msg, pb.BluetoothGATTNotifyRequest):
            key = (int_to_addr(msg.address), msg.handle)
            if msg.enable:
                self._notify.add(key)
            else:
                self._notify.discard(key)
            self._send([pb.BluetoothGATTNotifyResponse(address=msg.address, handle=msg.handle)])
            return True
        return False

    def close(self) -> None:
        """Home Assistant disconnected: nothing is left to use its links."""
        self._closed = True
        for addr in sorted(self._connected):
            self._spawn(self._quiet_disconnect(addr))
        self._connected.clear()
        self._notify.clear()
        if self.link.on_notify == self._on_notify:
            self.link.on_notify = self.link.on_disconnected = self.link.on_slots = None

    async def _ask_slots(self) -> None:
        try:
            await self.link.request("slots", timeout=10.0)
        except GattError as e:
            # An Echo that is not connected yet is the ordinary case at startup.
            level = logging.DEBUG if e.code == "not_running" else logging.INFO
            log.log(level, f"[{self._log_name}] slot query: {e}")

    async def _quiet_disconnect(self, addr: str) -> None:
        try:
            await self.link.request("disconnect", addr=addr)
        except GattError:
            pass

    def _run(self, address: int, coro) -> None:
        """Run one request, after any earlier one for the same peer."""
        lock = self._locks.setdefault(address, asyncio.Lock())

        async def ordered():
            async with lock:
                try:
                    await coro
                except Exception as e:  # one bad request must not end the rest
                    log.error(f"[{self._log_name}] request for "
                              f"{int_to_addr(address)} failed: {e!r}")

        self._spawn(ordered())

    def _out(self, msgs: list) -> None:
        if not self._closed:
            self._send(msgs)

    async def _device_request(self, msg) -> None:
        pb = self.pb
        address, addr = msg.address, int_to_addr(msg.address)
        kind = msg.request_type
        if kind in (pb.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT,
                    pb.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITH_CACHE,
                    pb.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITHOUT_CACHE):
            addr_type = int(msg.address_type) if msg.has_address_type else 0
            try:
                result = await self.link.request("connect", CONNECT_TIMEOUT_S,
                                                 addr=addr, addr_type=addr_type)
                mtu = int(result.get("mtu") or 23)
            except GattError as e:
                if e.code != "already_connected":
                    log.info(f"[{self._log_name}] connect to {addr} failed: {e}")
                    self._out([pb.BluetoothDeviceConnectionResponse(
                        address=address, connected=False, mtu=0, error=e.esp_error)])
                    return
                mtu = self.link.mtu.get(addr, 23)
            self._connected.add(addr)
            self.link.addrs.add(addr)
            log.info(f"[{self._log_name}] connected {addr} (mtu {mtu})")
            self._out([pb.BluetoothDeviceConnectionResponse(
                address=address, connected=True, mtu=mtu, error=0)])
        elif kind == pb.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT:
            was_up = addr in self._connected
            try:
                await self.link.request("disconnect", addr=addr)
            except GattError as e:
                log.info(f"[{self._log_name}] disconnect from {addr}: {e}")
            # Home Assistant waits for exactly one connected=false. For a link
            # that was up it is the Echo's `disconnected` event, which may
            # land before or after this result; for one that was not, no
            # event is coming and the answer has to be given here. The wait
            # covers an Echo that said "already gone" about a link we still
            # believed in.
            for _ in range(int(DISCONNECT_EVENT_WAIT_S / 0.05)):
                if addr not in self._connected:
                    break
                await asyncio.sleep(0.05)
            if not was_up or addr in self._connected:
                self._connected.discard(addr)
                self._out([pb.BluetoothDeviceConnectionResponse(
                    address=address, connected=False, mtu=0, error=0)])
        elif kind == pb.BLUETOOTH_DEVICE_REQUEST_TYPE_PAIR:
            self._out([pb.BluetoothDevicePairingResponse(
                address=address, paired=False, error=ESP_GATT_ERROR)])
        elif kind == pb.BLUETOOTH_DEVICE_REQUEST_TYPE_UNPAIR:
            self._out([pb.BluetoothDeviceUnpairingResponse(
                address=address, success=False, error=ESP_GATT_ERROR)])
        elif kind == pb.BLUETOOTH_DEVICE_REQUEST_TYPE_CLEAR_CACHE:
            # Nothing is cached across connections on the Echo.
            self._out([pb.BluetoothDeviceClearCacheResponse(
                address=address, success=True, error=0)])

    async def _services(self, address: int) -> None:
        pb = self.pb
        try:
            result = await self.link.request("services", addr=int_to_addr(address))
            out = []
            for s in result.get("services") or []:
                out.append(pb.BluetoothGATTGetServicesResponse(address=address, services=[
                    pb.BluetoothGATTService(
                        uuid=uuid_to_pair(s["uuid"]), handle=int(s["start"]),
                        characteristics=[
                            pb.BluetoothGATTCharacteristic(
                                uuid=uuid_to_pair(c["uuid"]),
                                # ESPHome's characteristic handle is the VALUE
                                # handle: it is what read and write address.
                                handle=int(c["value_handle"]),
                                properties=int(c["props"]),
                                descriptors=[
                                    pb.BluetoothGATTDescriptor(
                                        uuid=uuid_to_pair(d["uuid"]), handle=int(d["handle"]))
                                    for d in c.get("descs") or []])
                            for c in s.get("chars") or []])]))
        except GattError as e:
            self._out([pb.BluetoothGATTErrorResponse(address=address, handle=0, error=e.esp_error)])
            return
        except (KeyError, ValueError, TypeError) as e:
            log.warning(f"[{self._log_name}] malformed service table: {e!r}")
            self._out([pb.BluetoothGATTErrorResponse(address=address, handle=0,
                                                     error=ESP_GATT_ERROR)])
            return
        self._out(out + [pb.BluetoothGATTGetServicesDoneResponse(address=address)])

    async def _read(self, address: int, handle: int) -> None:
        pb = self.pb
        try:
            result = await self.link.request("read", addr=int_to_addr(address), handle=handle)
            data = base64.b64decode(result.get("value") or "")
        except GattError as e:
            self._out([pb.BluetoothGATTErrorResponse(address=address, handle=handle,
                                                     error=e.esp_error)])
            return
        self._out([pb.BluetoothGATTReadResponse(address=address, handle=handle, data=data)])

    async def _write(self, address: int, handle: int, data: bytes, response: bool) -> None:
        pb = self.pb
        try:
            await self.link.request("write", addr=int_to_addr(address), handle=handle,
                                    value=data, response=response)
        except GattError as e:
            if response:
                self._out([pb.BluetoothGATTErrorResponse(address=address, handle=handle,
                                                         error=e.esp_error)])
            else:
                # Nothing is waiting for this, and an error sent now would be
                # taken by the NEXT request on this handle.
                log.info(f"[{self._log_name}] write without response to "
                         f"{int_to_addr(address)} handle {handle} failed: {e}")
            return
        if response:
            self._out([pb.BluetoothGATTWriteResponse(address=address, handle=handle)])

    # ── from the Echo ─────────────────────────────────────────────────────

    def _slots_msg(self):
        return self.pb.BluetoothConnectionsFreeResponse(
            free=self.link.free, limit=self.link.limit,
            allocated=sorted(addr_to_int(a) for a in self.link.addrs))

    def _on_slots(self) -> None:
        if self._slots_subscribed:
            self._out([self._slots_msg()])

    def _on_notify(self, addr: str, handle: int, value: bytes, indication: bool) -> None:
        if (addr, handle) in self._notify:
            self._out([self.pb.BluetoothGATTNotifyDataResponse(
                address=addr_to_int(addr), handle=handle, data=value)])

    def _on_disconnected(self, addr: str, reason: int) -> None:
        self._notify = {k for k in self._notify if k[0] != addr}
        if addr in self._connected:
            self._connected.discard(addr)
            log.info(f"[{self._log_name}] {addr} disconnected (reason 0x{reason:02x})")
            self._out([self.pb.BluetoothDeviceConnectionResponse(
                address=addr_to_int(addr), connected=False, mtu=0, error=reason)])
