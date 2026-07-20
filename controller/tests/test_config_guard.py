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
    self-contained (stdlib only), so this keeps the test honest without the
    dependency weight.

    Uses AST rather than a regex: a regex over function boundaries broke the
    moment a neighbouring decorator moved, and — worse — silently widened to
    swallow decorated handlers. Decorators are deliberately NOT applied here,
    which is exactly why the separate decorator-placement tests below exist.
    """
    import ast
    src = (CONTROLLER / "em_api.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_dropped_keys":
            node.decorator_list = []
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {}
            exec(compile(ast.fix_missing_locations(mod), "<em_api>", "exec"), ns)
            return ns["_dropped_keys"]
    raise AssertionError("could not locate _dropped_keys in em_api.py")


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


# ── decorator-placement guards ────────────────────────────────────────────
#
# Inserting a helper immediately above an already-decorated handler silently
# steals its decorator: on 2026-07-20 `_dropped_keys` was written directly
# under `@auth.require_admin`, so the decorator bound to the helper instead
# and `_post_global_config` was left with NO admin requirement — an auth
# bypass on a config-write endpoint. It surfaced only as a 500 in live
# testing, because the helper was then called with two args while wrapped to
# take a request.
#
# The unit tests above could not catch it: they exec the extracted source
# text, which drops decorators entirely, so they were exercising a different
# function than production. These parse the real file instead.

def _ast_tree():
    import ast
    return ast.parse((CONTROLLER / "em_api.py").read_text())


def _decorators_of(name):
    import ast
    for n in ast.walk(_ast_tree()):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return [getattr(d, "id", getattr(d, "attr", "?")) for d in n.decorator_list]
    raise AssertionError(f"{name} not found in em_api.py")


def test_dropped_keys_is_a_plain_helper_not_a_route_handler():
    assert _decorators_of("_dropped_keys") == [], (
        "_dropped_keys has picked up a decorator — it is a pure helper. This "
        "means it was inserted directly beneath a decorated handler and stole "
        "that decorator."
    )


@pytest.mark.parametrize("handler", [
    "_post_global_config",
    "_post_device_config",
    "_post_upload_binary",
])
def test_mutating_handlers_still_require_admin(handler):
    """Any config/binary write must keep its auth decorator."""
    assert "require_admin" in _decorators_of(handler), (
        f"{handler} lost its @auth.require_admin — anyone could call it"
    )
