"""
em_diagnose: each rule fires on the fault it names, and a healthy bundle
produces nothing. Fixtures are built through em_support.build, so a rule reads
the shape a real bundle has rather than one written for the test.
"""

import json
import sys
from pathlib import Path

import em_diagnose as D
import em_support as S

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import bundle_check  # noqa: E402

DID = "G090LF1180130NJG"
CAPS = ["mic", "speaker", "wake_cue", "volume_cue", "oww_local_only"]


def _view(**over):
    view = {
        "listen": {"state": "local", "streams": False, "reason": ""},
        "voiceSatellite": {"port": 6053, "listening": True, "haConnected": True},
        "linkQuality": {"verdict": "good", "lossPct": 0.2, "minutes": []},
        "firmware_update": False, "wifi": {"pending": None, "last_result": None},
    }
    view.update(over)
    return view


def _bundle(*, view=None, live=None, config=None, turns=(), metrics=(),
            counters=(), stats=None, device=None, snapshot=None):
    row = {"device_id": DID, "approved": 1, "firmware_ver": "v2.27.0"}
    row.update(device or {})
    lv = {"connected": True, "capabilities": CAPS}
    lv.update(live or {})
    built = S.build(
        controller_version="v2.27.0", devices=[row], fleet_config={},
        schema_version=33, turns=list(turns), metrics=list(metrics),
        counters=list(counters), device_configs={DID: config or {}},
        live_state={DID: lv}, controller_log=[], device_log=[],
        controller_stats=stats or {"data_free_mb": 40000, "mem_available_mb": 900,
                                   "mem_total_mb": 2000, "cpu_pct_5m": 8},
        views={DID: view if view is not None else _view()},
        snapshots={DID: snapshot or {"status": "unsupported"}})
    # Through JSON, as the tool reads it from a file.
    return json.loads(S.to_json(built))


def _rules(bundle):
    return [f["rule"] for f in D.check(bundle)]


def test_a_healthy_bundle_has_no_findings():
    turns = [{"device_id": DID, "outcome": "ok", "trigger_type": "wakeword",
              "underruns": 0}] * 10
    assert D.check(_bundle(turns=turns)) == []


def test_ha_not_connected_is_found():
    view = _view(voiceSatellite={"port": 6053, "listening": True, "haConnected": False})
    assert _rules(_bundle(view=view)) == ["ha_not_connected"]


def test_no_satellite_at_all_is_found():
    assert _rules(_bundle(view=_view(voiceSatellite=None))) == ["ha_not_connected"]


def test_a_degraded_echo_says_why():
    view = _view(listen={"state": "degraded", "streams": False, "reason": "no model"})
    (f,) = D.check(_bundle(view=view))
    assert f["rule"] == "listen_degraded" and f["evidence"]["reason"] == "no model"


def test_offline_skips_the_live_rules():
    view = _view(voiceSatellite=None)
    assert _rules(_bundle(view=view, live={"connected": False})) == ["offline"]


def test_a_setting_without_its_capability_is_found():
    found = D.check(_bundle(config={"wakeMic": 2, "wakeSound": True}))
    assert [(f["rule"], f["evidence"]["key"]) for f in found] == [
        ("setting_ignored", "wakeMic")]


def test_a_setting_at_its_default_is_not_flagged():
    assert _rules(_bundle(config={"wakeMic": 0})) == []


def test_link_refused_and_poor_link():
    view = _view(linkRefused="token mismatch",
                 linkQuality={"verdict": "poor", "lossPct": 6.1})
    assert _rules(_bundle(view=view)) == ["link_refused", "link_poor"]


def test_update_and_wifi_failures():
    view = _view(update_error="transfer stalled", firmware_update=True,
                 wifi={"pending": None, "last_result": {"ok": False, "error": "auth", "at": 1}})
    assert _rules(_bundle(view=view)) == [
        "update_failed", "firmware_behind", "wifi_change_failed"]


def test_unplanned_boot_and_flash_wear():
    boot = {"boot_reason": "kernel panic", "emmc_rev": 8, "emmc_life_a": 10,
            "emmc_pre_eol": 1}
    assert _rules(_bundle(live={"boot": boot})) == ["unplanned_boot", "emmc_wear"]


def test_thermal_core_loss():
    metrics = [{"device_id": DID, "hour_ts": 0, "cores_online_min": 2, "cores_total": 4}]
    assert _rules(_bundle(metrics=metrics)) == ["thermal_cores"]


