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


def test_wear_values_keep_only_well_typed_readings():
    e = {"rev": 7, "preEol": 1, "lifeA": 1, "lifeB": 2, "name": "FJ25AB", "date": "08/2017"}
    assert em_health.wear_values(e) == (7, 1, 1, 2, "FJ25AB", "08/2017", None)
    assert em_health.wear_values({"rev": "7", "lifeA": True, "name": ""}) is None
    assert em_health.wear_values(None) is None
    assert em_health.wear_values({"rev": 7, "lifeA": False}) == (7, None, None, None, None, None, None)


def _db(tmp_path):
    em_db.init(str(tmp_path / "em.db"))
    em_db._conn.execute("INSERT INTO devices (device_id, label, approved) VALUES ('D', 'x', 1)")
    em_db._conn.commit()


def _count(table):
    return em_db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_one_boot_row_per_boot_keeping_the_first(tmp_path):
    _db(tmp_path)
    em_db.record_boot("D", "boot-1", "v2.17.0", "power_key")
    em_db.record_boot("D", "boot-1", "v2.17.0", "wdt_by_pass_pwk")  # a redial
    assert _count("device_boots") == 1
    assert em_db.latest_health()["D"]["boot_reason"] == "power_key"
    em_db.record_boot("D", "boot-2", None, "")
    assert _count("device_boots") == 2


# A device that never reboots must still build a history: wear is per DAY,
# and the latest reading of a day replaces the earlier one.
def test_wear_is_one_row_per_day_with_the_latest_reading(tmp_path):
    _db(tmp_path)
    em_db.record_wear("D", "2026-10-01", (7, 1, 1, 1, "FJ25AB", "08/2017", None))
    em_db.record_wear("D", "2026-10-01", (7, 1, 1, 2, "FJ25AB", "08/2017", None))
    em_db.record_wear("D", "2026-10-02", (7, 1, 2, 2, "FJ25AB", "08/2017", None))
    assert _count("device_wear") == 2
    rows = em_db._conn.execute(
        "SELECT day, emmc_life_a, emmc_life_b FROM device_wear ORDER BY day").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-01", 1, 2), ("2026-10-02", 2, 2)]
    h = em_db.latest_health()["D"]
    assert (h["day"], h["emmc_life_a"]) == ("2026-10-02", 2)
    assert em_health.emmc_summary(h) == {"text": "10–20% used · normal", "level": "ok"}


def test_health_merges_boot_and_wear(tmp_path):
    _db(tmp_path)
    em_db.record_boot("D", "boot-1", "v2.17.0", "power_key")
    em_db.record_wear("D", "2026-10-01", (7, 1, 1, 1, None, None, None))
    h = em_db.latest_health()["D"]
    assert h["boot_reason"] == "power_key" and h["emmc_rev"] == 7 and h["boot_at"]


def test_deleting_a_device_removes_its_boots_and_wear(tmp_path):
    _db(tmp_path)
    em_db.record_boot("D", "boot-1", None, "power_key")
    em_db.record_wear("D", "2026-10-01", (7, 1, 1, 1, None, None, None))
    em_db.delete_device("D")
    assert _count("device_boots") == 0 and _count("device_wear") == 0
