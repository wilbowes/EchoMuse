"""
em_ble_proxy.py — Bluetooth proxy ESPHome servers
==================================================

Presents each Echo Dot's BLE scanner to Home Assistant as a *separate*
ESPHome device (a bluetooth_proxy), distinct from the voice satellite:
its own TCP port, its own mDNS `_esphomelib._tcp` entry, its own MAC-keyed
identity in HA's device registry. Deliberate — the voice assistant and the
BT proxy are independent capabilities and Wil wants them managed (and
visible in HA) independently.

Data path: device scans passively over /dev/stpbt (device/internal/
bluetooth), batches adverts up the /control WebSocket as `ble_adverts`
JSON; em_controller hands each batch to forward_adverts(), which re-encodes
it as BluetoothLERawAdvertisementsResponse for the subscribed HA connection.

Lifecycle: a proxy server exists only while the device's effective config
has bleProxyEnabled. reconcile() is the single entry point — called at
startup, on device config pushes (em_api), and indirectly via
device_connected/device_disconnected. Enabled + device online → listener
up; enabled + offline → mDNS registered but port down (HA shows
unavailable, same as the voice satellite); disabled → nothing exists.

Identity: MAC is the voice satellite's serial-derived MAC with the second
nibble XOR'd (locally-administered bit flipped) — deterministic, stable,
never collides with the voice identity that HA keys devices on. The chip's
real BD address is diagnostics-only (device stats), NOT identity: it isn't
known until the scanner first runs, and flipping identity after HA has
discovered the proxy would orphan the HA device entry.

Connections (#656): with bleProxyConnections on, and firmware announcing
`ble_connect`, the proxy also offers Home Assistant active connections —
em_ble_gatt holds that logic. Two things change with it, both at listener
creation, so toggling the setting rebuilds the proxy:

  - THE PORT REQUIRES AN ENCRYPTION KEY. A connection can operate the device
    at the other end (a lock), and this port has no other authentication, so
    connections are never offered on a plaintext listener. There is no
    setting for "connections without encryption" and there must not be one
    (Wil, 2026-10-03). The key is per device, assigned once, kept in the
    database and handed to the dashboard only on an admin's request.
  - The feature flags Home Assistant reads once at connect gain
    ACTIVE_CONNECTIONS, REMOTE_CACHING and CACHE_CLEARING.

A passive proxy is unchanged: plaintext, and nothing for Home Assistant to
re-enter.

Entities: one diagnostic sensor (total adverts seen). HA's ESPHome
integration was observed to silently ignore zero-entity devices (see
esphome/feature_flags.py MediaPlayerEntityFeature docstring), and a
monotonic advert counter is genuinely useful (rate via HA derivative).
"""

import asyncio
import base64
import logging
from typing import Awaitable, Callable, Optional

from zeroconf import ServiceInfo

import em_db as db
from esphome.satellite_server import SatelliteServerProtocol, serve, _HANDLED
from esphome.feature_flags import BluetoothProxyFeature
from esphome.vendor import api_pb2

import em_ble_gatt
import em_ble_health
import em_tasks

log = logging.getLogger("echomuse.bleproxy")

BT_PROXY_FLAGS = int(
    BluetoothProxyFeature.PASSIVE_SCAN
    | BluetoothProxyFeature.RAW_ADVERTISEMENTS
)

# With connections on. PAIRING is deliberately absent: the Echo does not pair.
BT_PROXY_FLAGS_ACTIVE = int(
    BT_PROXY_FLAGS
    | BluetoothProxyFeature.ACTIVE_CONNECTIONS
    | BluetoothProxyFeature.REMOTE_CACHING
    | BluetoothProxyFeature.CACHE_CLEARING
)

NOISE_PROTOCOL = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"

ADVERTS_SENSOR_KEY = 1


# ─── Satellite (one per active HA connection) ────────────────────────────────

