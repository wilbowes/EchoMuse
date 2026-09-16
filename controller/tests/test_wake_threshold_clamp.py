"""
A wake threshold that cannot fire must not reach the database.

openwakeword's score is a sigmoid: it approaches 1.0 and never reaches it, and
both scorers compare with `>=` (em_controller's ctrl_hit, and the device's own
shadow.go). So a stored threshold of exactly 1.0 is a bar nothing clears. The
device scores perfectly and never wakes, which presents as one that has stopped
responding rather than as a value set too high.

The dashboard's Sensitivity slider could write 1.0 until #543 — its strictest
notch mapped to exactly that — so the value is reachable in the field.

Clamped at the DB write path rather than in the API handlers because all four
writers go through those two functions, and a per-handler copy is one that can
disagree with the others. A dashboard-side clamp would not have been enough:
the threshold the device runs on comes from the pushed config, which comes from
the database, so clamping the display leaves the device deaf and the screen
reassuring.
"""

import json
import sqlite3

import pytest

import em_db


def _db(tmp_path):
    p = str(tmp_path / "em.db")
    em_db.init(p)
    return p


def _stored_config(path, device_id):
    c = sqlite3.connect(path)
    try:
        row = c.execute("SELECT config FROM devices WHERE device_id = ?",
                        (device_id,)).fetchone()
        return json.loads(row[0])
    finally:
        c.close()


def test_the_ceiling_is_below_one():
    """
    The whole point. A ceiling of 1.0 would clamp to a value that still cannot
    fire, which is the bug wearing a guard.
    """
    assert em_db.OWW_THRESHOLD_MAX < 1.0
    assert em_db.OWW_THRESHOLD_MAX > 0.9, (
        "the ceiling must stay strict enough to be worth having — 0.9 was "
        "already the strictest usable setting")


@pytest.mark.parametrize("sent,expected", [
    (1.0, 0.975),      # what the old slider's strictest notch wrote
    (1.5, 0.975),      # anything above the ceiling
    (0.975, 0.975),    # the ceiling itself passes through
    (0.5, 0.5),        # the default is untouched
    (0.1, 0.1),        # the eager end is untouched
])
def test_a_device_write_is_clamped(tmp_path, sent, expected):
    path = _db(tmp_path)
    em_db.register_new_device("D1", "10.0.0.1", "v1")
    em_db.set_device_config("D1", {"owwThreshold": sent})
    assert _stored_config(path, "D1")["owwThreshold"] == expected


def test_the_fleet_write_is_clamped_too(tmp_path):
    """
    Both write paths, because a fleet value reaches every device that has not
    overridden the wakeword section — the blast radius is larger, not smaller.
    """
    _db(tmp_path)
    em_db.set_global_device_config({"owwThreshold": 1.0})
    assert em_db.get_global_device_config()["owwThreshold"] == 0.975


def test_the_callers_dict_is_not_edited_underneath_them(tmp_path):
    """
    Callers reuse the dict they pass — `{**current, **values}` in the API, and
    the register path's stored_config. Mutating it would make the clamp visible
    somewhere that never asked for it.
    """
    _db(tmp_path)
    em_db.register_new_device("D2", "10.0.0.1", "v1")
    sent = {"owwThreshold": 1.0}
    em_db.set_device_config("D2", sent)
    assert sent["owwThreshold"] == 1.0


@pytest.mark.parametrize("value", [None, "0.5", True, False, [0.5]])
def test_a_non_number_passes_through_untouched(tmp_path, value):
    """
    Storing a wrong TYPE is somebody else's bug, and inventing a value here
    would hide it. `bool` is the one worth naming: it is a subclass of int in
    Python, so True would otherwise clamp to a plausible-looking 0.975.
    """
    path = _db(tmp_path)
    em_db.register_new_device("D3", "10.0.0.1", "v1")
    em_db.set_device_config("D3", {"owwThreshold": value})
    assert _stored_config(path, "D3")["owwThreshold"] == value


def test_a_config_with_no_threshold_is_left_alone(tmp_path):
    """A partial write must not acquire a key it never had."""
    path = _db(tmp_path)
    em_db.register_new_device("D4", "10.0.0.1", "v1")
    em_db.set_device_config("D4", {"micGainDb": 24})
    assert "owwThreshold" not in _stored_config(path, "D4")
