"""
BLE transport resets, and why they are a link-drop diagnostic.

`/dev/stpbt` is not a Bluetooth-only device — it is the MT8163's combo radio
behind MediaTek's WMT stack, shared with WiFi. When a scan session fails the
scanner reopens it, and opening it triggers a BT function-on plus firmware
patch download on the chip carrying the WiFi link. Observed once on
2026-08-30: stpbt read failure, reopen 5s later, `network is unreachable` 2s
after that.

The device has counted `restarts`/`hciErrors` since the proxy shipped and
sent them in the stats message. `update_stats` read `advertsSeen` and
dropped both, so nothing anywhere could say how often this happens.

These test `em_ble_health` rather than reaching the decision through
`em_ble_proxy`, which imports zeroconf — a dependency the CI test job does
not install, so a test routed through that module passes locally and cannot
run where it matters. That is the same reason the logic was split out at
all; see controller/CLAUDE.md on keeping the suite to pure-logic modules.
"""

import re
from pathlib import Path

import em_ble_health as health

CONTROLLER = Path(__file__).resolve().parents[1]


def test_a_rise_warns():
    obs = health.observe(0, 0, 1, 1)
    assert obs.warning is not None
    assert (obs.restarts, obs.errors) == (1, 1)


def test_an_unchanged_counter_does_not_re_warn():
    """
    The counters never fall on their own, so warning on "non-zero" would
    repeat the same reset every 30s forever and bury the next real one.
    """
    obs = health.observe(1, 1, 1, 1)
    assert obs.warning is None
    assert (obs.restarts, obs.errors) == (1, 1)


def test_either_counter_rising_is_enough():
    assert health.observe(1, 0, 2, 0).warning is not None, "restarts alone"
    assert health.observe(1, 0, 1, 1).warning is not None, "errors alone"


def test_the_warning_names_the_delta_and_the_total():
    obs = health.observe(2, 3, 5, 9)
    assert "+3 restarts" in obs.warning
    assert "+6 errors" in obs.warning
    assert "5/9 total" in obs.warning


def test_a_clean_transport_never_warns():
    prev = (0, 0)
    for _ in range(10):
        obs = health.observe(prev[0], prev[1], 0, 0)
        assert obs.warning is None
        prev = (obs.restarts, obs.errors)


def test_a_device_restart_rebases_instead_of_going_negative():
    """
    A reboot resets both counters to 0. Treating that as a negative delta
    and keeping the old baseline would silence the next genuine rise all the
    way back up to the old total — precisely the window after a reboot, when
    resets matter most.
    """
    obs = health.observe(9, 9, 0, 0)
    assert obs.warning is None, "a rebase is not a reset event"
    assert (obs.restarts, obs.errors) == (0, 0), "must adopt the new baseline"

    after = health.observe(obs.restarts, obs.errors, 1, 0)
    assert after.warning is not None, "the first rise after a reboot must warn"


def test_missing_counters_are_treated_as_zero_not_as_a_crash():
    """Firmware predating the counters sends neither field."""
    obs = health.observe(0, 0, None, None)
    assert obs.warning is None
    assert (obs.restarts, obs.errors) == (0, 0)


# ── The wiring, asserted on source ───────────────────────────────────────────
#
# em_ble_proxy cannot be imported here (zeroconf), so the two facts that make
# the pure logic above reach anything are checked against the file.

def test_the_proxy_routes_its_counters_through_this_module():
    src = (CONTROLLER / "em_ble_proxy.py").read_text()
    assert "import em_ble_health" in src
    assert re.search(r"em_ble_health\.observe\(", src), \
        "update_stats must use the shared decision, not its own copy"


def test_the_status_payload_surfaces_the_counters():
    """
    A non-zero value here is the first thing to check against an unexplained
    link drop on that device, so it has to reach the dashboard.
    """
    src = (CONTROLLER / "em_ble_proxy.py").read_text()
    assert '"hciRestarts"' in src
    assert '"hciErrors"' in src