class BluetoothProxySatellite(SatelliteServerProtocol):
    """ESPHome native API endpoint for one device's BT proxy."""

    def __init__(self, device_id: str, label: str, mac_address: str,
                 on_disconnected_cb, owning_server) -> None:
        super().__init__(
            server_name=f"echomuse-{device_id[-12:].lower()}-bt",
            log_name=f"bleproxy.{device_id[-8:]}",
        )
        self.device_id     = device_id
        self.label         = label
        self.mac_address   = mac_address
        self._owning_server = owning_server
        self._disconnected_hook = on_disconnected_cb
        # Set by SubscribeBluetoothLEAdvertisementsRequest; forward_adverts
        # only encodes/sends while HA is actually subscribed.
        self.subscribed = False
        self._states_subscribed = False
        # Connections: an encrypted listener and the Bluetooth handlers, or
        # neither. Decided by the server when this connection is accepted.
        self._gatt: Optional[em_ble_gatt.GattProxy] = None
        if owning_server.connections:
            self.set_encryption(owning_server.key, self.server_name,
                                mac_address.replace(":", "").lower())
            self._gatt = em_ble_gatt.GattProxy(
                owning_server.link, api_pb2, self._send_many, em_tasks.spawn,
                log_name=self._log_name)

    def close_gatt(self) -> None:
        if self._gatt is not None:
            self._gatt.close()
            self._gatt = None

    def handle_message(self, msg):
        if self._gatt is not None and self._gatt.handle(msg):
            yield _HANDLED
            return

        if isinstance(msg, api_pb2.DeviceInfoRequest):
            yield api_pb2.DeviceInfoResponse(
                uses_password=False,
                name=self.server_name,
                friendly_name=f"{self.label} BT Proxy",
                mac_address=self.mac_address,
                manufacturer="EchoMuse",
                model=_device_model(),
                # Dot required — HA splits project_name on "." (see
                # em_esphome.EchoMuseSatellite.handle_message), and shows
                # the part after it as the device Model.
                project_name=f"EchoMuse.{_device_model()}",
                project_version=_project_version(),
                bluetooth_proxy_feature_flags=(
                    BT_PROXY_FLAGS_ACTIVE if self._gatt is not None else BT_PROXY_FLAGS),
                api_encryption_supported=self._gatt is not None,
            )
            return

        if isinstance(msg, api_pb2.ListEntitiesRequest):
            yield api_pb2.ListEntitiesSensorResponse(
                object_id="ble_advertisements",
                key=ADVERTS_SENSOR_KEY,
                name="BLE advertisements",
                accuracy_decimals=0,
                state_class=api_pb2.STATE_CLASS_TOTAL_INCREASING,
                entity_category=api_pb2.ENTITY_CATEGORY_DIAGNOSTIC,
            )
            yield api_pb2.ListEntitiesDoneResponse()
            return

        if isinstance(msg, (api_pb2.SubscribeStatesRequest,
                            api_pb2.SubscribeHomeAssistantStatesRequest)):
            self._states_subscribed = True
            yield api_pb2.SensorStateResponse(
                key=ADVERTS_SENSOR_KEY,
                state=float(self._owning_server.adverts_seen),
            )
            return

        if isinstance(msg, api_pb2.SubscribeBluetoothLEAdvertisementsRequest):
            log.info(f"[{self._log_name}] HA subscribed to BLE advertisements "
                     f"(flags={msg.flags})")
            self.subscribed = True
            yield _HANDLED
            return

        if isinstance(msg, api_pb2.UnsubscribeBluetoothLEAdvertisementsRequest):
            log.info(f"[{self._log_name}] HA unsubscribed from BLE advertisements")
            self.subscribed = False
            yield _HANDLED
            return

        if isinstance(msg, api_pb2.SubscribeBluetoothConnectionsFreeRequest):
            # Passive proxy: no GATT connection slots.
            yield api_pb2.BluetoothConnectionsFreeResponse(free=0, limit=0)
            return

        if isinstance(msg, api_pb2.SubscribeHomeassistantServicesRequest):
            yield _HANDLED
            return

    def push_adverts_sensor(self, total: int) -> None:
        if self._states_subscribed:
            self._send_one(api_pb2.SensorStateResponse(
                key=ADVERTS_SENSOR_KEY, state=float(total)))


# ─── Per-device server ───────────────────────────────────────────────────────

