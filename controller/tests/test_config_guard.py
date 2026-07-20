"""
Guard against the config-clobber trap.

Config POSTs REPLACE the stored dict rather than merging. That is safe for
the dashboard, which always submits the complete config, and a trap for any
caller that submits a partial one. On 2026-07-20 a POST carrying the single
key wakeArbitrationMs reset all 26 fleet settings to defaults: the wake
model reverted hey_mycroft -> hey_jarvis (so the real wake word stopped
working), owwThreshold dropped 0.5 -> 0.3 (so devices false-woke on ordinary
conversation), and AEC, barge-in, NS, beamforming, the BLE proxy and the EQ
curve all switched off.

These tests exercise the pure key-set logic directly rather than standing up
an aiohttp app — em_api pulls in the whole controller stack, which this
suite deliberately keeps out. The handler wiring is a two-line call into
this function on each of the two write paths.
"""

import re
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[1]


def _load_dropped_keys():
    """
    Extract _dropped_keys from em_api source and exec it in isolation.
    Importing em_api would drag in aiohttp/openwakeword; the function is
    self-contained (stdlib only), so this keeps the test honest without
    the dependency weight.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    m = re.search(r"^def _dropped_keys\(.*?\n(?=\n\nasync def )", src,
                  re.S | re.M)
    assert m, "could not locate _dropped_keys in em_api.py"
    ns: dict = {}
    exec(m.group(0), ns)
    return ns["_dropped_keys"]


dropped_keys = _load_dropped_keys()

# The real fleet config as it stood before the incident.
LIVE_CONFIG = {
    "adcDigitalGain": 88, "adcMicpga": 40, "micGainDb": 24,
    "aecEnabled": True, "aecDelayMs": 0, "aecTailMs": 300,
    "startupVolume": 85, "vadThreshold": 0.001, "vadSpeechMs": 32,
    "vadSilenceMs": 900, "owwThreshold": 0.5, "bargeInEnabled": True,
    "bargeInThreshold": 0.05, "owwModel": "hey_mycroft_v0.1",
    "owwSpeexNs": False, "nsAsr": True, "bleProxyEnabled": True,
    "beamformingEnabled": True, "beamAngle": -1,
    "eqBands": [0, 3, 2, 0, -2, 3, 7, 0], "eqLoudness": True,
    "ledScene": "standard", "ledListenColor": "#00b400",
    "ledThinkColor": "#00c800", "agcEnabled": True, "nsEnabled": False,
}


def test_the_exact_body_that_caused_the_incident_is_caught():
    """The literal payload I sent on 2026-07-20."""
    dropped = dropped_keys({"wakeArbitrationMs": 700}, LIVE_CONFIG)
    assert len(dropped) == 26
    # The two that turned into audible symptoms.
    assert "owwModel" in dropped
    assert "owwThreshold" in dropped


def test_full_config_write_drops_nothing():
    """How the dashboard behaves — must stay a no-op."""
    body = dict(LIVE_CONFIG)
    body["wakeArbitrationMs"] = 700
    assert dropped_keys(body, LIVE_CONFIG) == []


def test_read_modify_write_is_the_safe_pattern():
    """The pattern the error message tells callers to use."""
    body = {**LIVE_CONFIG, "owwThreshold": 0.6}
    assert dropped_keys(body, LIVE_CONFIG) == []


def test_dropping_a_single_key_is_still_caught():
    body = {k: v for k, v in LIVE_CONFIG.items() if k != "aecEnabled"}
    assert dropped_keys(body, LIVE_CONFIG) == ["aecEnabled"]


def test_empty_stored_config_permits_anything():
    """First write on a fresh install has nothing to destroy."""
    assert dropped_keys({"owwThreshold": 0.5}, {}) == []


def test_result_is_sorted_for_a_stable_error_message():
    body = {"micGainDb": 24}
    out = dropped_keys(body, LIVE_CONFIG)
    assert out == sorted(out)


@pytest.mark.parametrize("path,handler", [
    ("global", "_post_global_config"),
    ("device", "_post_device_config"),
])
def test_both_write_paths_are_guarded(path, handler):
    """
    Both endpoints replace rather than merge, so both need the check. A
    guard on only one would leave the identical trap open next door.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    m = re.search(rf"async def {handler}\(.*?(?=\nasync def )", src, re.S)
    assert m, f"could not locate {handler}"
    body = m.group(0)
    assert "_dropped_keys(" in body, f"{handler} does not call _dropped_keys"
    assert "would_drop_keys" in body, f"{handler} does not refuse the write"


def test_new_default_key_does_not_block_a_stale_dashboard_save():
    """
    Regression for a false positive found while writing this guard.

    get_global_device_config() underlays DEFAULT_DEVICE_CONFIG, so if the
    guard compared against that view, a controller upgrade adding a new
    default (exactly what wakeArbitrationMs was) would make every save from
    an already-open dashboard tab look like a deletion of that key and be
    refused. Comparing against the RAW stored config — what an operator has
    actually persisted — keeps legitimate saves working while still
    catching a genuinely destructive partial write.
    """
    raw_stored = {k: v for k, v in LIVE_CONFIG.items()}   # no new key yet
    stale_dashboard_body = dict(raw_stored)               # lacks the new default
    assert dropped_keys(stale_dashboard_body, raw_stored) == []

    # ...while the destructive partial write is still refused.
    assert len(dropped_keys({"wakeArbitrationMs": 700}, raw_stored)) == 26


def test_guard_reads_raw_stored_config_not_the_underlaid_view():
    """The guard must call the raw accessor, or the false positive returns."""
    src = (CONTROLLER / "em_api.py").read_text()
    m = re.search(r"async def _post_global_config\(.*?(?=\nasync def )", src, re.S)
    assert m
    assert "get_global_device_config_raw" in m.group(0)
