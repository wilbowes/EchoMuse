"""
em_health and db.record_boot: eMMC wear (JEDEC JESD84-B51 EXT_CSD) and boot
reason, from the register message to a dashboard line. Written from the
spec's edges: every life-time value it defines, the reserved ones, and the
revision below which the fields mean nothing.
"""
import em_db
import em_health


def test_every_defined_life_estimate_reads_as_its_decile():
    assert em_health.life_label(1) == "0–10% used"
    assert em_health.life_label(2) == "10–20% used"
    assert em_health.life_label(10) == "90–100% used"
    assert em_health.life_label(11) == "past rated life"


def test_undefined_and_reserved_life_values_say_nothing():
    for v in (0, 12, 255, -1, None, True, "1"):
        assert em_health.life_label(v) is None, v


def _row(rev=7, eol=1, a=1, b=1):
    return {"emmc_rev": rev, "emmc_pre_eol": eol, "emmc_life_a": a, "emmc_life_b": b}


def test_a_healthy_part_reads_ok_and_uses_the_worse_estimate():
    s = em_health.emmc_summary(_row(a=1, b=2))
    assert s == {"text": "10–20% used · normal", "level": "ok"}


def test_wear_levels():
    assert em_health.emmc_summary(_row(a=9))["level"] == "warn"
    assert em_health.emmc_summary(_row(eol=2))["level"] == "warn"
    assert em_health.emmc_summary(_row(eol=3))["level"] == "error"
    assert em_health.emmc_summary(_row(b=11))["level"] == "error"


def test_below_rev_7_the_bytes_are_not_health():
    s = em_health.emmc_summary(_row(rev=6, eol=3, a=11, b=11))
    assert s == {"text": "not reported by this part", "level": None}


def test_nothing_reported_is_none_not_healthy():
    assert em_health.emmc_summary(None) is None
    assert em_health.emmc_summary({"emmc_rev": None}) is None
    assert em_health.emmc_summary(_row(eol=0, a=0, b=0)) == {"text": "unknown", "level": None}


def test_unplanned_boots_are_warnings():
    assert em_health.boot_summary("power_key")["level"] == "ok"
    for r in ("wdt_by_pass_pwk", "kernel_panic", "WDT", "thermal"):
        assert em_health.boot_summary(r)["level"] == "warn", r
    assert em_health.boot_summary("") is None
    assert em_health.boot_summary(None) is None


def _db(tmp_path):
    em_db.init(str(tmp_path / "em.db"))
    em_db._conn.execute("INSERT INTO devices (device_id, label, approved) VALUES ('D', 'x', 1)")
    em_db._conn.commit()


def test_one_row_per_boot_keeping_the_first_reading(tmp_path):
    _db(tmp_path)
    e = {"rev": 7, "preEol": 1, "lifeA": 1, "lifeB": 2, "name": "FJ25AB", "date": "08/2017"}
    em_db.record_boot("D", "boot-1", "v2.17.0", "power_key", e)
    em_db.record_boot("D", "boot-1", "v2.17.0", "power_key", {**e, "lifeB": 9})  # a redial
    b = em_db.latest_boot("D")
    assert (b["boot_reason"], b["emmc_life_b"], b["emmc_name"]) == ("power_key", 2, "FJ25AB")
    assert em_db._conn.execute("SELECT COUNT(*) FROM device_boots").fetchone()[0] == 1
    assert em_db.latest_boots()["D"]["boot_id"] == "boot-1"


def test_bad_or_missing_readings_store_as_null(tmp_path):
    _db(tmp_path)
    em_db.record_boot("D", "boot-1", None, "", {"rev": "7", "lifeA": True, "name": ""})
    b = em_db.latest_boot("D")
    assert b["boot_reason"] is None and b["emmc_rev"] is None
    assert b["emmc_life_a"] is None and b["emmc_name"] is None
    em_db.record_boot("D", "boot-2", None, None, None)
    assert em_db._conn.execute("SELECT COUNT(*) FROM device_boots").fetchone()[0] == 2


def test_deleting_a_device_removes_its_boots(tmp_path):
    _db(tmp_path)
    em_db.record_boot("D", "boot-1", None, "power_key", None)
    em_db.delete_device("D")
    assert em_db._conn.execute("SELECT COUNT(*) FROM device_boots").fetchone()[0] == 0