class DeviceBleProxyServer:
    """TCP listener + mDNS identity for one device's BT proxy (single-claimant)."""

    def __init__(self, device_id: str, label: str, mac_address: str, port: int,
                 key: Optional[bytes] = None) -> None:
        self.device_id   = device_id
        self.label       = label
        self.mac_address = mac_address
        self.port        = port
        # Connections are on exactly when there is a key: the listener is
        # then encrypted and the Bluetooth handlers exist.
        self.key         = key
        self.connections = key is not None
        self.link        = em_ble_gatt.GattLink(self._send_gatt)
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_satellite: Optional[BluetoothProxySatellite] = None
        self._mdns_info: Optional[ServiceInfo] = None
        # Forwarding counters (controller-side view, dashboard diagnostics).
        self.adverts_received = 0   # batches' adverts arriving from the device
        self.adverts_forwarded = 0  # actually sent to a subscribed HA
        self.adverts_seen = 0       # device-reported cumulative counter (stats)
        # Baseline so seen and forwarded count from the same instant (#410).
        self.seen_base: Optional[int] = None
        # Transport health, device-reported and cumulative since ITS process
        # start. Kept so update_stats can warn on a RISE rather than on a
        # non-zero value — the counters never fall on their own, so warning
        # on non-zero would repeat the same reset every 30s forever.
        self.hci_restarts = 0
        self.hci_errors   = 0

    async def _send_gatt(self, payload: bytes) -> bool:
        sender = _gatt_sender
        return sender is not None and await sender(self.device_id, payload)

    def _protocol_factory(self):
        if self._active_satellite is not None:
            log.warning(f"[bleproxy.{self.device_id[-8:]}] Second connection "
                        f"attempt — rejecting (single-claimant)")
            from em_esphome import _RejectProtocol
            return _RejectProtocol()
        satellite = BluetoothProxySatellite(
            device_id=self.device_id,
            label=self.label,
            mac_address=self.mac_address,
            on_disconnected_cb=self._on_satellite_disconnected,
            owning_server=self,
        )
        self._active_satellite = satellite
        log.info(f"[bleproxy.{self.device_id[-8:]}] HA connected on port {self.port}")
        return satellite

    def _on_satellite_disconnected(self, satellite) -> None:
        # Its links go with it: nothing is left to use them.
        satellite.close_gatt()
        if self._active_satellite is satellite:
            self._active_satellite = None
            log.info(f"[bleproxy.{self.device_id[-8:]}] HA disconnected")

    def get_satellite(self) -> Optional[BluetoothProxySatellite]:
        return self._active_satellite

    async def start(self, host: str) -> None:
        if self._server is not None:
            return
        self._server = await serve(self._protocol_factory, host, self.port)
        log.info(f"[bleproxy.{self.device_id[-8:]}] Listening on {host}:{self.port}")

    async def stop(self) -> None:
        # Same teardown discipline as DeviceESPhomeServer.stop(): detach
        # before awaiting, and close the satellite so Python 3.12's
        # wait_closed() (which waits for accepted connections too) returns.
        if self._server is None and self._active_satellite is None:
            return  # reconcile() calls stop() freely — quiet no-op
        server, self._server = self._server, None
        satellite, self._active_satellite = self._active_satellite, None
        if satellite is not None:
            satellite.close_gatt()
            satellite.close()
        if server:
            server.close()
            await server.wait_closed()
        log.info(f"[bleproxy.{self.device_id[-8:]}] Server stopped")


# ─── Fleet state ─────────────────────────────────────────────────────────────

_proxies: dict[str, DeviceBleProxyServer] = {}
_online: set[str] = set()   # device_ids with a live /control connection
_can_connect: set[str] = set()   # of those, the ones announcing `ble_connect`
_host: str = "0.0.0.0"
# Writes one connection-bridge message to a device's data plane. Set by
# em_controller, which owns the sockets; this module must not import it.
_gatt_sender: Optional[Callable[[str, bytes], Awaitable[bool]]] = None


def set_gatt_sender(sender: Callable[[str, bytes], Awaitable[bool]]) -> None:
    global _gatt_sender
    _gatt_sender = sender