def test_rates_need_enough_turns():
    few = [{"device_id": DID, "outcome": "no_ha", "trigger_type": "wakeword"}] * 4
    assert _rules(_bundle(turns=few)) == []


def test_failing_turns_and_false_wakes():
    turns = ([{"device_id": DID, "outcome": "no_speech", "trigger_type": "wakeword"}] * 4
             + [{"device_id": DID, "outcome": "tts_error", "trigger_type": "button"}] * 2
             + [{"device_id": DID, "outcome": "ok", "trigger_type": "wakeword", "underruns": 3}] * 4)
    found = {f["rule"]: f for f in D.check(_bundle(turns=turns, config={"owwThreshold": 0.5}))}
    assert found["turns_failing"]["evidence"]["outcomes"] == {"tts_error": 2}
    assert found["false_wakes"]["text"] == "4 of 8 wakes heard no command"
    assert found["reply_underruns"]["level"] == D.SUGGESTS


def test_near_misses():
    counters = [{"device_id": DID, "hour_ts": h, "near_misses": 4, "near_miss_max": 0.41}
                for h in range(3)]
    (f,) = D.check(_bundle(counters=counters, config={"owwThreshold": 0.5}))
    assert f["rule"] == "near_misses" and f["evidence"]["highest"] == 0.41


def test_controller_resources():
    stats = {"data_free_mb": 0, "mem_available_mb": 50, "mem_total_mb": 2000,
             "cpu_pct_5m": 97}
    assert _rules(_bundle(stats=stats)) == ["disk_low", "memory_low", "cpu_busy"]


def test_facts_come_before_suggestions():
    counters = [{"device_id": DID, "hour_ts": 0, "near_misses": 20, "near_miss_max": 0.4}]
    view = _view(linkRefused="token mismatch")
    levels = [f["level"] for f in D.check(_bundle(view=view, counters=counters))]
    assert levels == [D.FACT, D.SUGGESTS]


def test_capability_names_are_ones_the_device_announces():
    # A misspelt capability would flag the setting on every Echo.
    src = (CONTROLLER.parent / "device/internal/client/control.go").read_text()
    for cap in set(D.NEEDS_CAPABILITY.values()):
        assert f'"{cap}"' in src, cap


def test_every_listed_key_is_a_config_key():
    import em_db
    assert set(D.NEEDS_CAPABILITY) <= set(em_db.DEFAULT_DEVICE_CONFIG)


def test_the_tool_reads_a_file(tmp_path, capsys):
    path = tmp_path / "b.json"
    path.write_text(json.dumps(_bundle(view=_view(voiceSatellite=None))))
    assert bundle_check.main([str(path)]) == 0
    assert "[fact] device-1: Home Assistant is not connected" in capsys.readouterr().out


def test_the_tool_warns_on_an_old_format(tmp_path, capsys):
    b = _bundle()
    b["format"] = 2
    path = tmp_path / "b.json"
    path.write_text(json.dumps(b))
    bundle_check.main([str(path)])
    assert "format 2" in capsys.readouterr().err


def _snap(**over):
    snap = {"board": {"id": "biscuit", "notes": [], "problems": []},
            "mixer": [{"name": "DIF1 Switch", "want": "1", "got": "On", "match": True},
                      {"name": "Amp Switch", "got": "On"}],
            "kernel": ["mt-snd-card: probe ok"]}
    snap.update(over)
    return {"status": "ok", "snapshot": snap}


def test_a_healthy_snapshot_has_no_findings():
    assert D.check(_bundle(snapshot=_snap())) == []


def test_a_route_that_did_not_take_is_found():
    # #546: every write succeeded, and the reads show they landed elsewhere.
    snap = _snap(mixer=[{"name": "DIF1 Switch", "want": "On", "got": "Off", "match": False},
                        {"name": "HPR Switch", "err": "no control named"}])
    found = {f["rule"]: f for f in D.check(_bundle(snapshot=snap))}
    assert found["mixer_mismatch"]["evidence"]["controls"] == ["DIF1 Switch: Off (writes On)"]
    assert found["mixer_missing"]["evidence"]["controls"] == ["HPR Switch"]


def test_board_problems_and_deferred_probes():
    snap = _snap(board={"id": "biscuit", "problems": ["keys: by number"]},
                 kernel=["tas2770 3-004c: probe deferred"])
    assert _rules(_bundle(snapshot=snap)) == ["board_parts", "driver_deferred"]
