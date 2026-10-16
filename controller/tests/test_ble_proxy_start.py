"""A second Bluetooth proxy start must not bind, and must not escape."""

import asyncio
import errno

import em_ble_proxy as ble


def _reset():
    ble._proxies.clear()
    ble._online.clear()
    ble._can_connect.clear()
    ble._locks.clear()


def _patch(monkeypatch, serve):
    monkeypatch.setattr(ble, "serve", serve)
    monkeypatch.setattr(ble, "_azc", lambda: None)
    monkeypatch.setattr(ble.db, "get_device",
                        lambda _i: {"approved": 1, "label": "Dot-A"})
    monkeypatch.setattr(
        ble.db, "get_effective_device_config",
        lambda _i: {"bleProxyEnabled": True, "bleProxyConnections": False},
    )
    monkeypatch.setattr(ble.db, "ensure_ble_proxy_port", lambda _i: 16050)
    monkeypatch.setattr(ble.db, "get_esphome_mac",
                        lambda _i: "AA:BB:CC:DD:EE:FF")


def test_two_reconciles_bind_once(monkeypatch):
    binds = []

    async def serve(factory, host, port):
        await asyncio.sleep(0.05)
        binds.append(port)

        class Server:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        return Server()

    _patch(monkeypatch, serve)
    ble._online.add("DOTA0000000000AA")

    async def both():
        await asyncio.gather(
            ble.reconcile("DOTA0000000000AA"),
            ble.reconcile("DOTA0000000000AA"),
        )

    try:
        asyncio.run(both())
        assert binds == [16050]
    finally:
        _reset()


def test_address_in_use_does_not_escape(monkeypatch):
    async def serve(factory, host, port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    _patch(monkeypatch, serve)
    ble._online.add("DOTB0000000000BB")

    async def both():
        await ble.reconcile("DOTB0000000000BB")
        await ble.device_connected("DOTB0000000000BB", False)

    try:
        asyncio.run(both())
    finally:
        _reset()


def test_a_start_already_in_progress_does_not_bind_again(monkeypatch):
    binds = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def serve(factory, host, port):
        binds.append(port)
        started.set()
        await release.wait()

        class Server:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        return Server()

    _patch(monkeypatch, serve)

    async def race():
        ble._online.add("DOTC0000000000CC")
        first = asyncio.create_task(ble.reconcile("DOTC0000000000CC"))
        await asyncio.wait_for(started.wait(), 2)
        proxy = ble._proxies["DOTC0000000000CC"]
        await proxy.start("0.0.0.0")
        release.set()
        await first

    try:
        asyncio.run(race())
        assert binds == [16050]
    finally:
        _reset()