def _project_version() -> str:
    from em_esphome import ESPHOME_PROJECT_VERSION
    return ESPHOME_PROJECT_VERSION


def _device_model() -> str:
    from em_esphome import ESPHOME_DEVICE_MODEL
    return ESPHOME_DEVICE_MODEL


def _proxy_mac(device_id: str) -> str:
    """
    Stable, distinct MAC identity for the BT proxy: the voice satellite's
    serial-derived MAC with the locally-administered bit flipped (XOR 0x02
    on the first octet). XOR guarantees it differs from the voice MAC that
    HA keys the satellite device on.
    """
    from em_esphome import _serialno_to_mac
    mac = _serialno_to_mac(device_id)
    first = int(mac[0:2], 16) ^ 0x02
    return f"{first:02X}{mac[2:]}"


def _make_mdns_info(device_id: str, label: str, port: int,
                    encrypted: bool = False) -> ServiceInfo:
    from em_esphome import SERVER_IP
    import socket
    svc_name = f"echomuse-{device_id[-12:].lower()}-bt"
    extra = {"api_encryption": NOISE_PROTOCOL} if encrypted else {}
    return ServiceInfo(
        "_esphomelib._tcp.local.",
        f"{svc_name}._esphomelib._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=port,
        properties={
            **extra,
            "version": _project_version(),
            "friendly_name": f"{label} BT Proxy",
            # mac TXT is MANDATORY for HA discovery (mdns_missing_mac) and
            # must match DeviceInfoResponse.mac_address — see
            # em_esphome._make_device_mdns_info.
            "mac": _proxy_mac(device_id).replace(":", "").lower(),
            "network": "ethwifi",
            "project_name": f"EchoMuse.{_device_model()}",
            "project_version": _project_version(),
        },
        server=f"{svc_name}.local.",
    )


def _azc():
    from em_esphome import _azc as azc
    return azc


# ─── Lifecycle ───────────────────────────────────────────────────────────────

async def reconcile(device_id: str) -> None:
    """
    Bring this device's BT proxy in line with its effective config.

    The single lifecycle entry point: creates the proxy server + mDNS entry
    (allocating a port on first enable), starts/stops the listener based on
    device online state, and tears everything down when disabled. Idempotent.
    """
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    enabled = False
    wants_connections = False
    label = device_id[-8:]
    if row is not None and row["approved"]:
        label = row["label"] or f"EchoMuse {device_id[-8:]}"
        cfg = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
        enabled = bool(cfg.get("bleProxyEnabled", False))
        wants_connections = enabled and bool(cfg.get("bleProxyConnections", False))

    proxy = _proxies.get(device_id)

    if not enabled:
        if proxy is not None:
            await _teardown(device_id, proxy)
        return

    # Connections need firmware that can make them. A device that is offline
    # keeps what it last had, so its proxy does not flip to plaintext and
    # back every time it reconnects: Home Assistant would be asked for the
    # key again each time.
    if device_id in _online:
        connections = wants_connections and device_id in _can_connect
    else:
        connections = wants_connections and (proxy is None or proxy.connections)
    if proxy is not None and proxy.connections != connections:
        # Encryption and the feature flags are fixed when a listener is
        # made, and Home Assistant reads the flags once per connection.
        log.info(f"[{device_id}] BT proxy connections "
                 f"{'on' if connections else 'off'} — rebuilding the proxy")
        await _teardown(device_id, proxy)
        proxy = None

    if proxy is None:
        # BLE port is the voice satellite port + offset (paired, deterministic).
        port = await loop.run_in_executor(None, db.ensure_ble_proxy_port, device_id)
        if port is None:
            log.warning(f"[{device_id}] BLE proxy enabled but device has no "
                        f"ESPHome voice port yet — deferring until it does")
            return
        key = None
        if connections:
            stored = await loop.run_in_executor(None, db.ensure_ble_proxy_key, device_id)
            key = base64.b64decode(stored) if stored else None
            if key is None or len(key) != 32:
                log.error(f"[{device_id}] no usable BT proxy key — connections stay off")
                key = None
        proxy = DeviceBleProxyServer(device_id, label, _proxy_mac(device_id), port, key)
        _proxies[device_id] = proxy
        azc = _azc()
        if azc is not None:
            mdns_info = _make_mdns_info(device_id, label, port, encrypted=proxy.connections)
            try:
                await azc.async_register_service(mdns_info, allow_name_change=True)
                proxy._mdns_info = mdns_info
                log.info(f"[{device_id}] BT proxy mDNS registered: "
                         f"echomuse-{device_id[-12:].lower()}-bt → port {port}")
            except Exception as e:
                log.warning(f"[{device_id}] BT proxy mDNS registration failed: {e}")

    # Listener tracks device presence, same as the voice satellite's port.
    if device_id in _online:
        await proxy.start(_host)
        if proxy.connections and proxy.link.limit == 0:
            # A proxy made just now has an empty link, and the device's own
            # report of its slots was addressed to the one this replaced (it
            # sends it the moment the setting reaches it, which is before the
            # rebuild). Without asking again Home Assistant is told 0 of 0
            # and never routes a connection here — found on the first real
            # run, 2026-10-03.
            em_tasks.spawn(sync_slots(device_id))
    else:
        await proxy.stop()


