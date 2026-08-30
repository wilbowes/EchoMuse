"""
BLE transport resets, and why they are a link-drop diagnostic.

`/dev/stpbt` is not a Bluetooth-only device — it is the MT8163's combo radio
behind MediaTek's WMT stack, shared with WiFi. When a scan session fails the
scanner reopens it, and opening it triggers a BT function-on plus firmware
patch download on the chip carrying the WiFi link. Observed once on
2026-08-30: stpbt read failure, reopen 5s later, `network is unreachable` 2s
after that.

The device has counted `restarts`/`hciErrors` since the proxy shipped and
sent them in the stats message. update_stats read `advertsSeen` and dropped
both, so nothing anywhere could say how often this happens. These guard the
counting rule, which is the part that is easy to get wrong: the counters are
cumulative on the DEVICE, so the signal is a RISE, not a non-zero value.
"""

import logging

import pytest

import em_ble_proxy


@pytest.fixture()
def proxy(monkeypatch):
    p = em_ble_proxy.DeviceBleProxyServer(
        device_id="G090LF1180440C95", label="Test Echo 1",
        mac_address="90:f1:18:04:40:c9", port=6053,
    )
    monkeypatch.setitem(em_ble_proxy._proxies, p.device_id, p)
    return p


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_rise_warns_once_not_every_report(proxy, caplog):
    """
    The counters never fall on their own, so warning on "non-zero" would
    repeat the same reset every 30s forever and bury the next real one.
    """
    with caplog.at_level(logging.WARNING, logger="echomuse.bleproxy"):
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 1, "hciErrors": 1})
        first = len(_warnings(caplog))
        for _ in range(5):
            em_ble_proxy.update_stats(proxy.device_id,
                                      {"restarts": 1, "hciErrors": 1})
        assert first == 1, "a rise must warn"
        assert len(_warnings(caplog)) == 1, \
            "an unchanged counter must not re-warn on every stats report"


def test_a_second_rise_warns_again(proxy, caplog):
    with caplog.at_level(logging.WARNING, logger="echomuse.bleproxy"):
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 1, "hciErrors": 0})
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 1, "hciErrors": 0})
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 2, "hciErrors": 0})
    assert len(_warnings(caplog)) == 2


def test_a_clean_transport_never_warns(proxy, caplog):
    with caplog.at_level(logging.WARNING, logger="echomuse.bleproxy"):
        for _ in range(10):
            em_ble_proxy.update_stats(proxy.device_id,
                                      {"advertsSeen": 900, "restarts": 0,
                                       "hciErrors": 0})
    assert _warnings(caplog) == []


def test_a_device_restart_rebases_instead_of_going_negative(proxy, caplog):
    """
    A device reboot resets both counters to 0. Treating that as a negative
    delta and leaving the stored value high would silence the next genuine
    rise all the way back up to the old total — which is precisely the
    window after a reboot, when resets matter most.
    """
    with caplog.at_level(logging.WARNING, logger="echomuse.bleproxy"):
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 9, "hciErrors": 9})
        before = len(_warnings(caplog))
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 0, "hciErrors": 0})
        assert len(_warnings(caplog)) == before, \
            "a rebase is not a reset event and must not warn"
        assert proxy.hci_restarts == 0, "counters must rebase to the new baseline"
        em_ble_proxy.update_stats(proxy.device_id, {"restarts": 1, "hciErrors": 0})
        assert len(_warnings(caplog)) == before + 1, \
            "the first rise after a reboot must still warn"


def test_status_surfaces_the_counters(proxy):
    em_ble_proxy.update_stats(proxy.device_id, {"restarts": 4, "hciErrors": 6})
    st = em_ble_proxy.get_status(proxy.device_id)
    assert st["hciRestarts"] == 4
    assert st["hciErrors"] == 6


def test_a_missing_ble_object_is_not_an_error(proxy):
    """Firmware predating the counters, and any non-dict, must be ignored."""
    em_ble_proxy.update_stats(proxy.device_id, {})
    em_ble_proxy.update_stats(proxy.device_id, None)
    em_ble_proxy.update_stats("unknown-device", {"restarts": 3})
    assert proxy.hci_restarts == 0
