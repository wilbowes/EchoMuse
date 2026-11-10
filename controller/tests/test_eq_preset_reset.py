"""
v32: firmware with the stock-sound profile corrects the driver itself (#229),
so the old Speech and Music presets, which were partial driver corrections,
would stack on it. They reset to Flat, and Speech boost, which lost its
control, to off. Anything else is left as it was.
"""

import json
import sqlite3

import em_db

SPEECH = [0, 4, 2, 0, -2, -5, 0, 0]
MUSIC = [0, 8, 3, 0, -3, -6, 0, 0]
CUSTOM = [0, 3, 2, 0, -2, 3, 7, 0]


def _at_v31(path, fleet_cfg, devices, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(em_db, "MIGRATIONS", em_db.MIGRATIONS[:31])
        em_db.init(path)
    c = sqlite3.connect(path)
    try:
        c.execute("UPDATE system_config SET value = ? WHERE key = 'global_device_config'",
                  (json.dumps(fleet_cfg),))
        for dev_id, cfg in devices.items():
            c.execute("INSERT INTO devices (device_id, config) VALUES (?, ?)",
                      (dev_id, cfg if isinstance(cfg, str) else json.dumps(cfg)))
        c.commit()
    finally:
        c.close()


def _read(path):
    c = sqlite3.connect(path)
    try:
        fleet = json.loads(c.execute("SELECT value FROM system_config "
                                     "WHERE key = 'global_device_config'").fetchone()[0])
        devs = {r[0]: r[1] for r in c.execute("SELECT device_id, config FROM devices")}
        return fleet, devs
    finally:
        c.close()


def test_old_presets_reset_and_custom_curves_kept(tmp_path, monkeypatch):
    p = str(tmp_path / "em.db")
    _at_v31(p, {"eqBands": [float(b) for b in MUSIC], "eqLoudness": True}, {
        "speech": {"eqBands": SPEECH, "duckDb": -12},
        "custom": {"eqBands": CUSTOM, "eqLoudness": False},
        "noeq":   {"duckDb": -6},
        "broken": "{not json",
    }, monkeypatch)
    em_db.init(p)
    fleet, devs = _read(p)
    assert fleet["eqBands"] == [0.0] * 8 and fleet["eqLoudness"] is False
    assert json.loads(devs["speech"]) == {"eqBands": [0.0] * 8, "duckDb": -12}
    assert json.loads(devs["custom"]) == {"eqBands": CUSTOM, "eqLoudness": False}
    assert json.loads(devs["noeq"]) == {"duckDb": -6}
    assert devs["broken"] == "{not json"


def test_a_near_miss_is_a_custom_curve(tmp_path, monkeypatch):
    p = str(tmp_path / "em.db")
    near = list(MUSIC)
    near[1] = 7
    _at_v31(p, {"eqBands": near}, {}, monkeypatch)
    em_db.init(p)
    assert _read(p)[0]["eqBands"] == near