async def _teardown(device_id: str, proxy: DeviceBleProxyServer) -> None:
    _proxies.pop(device_id, None)
    azc = _azc()
    if proxy._mdns_info is not None and azc is not None:
        try:
            await azc.async_unregister_service(proxy._mdns_info)
        except Exception:
            pass
        proxy._mdns_info = None
    await proxy.stop()
    log.info(f"[{device_id}] BT proxy disabled — listener + mDNS removed")


async def start_ble_proxy_servers(host: str = "0.0.0.0") -> None:
    """Reconcile every approved device at controller startup.

    Call AFTER em_esphome.start_esphome_servers() — reuses its AsyncZeroconf.
    """
    global _host
    _host = host
    loop = asyncio.get_event_loop()
    all_devices = await loop.run_in_executor(None, db.get_all_devices)
    for row in all_devices:
        if row["approved"]:
            await reconcile(row["device_id"])
    log.info(f"BLE proxy servers ready ({len(_proxies)} enabled)")


async def stop_ble_proxy_servers() -> None:
    for device_id, proxy in list(_proxies.items()):
        await _teardown(device_id, proxy)
    _proxies.clear()


async def device_connected(device_id: str, ble_connect: bool = False) -> None:
    """Called by em_controller when the physical device connects."""
    _online.add(device_id)
    if ble_connect:
        _can_connect.add(device_id)
    else:
        _can_connect.discard(device_id)
    proxy = _proxies.get(device_id)
    if proxy is not None:
        await proxy.start(_host)


async def device_disconnected(device_id: str) -> None:
    """Called by em_controller when the physical device disconnects."""
    _online.discard(device_id)
    _can_connect.discard(device_id)
    proxy = _proxies.get(device_id)
    if proxy is not None:
        # The device drops its links when the controller goes; say so to
        # Home Assistant before the listener closes under it.
        proxy.link.reset()
        await proxy.stop()


def gatt_from_device(device_id: str, payload: bytes) -> None:
    """One connection-bridge message off a device's data plane."""
    proxy = _proxies.get(device_id)
    if proxy is not None and proxy.connections:
        proxy.link.feed(payload)


async def sync_slots(device_id: str) -> None:
    """Ask a newly connected device what it holds, so the slot count is its own."""
    proxy = _proxies.get(device_id)
    if proxy is None or not proxy.connections:
        return
    try:
        await proxy.link.request("slots", timeout=10.0)
    except em_ble_gatt.GattError as e:
        # An Echo that is not connected yet is the ordinary case at startup.
        level = logging.DEBUG if e.code == "not_running" else logging.INFO
        log.log(level, f"[bleproxy.{device_id[-8:]}] slot query: {e}")


# ─── Data path ───────────────────────────────────────────────────────────────

