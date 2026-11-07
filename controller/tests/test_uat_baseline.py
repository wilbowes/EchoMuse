"""tools/uat/baseline.py: every UAT starts from the shipped defaults plus a
short, reasoned list of exceptions, with every Echo following the fleet."""
import importlib.util
from pathlib import Path

import em_db
from em_config_sections import STATE_KEYS

_PATH = Path(__file__).resolve().parents[2] / "tools" / "uat" / "baseline.py"
_spec = importlib.util.spec_from_file_location("uat_baseline", _PATH)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

SPEC = baseline.load()


def test_exceptions_name_real_keys_and_change_something():
    for key, e in SPEC["exceptions"].items():
        assert key in em_db.DEFAULT_DEVICE_CONFIG, f"{key}: not a config key (renamed?)"
        assert e["value"] != em_db.DEFAULT_DEVICE_CONFIG[key], f"{key}: repeats the default"
        assert e["why"].strip(), f"{key}: an exception needs a reason"


def test_site_keys_are_never_in_the_baseline():
    want = baseline.desired(SPEC)
    for key in SPEC["site_keys"]["keys"]:
        assert key not in want
    assert set(want) == set(em_db.DEFAULT_DEVICE_CONFIG) - set(SPEC["site_keys"]["keys"])


def test_hash_follows_the_defaults():
    a = baseline.digest(baseline.desired(SPEC))
    changed = dict(em_db.DEFAULT_DEVICE_CONFIG, ledScene="something-else")
    assert baseline.digest(baseline.desired(SPEC, changed)) != a
    assert baseline.digest(baseline.desired(SPEC)) == a


class FakeController:
    def __init__(self, fleet, devices):
        self.fleet, self.devices, self.posts = dict(fleet), devices, []

    def __call__(self, method, path, body=None):
        if method == "POST":
            self.posts.append((path, body))
        if path == "/api/devices":
            return [{"device_id": d, "connected": True} for d in self.devices]
        if path == "/api/global/config":
            if method == "POST":
                self.fleet = dict(body)
            return {"config": self.fleet}
        dev = path.split("/")[3]
        if method == "POST":
            self.devices[dev]["config_sections"] = body["config_sections"]
        return self.devices[dev]


def _drifted():
    fleet = dict(baseline.desired(SPEC), aecRefSource="hw",
                 consolePassword="secret", controllerEndpoints=[{"host": "x"}])
    devices = {
        "G090AAAA0000AAAA": {"config_sections": [], "config": {"startupVolume": 50}},
        "G090BBBB0000BBBB": {"config_sections": ["playback"],
                             "config": {"startupVolume": 89, "eqBands": [1] * 8}},
    }
    return FakeController(fleet, devices)


def test_check_reports_fleet_drift_and_overrides():
    _, diffs = baseline.check(_drifted())
    assert diffs == ["fleet aecRefSource: 'hw', baseline 'auto'", "BBBB overrides playback"]


def test_apply_reaches_baseline_and_keeps_site_and_state_keys():
    api = _drifted()
    baseline.apply(api)
    assert baseline.check(api)[1] == []
    assert api.fleet["consolePassword"] == "secret"
    assert api.fleet["controllerEndpoints"] == [{"host": "x"}]
    # The clear re-sends state keys, or the controller refuses it as a drop.
    path, body = api.posts[-1]
    assert path.endswith("G090BBBB0000BBBB/config")
    assert body == {"config_sections": [], **{k: 89 for k in STATE_KEYS}}


def test_apply_writes_nothing_at_baseline():
    api = _drifted()
    baseline.apply(api)
    api.posts.clear()
    baseline.apply(api)
    assert api.posts == []
