import asyncio
import json
from types import SimpleNamespace

import pytest

from em_kiosk_arbitration import BROADCAST, PORT, KioskArbiter, wake_energy


@pytest.fixture
def controller():
    import _oww_stub
    _oww_stub.install()
    try:
        import em_controller
        yield em_controller
    finally:
        _oww_stub.uninstall()


class Transport:
    def __init__(self):
        self.packets = []

    def sendto(self, data, address):
        self.packets.append((json.loads(data), address))


def packet(peer, **fields):
    return json.dumps({"ks": "wake", "v": 1, "id": peer, **fields}).encode()


def claim(arbiter, peer, energy, phrase="Glados", nonce=1):
    arbiter.datagram_received(packet(peer, p=phrase, e=energy, n=nonce), ("192.0.2.2", PORT))


def test_noise_floor_is_gain_corrected_and_bounded():
    assert wake_energy(-42, 0.001, 6, 0) == 24
    assert wake_energy(-42, 1e-6, 6, 2) == 41
    assert wake_energy(None, 0.001, 0, 0) is None
    assert wake_energy(-42, 0, 0, 0) is None


@pytest.mark.parametrize("show_energy, winner", [(45, "show"), (15, "puck")])
def test_three_way_glados_arbitration(show_energy, winner):
    async def main():
        arbiter = KioskArbiter(window_ms=100)
        transport = Transport()
        arbiter.connection_made(transport)
        try:
            arbiter.datagram_received(packet("lenovo", hello=True), ("192.0.2.2", PORT))
            claim(arbiter, "lenovo", 21)
            claim(arbiter, "show", show_energy)
            claim(arbiter, "other", 120, phrase="Okay Nabu")
            result = await arbiter.contest("puck", "  GLaDOS ", 30)
            assert (result.device_id or result.peer_id) == winner
            packets = [(msg, address) for msg, address in transport.packets if "n" in msg]
            broadcasts = [msg for msg, address in packets if address == BROADCAST]
            assert len(broadcasts) == 3 and broadcasts[0] == broadcasts[1] == broadcasts[2]
            assert broadcasts[0]["p"] == "glados"
            assert broadcasts[0]["e"] == 30
            assert any(address == ("192.0.2.2", PORT) for _, address in packets)
        finally:
            arbiter.connection_lost(None)

    asyncio.run(main())


def test_two_local_pucks_and_missing_levels():
    async def main():
        arbiter = KioskArbiter(window_ms=100)
        transport = Transport()
        arbiter.connection_made(transport)
        try:
            results = await asyncio.gather(
                arbiter.contest("older-puck", "Glados", None),
                arbiter.contest("puck", "Glados", 24),
            )
            assert [result.device_id for result in results] == ["puck", "puck"]
            claims = [msg for msg, _ in transport.packets if "n" in msg]
            assert {msg["id"] for msg in claims} == {results[0].peer_id}
        finally:
            arbiter.connection_lost(None)

    asyncio.run(main())


def test_ties_use_wire_id_and_duplicate_keeps_first_arrival():
    async def main():
        arbiter = KioskArbiter(window_ms=100)
        arbiter.connection_made(Transport())
        try:
            claim(arbiter, "0", 20)
            claim(arbiter, "0", 15)
            result = await arbiter.contest("puck", "Glados", 19.98)
            assert result.peer_id == "0"
            assert result.energy == 20
        finally:
            arbiter.connection_lost(None)

    asyncio.run(main())


def test_solo_puck_ignores_invalid_claims():
    async def main():
        arbiter = KioskArbiter(window_ms=100)
        arbiter.connection_made(Transport())
        try:
            addr = ("192.0.2.2", PORT)
            arbiter.datagram_received(b"invalid", addr)
            arbiter.datagram_received(packet("p", p="Glados", n=1, e=float("nan")), addr)
            arbiter.datagram_received(packet("p", p="Glados", n=1, e=999), addr)
            arbiter.datagram_received(packet("p", p="Glados", n=True, e=55), addr)
            assert (await arbiter.contest("puck", "Glados", 10)).device_id == "puck"
        finally:
            arbiter.connection_lost(None)

    asyncio.run(main())


def test_disabled_controller_uses_existing_arbiter(monkeypatch, controller):

    async def main():
        device = SimpleNamespace(device_id="puck", wake_arb_ms=700, adc_gains=None,
                                 timer_alarm_ringing=False)
        monkeypatch.setattr(controller, "kiosk_arbiter", None)
        monkeypatch.setattr(controller.em_state, "devices", {"puck": device})
        assert not controller._should_arbitrate(device)
        assert await controller._claim_wake(device, None) == "puck"

    asyncio.run(main())


def test_single_puck_can_yield_to_kiosk_in_controller(monkeypatch, controller):

    async def main():
        arbiter = KioskArbiter(window_ms=100)
        arbiter.connection_made(Transport())
        device = SimpleNamespace(device_id="puck", wake_arb_ms=700, noise_floor=0.001,
                                 mic_gain_db=0)
        try:
            monkeypatch.setattr(controller, "kiosk_arbiter", arbiter)
            monkeypatch.setattr(controller.em_state, "devices", {"puck": device})
            monkeypatch.setattr(controller.em_dbwriter, "submit", lambda *args: None)
            satellite = SimpleNamespace(oww_model_info=SimpleNamespace(name="glados"))
            server = SimpleNamespace(get_satellite=lambda: satellite)
            monkeypatch.setattr(controller.esphome, "get_server", lambda _: server)
            assert controller._should_arbitrate(device)
            claim(arbiter, "show", 40)
            assert await controller._claim_wake(device, None, (-45, -30)) == "kiosk:show"
            device.wake_arb_ms = 0
            assert not controller._should_arbitrate(device)
        finally:
            arbiter.connection_lost(None)

    asyncio.run(main())


def test_udp_claim_reaches_controller_over_loopback(monkeypatch):
    import errno
    import socket

    import em_kiosk_arbitration

    async def main():
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            try:
                peer.bind(("127.0.0.1", 0))
            except PermissionError as err:
                if err.errno in (errno.EPERM, errno.EACCES):
                    pytest.skip("local UDP sockets are blocked")
                raise
            peer.setblocking(False)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            monkeypatch.setattr(em_kiosk_arbitration, "PORT", port)
            monkeypatch.setattr(em_kiosk_arbitration, "BROADCAST", peer.getsockname())

            arbiter = KioskArbiter(window_ms=100)
            assert await arbiter.start()
            try:
                hello = await asyncio.wait_for(loop.sock_recv(peer, 1024), 1)
                assert json.loads(hello)["hello"] is True
                await loop.sock_sendto(
                    peer, packet("lenovo", p="Glados", n=1, e=40), ("127.0.0.1", port),
                )
                result = await arbiter.contest("puck", "glados", 20)
                assert result.peer_id == "lenovo"
                message = await asyncio.wait_for(loop.sock_recv(peer, 1024), 1)
                assert json.loads(message)["p"] == "glados"
            finally:
                arbiter.close()
                await asyncio.sleep(0)

    asyncio.run(main())