def forward_adverts(device_id: str, adverts: list) -> None:
    """
    Forward one ble_adverts batch from the device to the subscribed HA
    connection. adverts: [{"addr": "aa:bb:..", "addrType": 0, "rssi": -62,
    "data": "<base64>"}] — the device's bluetooth.Advert JSON shape.
    """
    proxy = _proxies.get(device_id)
    if proxy is None:
        return
    proxy.adverts_received += len(adverts)
    satellite = proxy.get_satellite()
    if satellite is None or not satellite.subscribed:
        return

    import base64
    resp = api_pb2.BluetoothLERawAdvertisementsResponse()
    for a in adverts:
        try:
            adv = resp.advertisements.add()
            adv.address = int(a["addr"].replace(":", ""), 16)
            adv.rssi = int(a["rssi"])
            adv.address_type = int(a.get("addrType", 0))
            adv.data = base64.b64decode(a.get("data") or "")
        except (KeyError, ValueError, TypeError) as e:
            log.debug(f"[{device_id}] Malformed advert skipped: {e}")
            continue
    if not resp.advertisements:
        return
    satellite._send_one(resp)
    proxy.adverts_forwarded += len(resp.advertisements)


def update_stats(device_id: str, ble_stats: dict) -> None:
    """
    Called by em_controller when a device stats message carries a `ble`
    object. Pushes the adverts-seen counter to HA's diagnostic sensor.
    """
    proxy = _proxies.get(device_id)
    if proxy is None or not isinstance(ble_stats, dict):
        return
    raw = int(ble_stats.get("advertsSeen") or 0)
    rb = em_ble_health.rebase_seen(proxy.seen_base, proxy.adverts_seen, raw)
    proxy.seen_base, proxy.adverts_seen = rb.base, rb.last
    if rb.reset_forwarded:
        proxy.adverts_forwarded = 0

    # Transport resets. Worth a warning of their own because /dev/stpbt is
    # NOT a Bluetooth-only device: it is the MT8163's combo radio behind
    # MediaTek's WMT stack, shared with WiFi. See em_ble_health for the
    # mechanism, why the signal is a RISE rather than a value, and why a
    # counter going backwards rebases — the decision lives there as pure
    # logic so it is testable without importing zeroconf through this
    # module.
    obs = em_ble_health.observe(
        proxy.hci_restarts, proxy.hci_errors,
        ble_stats.get("restarts"), ble_stats.get("hciErrors"),
    )
    proxy.hci_restarts, proxy.hci_errors = obs.restarts, obs.errors
    if obs.warning:
        log.warning(f"[bleproxy.{device_id[-8:]}] {obs.warning}")

    satellite = proxy.get_satellite()
    if satellite is not None:
        satellite.push_adverts_sensor(proxy.adverts_seen)


def get_status(device_id: str) -> Optional[dict]:
    """Controller-side proxy state for the dashboard (None when disabled)."""
    proxy = _proxies.get(device_id)
    if proxy is None:
        return None
    satellite = proxy.get_satellite()
    return {
        "port":             proxy.port,
        "listening":        proxy._server is not None,
        "haConnected":      satellite is not None,
        "haSubscribed":     bool(satellite is not None and satellite.subscribed),
        "advertsReceived":  proxy.adverts_received,
        "advertsForwarded": proxy.adverts_forwarded,
        # Seen since the same instant as advertsForwarded (#410).
        "advertsSeen":      (proxy.adverts_seen - proxy.seen_base
                             if proxy.seen_base is not None else None),
        # Transport resets, surfaced because a non-zero value here is the
        # first thing to check against an unexplained link drop on this
        # device — see update_stats for why a BLE restart can take WiFi out.
        "hciRestarts":      proxy.hci_restarts,
        "hciErrors":        proxy.hci_errors,
        # Connections (#656). `encrypted` is what the port requires, which is
        # what the dashboard has to tell the operator about.
        "connections":      proxy.connections,
        "encrypted":        proxy.connections,
        "slotsFree":        proxy.link.free if proxy.connections else None,
        "slotsLimit":       proxy.link.limit if proxy.connections else None,
        "connected":        len(proxy.link.addrs) if proxy.connections else None,
    }
