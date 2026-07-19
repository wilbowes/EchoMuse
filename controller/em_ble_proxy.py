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

Entities: one diagnostic sensor (total adverts seen). HA's ESPHome
integration was observed to silently ignore zero-entity devices (see
esphome/feature_flags.py MediaPlayerEntityFeature docstring), and a
monotonic advert counter is genuinely useful (rate via HA derivative).
"""

import asyncio
import logging
from typing import Optional

from zeroconf import ServiceInfo

import em_db as db
from esphome.satellite_server import SatelliteServerProtocol, serve, _HANDLED
from esphome.feature_flags import BluetoothProxyFeature
from esphome.vendor import api_pb2

log = logging.getLogger("echomuse.bleproxy")

BT_PROXY_FLAGS = int(
    BluetoothProxyFeature.PASSIVE_SCAN
    | BluetoothProxyFeature.RAW_ADVERTISEMENTS
)

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

    def handle_message(self, msg):
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
                bluetooth_proxy_feature_flags=BT_PROXY_FLAGS,
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

    def __init__(self, device_id: str, label: str, mac_address: str, port: int) -> None:
        self.device_id   = device_id
        self.label       = label
        self.mac_address = mac_address
        self.port        = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_satellite: Optional[BluetoothProxySatellite] = None
        self._mdns_info: Optional[ServiceInfo] = None
        # Forwarding counters (controller-side view, dashboard diagnostics).
        self.adverts_received = 0   # batches' adverts arriving from the device
        self.adverts_forwarded = 0  # actually sent to a subscribed HA
        self.adverts_seen = 0       # device-reported cumulative counter (stats)

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
            satellite.close()
        if server:
            server.close()
            await server.wait_closed()
        log.info(f"[bleproxy.{self.device_id[-8:]}] Server stopped")


# ─── Fleet state ─────────────────────────────────────────────────────────────

_proxies: dict[str, DeviceBleProxyServer] = {}
_online: set[str] = set()   # device_ids with a live /control connection
_host: str = "0.0.0.0"


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


def _make_mdns_info(device_id: str, label: str, port: int) -> ServiceInfo:
    from em_esphome import SERVER_IP
    import socket
    svc_name = f"echomuse-{device_id[-12:].lower()}-bt"
    return ServiceInfo(
        "_esphomelib._tcp.local.",
        f"{svc_name}._esphomelib._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=port,
        properties={
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
    label = device_id[-8:]
    if row is not None and row["approved"]:
        label = row["label"] or f"EchoMuse {device_id[-8:]}"
        cfg = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
        enabled = bool(cfg.get("bleProxyEnabled", False))

    proxy = _proxies.get(device_id)

    if not enabled:
        if proxy is not None:
            await _teardown(device_id, proxy)
        return

    if proxy is None:
        # BLE port is the voice satellite port + offset (paired, deterministic).
        port = await loop.run_in_executor(None, db.ensure_ble_proxy_port, device_id)
        if port is None:
            log.warning(f"[{device_id}] BLE proxy enabled but device has no "
                        f"ESPHome voice port yet — deferring until it does")
            return
        proxy = DeviceBleProxyServer(device_id, label, _proxy_mac(device_id), port)
        _proxies[device_id] = proxy
        azc = _azc()
        if azc is not None:
            mdns_info = _make_mdns_info(device_id, label, port)
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


async def device_connected(device_id: str) -> None:
    """Called by em_controller when the physical device connects."""
    _online.add(device_id)
    proxy = _proxies.get(device_id)
    if proxy is not None:
        await proxy.start(_host)


async def device_disconnected(device_id: str) -> None:
    """Called by em_controller when the physical device disconnects."""
    _online.discard(device_id)
    proxy = _proxies.get(device_id)
    if proxy is not None:
        await proxy.stop()


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
    proxy.adverts_seen = int(ble_stats.get("advertsSeen") or 0)
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
    }
