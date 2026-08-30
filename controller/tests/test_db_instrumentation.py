"""
Schema + writer tests for the v7 delivery instrumentation.

em_db is dependency-light (sqlite3/json/time only), so these run against a
real temporary database — migrations included. That matters more than usual
here: a migration that fails at container start takes the whole controller
down, and the fleet's live DB is the only copy of the activity history.
"""

import sqlite3

import pytest

import em_db as db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A real database migrated from scratch to the current schema."""
    path = tmp_path / "test.db"
    db.init(str(path))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _cols(table: str) -> set:
    return {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})")}


def test_migrates_to_v7(fresh_db):
    version = db.get_config("schema_version")
    assert int(version) >= 7


def test_turns_has_delivery_columns(fresh_db):
    cols = _cols("turns")
    for c in ("min_depth", "prime_wait_ms", "recv_span_ms", "max_gap_ms",
              "bytes_recv", "send_ms", "delivery_ms", "eq_ms"):
        assert c in cols, f"turns.{c} missing"


def test_device_metrics_has_link_columns(fresh_db):
    cols = _cols("device_metrics")
    for c in ("link_speed_last", "link_speed_min", "wifi_freq_last",
              "wifi_bssid_last", "tx_bytes_sum", "rx_bytes_sum",
              "tx_errors_sum", "tx_dropped_sum", "rx_crc_sum"):
        assert c in cols, f"device_metrics.{c} missing"


def _mk_turn(fresh_db, device_id="dev1") -> int:
    db.register_new_device(device_id, "1.2.3.4", "v2.9.6")
    return db.insert_turn(device_id, {
        "trigger": "wakeword(0.9)", "outcome": "ok", "tts_bytes": 96000,
    })


def test_set_turn_playback_stores_margin_fields(fresh_db):
    turn_id = _mk_turn(fresh_db)
    db.set_turn_playback(turn_id, periods=50, underruns=1, stats={
        "minDepth": 2, "primeWaitMs": 900, "recvSpanMs": 4200,
        "maxGapMs": 310, "bytesRecv": 204800,
    })
    row = db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["playback_periods"] == 50
    assert row["underruns"] == 1
    assert row["min_depth"] == 2
    assert row["prime_wait_ms"] == 900
    assert row["recv_span_ms"] == 4200
    assert row["max_gap_ms"] == 310
    assert row["bytes_recv"] == 204800


def test_set_turn_playback_without_stats_leaves_nulls(fresh_db):
    """Pre-v2.9.6 firmware reports only periods/underruns — the margin
    columns must read NULL ('never reported'), not 0 ('perfect')."""
    turn_id = _mk_turn(fresh_db)
    db.set_turn_playback(turn_id, periods=50, underruns=0)
    row = db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["playback_periods"] == 50
    assert row["min_depth"] is None
    assert row["recv_span_ms"] is None


def test_set_turn_delivery(fresh_db):
    turn_id = _mk_turn(fresh_db)
    db.set_turn_delivery(turn_id, send_ms=30, delivery_ms=8200, eq_ms=140)
    row = db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    # The whole point of the pair: a near-zero socket write next to a
    # multi-second delivery is the signature that misled 2026-07-20.
    assert row["send_ms"] == 30
    assert row["delivery_ms"] == 8200
    assert row["eq_ms"] == 140


def test_insert_turn_accepts_delivery_fields(fresh_db):
    """Stats-before-persist path: the fields ride the initial insert."""
    db.register_new_device("dev1", "1.2.3.4", "v2.9.6")
    turn_id = db.insert_turn("dev1", {
        "trigger": "wakeword(0.9)", "outcome": "ok",
        "min_depth": 0, "delivery_ms": 11200, "recv_span_ms": 9000,
    })
    row = db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["min_depth"] == 0
    assert row["delivery_ms"] == 11200
    assert row["recv_span_ms"] == 9000


def test_record_device_stats_accumulates_link_metrics(fresh_db):
    db.register_new_device("dev1", "1.2.3.4", "v2.9.6")
    db.record_device_stats("dev1", {
        "cpuPct": 20.0, "memUsedMb": 180, "wifiRssi": -55,
        "linkSpeedMbps": 135, "wifiFreqMhz": 5805, "wifiBssid": "aa:bb",
        "txBytes": 1000, "rxBytes": 2000, "txErrors": 1,
        "txDropped": 2, "rxCrcErrors": 3,
    })
    db.record_device_stats("dev1", {
        "cpuPct": 22.0, "memUsedMb": 181, "wifiRssi": -57,
        "linkSpeedMbps": 72, "wifiFreqMhz": 2412, "wifiBssid": "aa:cc",
        "txBytes": 500, "rxBytes": 700, "txErrors": 4,
        "txDropped": 0, "rxCrcErrors": 1,
    })
    row = db._q1("SELECT * FROM device_metrics WHERE device_id = 'dev1'")
    assert row["samples"] == 2
    assert row["tx_bytes_sum"] == 1500
    assert row["rx_bytes_sum"] == 2700
    assert row["tx_errors_sum"] == 5
    assert row["rx_crc_sum"] == 4
    # Latest identity wins (a band change should be visible)...
    assert row["link_speed_last"] == 72
    assert row["wifi_freq_last"] == 2412
    assert row["wifi_bssid_last"] == "aa:cc"
    # ...but the worst PHY rate is what a throughput hunt needs.
    assert row["link_speed_min"] == 72


def test_link_speed_absent_does_not_poison_minimum(fresh_db):
    """linkSpeedMbps is omitempty and refreshed on a slower cadence, so a
    tick without it must not record a 0 Mbps minimum."""
    db.register_new_device("dev1", "1.2.3.4", "v2.9.6")
    db.record_device_stats("dev1", {"cpuPct": 5.0, "memUsedMb": 100,
                                    "linkSpeedMbps": 150})
    db.record_device_stats("dev1", {"cpuPct": 5.0, "memUsedMb": 100})
    row = db._q1("SELECT * FROM device_metrics WHERE device_id = 'dev1'")
    assert row["link_speed_min"] == 150
    assert row["link_speed_last"] == 150


def test_stats_relay_allowlist_covers_every_device_stat():
    """
    em_controller's stats handler copies device stats into device.stats via
    an explicit allowlist before record_device_stats sees them. A field can
    therefore be present on the device AND handled by the DB writer and
    still be silently dropped in between — which is exactly what happened
    on 2026-07-20, caught only by watching a live OTA'd device report nulls.

    Guard: every key record_device_stats reads must appear in the handler's
    allowlist. Parses the source rather than importing em_controller, which
    pulls in openwakeword/aiohttp and has no place in this suite.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    ctrl = (root / "em_controller.py").read_text()
    dbsrc = (root / "em_db.py").read_text()

    body = re.search(r'msg_type == "stats":(.*?)\n\s*if msg\.get\("ble"\)',
                     ctrl, re.S)
    assert body, "could not locate the stats handler allowlist"
    allowlist = set(re.findall(r'"(\w+)":\s*msg\.get', body.group(1)))

    # RTT is measured controller-side rather than relayed from the device
    # message, so it reaches record_device_stats through Device.drain_rtt()
    # merged into the same dict. Same failure mode, different source: a key
    # the DB writer reads but drain_rtt never emits is silently always NULL.
    drain = re.search(r"def drain_rtt\(.*?\n(?=    def |\n\n)", ctrl, re.S)
    assert drain, "could not locate Device.drain_rtt"
    allowlist |= set(re.findall(r'"(\w+)":', drain.group(0)))

    record = re.search(r"def record_device_stats\(.*?\n(?=def )", dbsrc, re.S)
    assert record, "could not locate record_device_stats"
    consumed = set(re.findall(r'stats\.get\("(\w+)"\)', record.group(0)))

    missing = sorted(consumed - allowlist)
    assert not missing, (
        f"record_device_stats reads {missing} but the em_controller stats "
        f"handler never copies them — they will always be None"
    )


# ─── v9 — control-plane RTT ──────────────────────────────────────────────────

def test_migrates_to_head(fresh_db):
    """Head of MIGRATIONS, so appending one without bumping its own
    schema_version statement fails here rather than in production."""
    expected = str(len(db.MIGRATIONS))
    assert db._q1("SELECT value FROM system_config WHERE key='schema_version'")["value"] == expected


def test_rtt_accumulates_and_keeps_extremes(fresh_db):
    db.register_new_device("dev1", "10.0.0.9", "vtest")
    db.record_device_stats("dev1", {
        "cpuPct": 5.0, "memUsedMb": 100,
        "rttSumMs": 300, "rttSamples": 6, "rttMinMs": 30, "rttMaxMs": 90,
        "rttExcursions": 0, "rttExcursionsIdle": 0,
    })
    db.record_device_stats("dev1", {
        "cpuPct": 5.0, "memUsedMb": 100,
        "rttSumMs": 1400, "rttSamples": 6, "rttMinMs": 40, "rttMaxMs": 1100,
        "rttExcursions": 2, "rttExcursionsIdle": 2,
    })
    m = db.get_device_metrics("dev1", 0)[-1]
    assert m["rtt_samples"] == 12
    assert m["rtt_avg_ms"] == round(1700 / 12, 1)
    assert m["rtt_min_ms"] == 30    # best across both windows
    assert m["rtt_max_ms"] == 1100  # worst across both windows
    assert m["rtt_excursions"] == 2
    assert m["rtt_excursions_idle"] == 2


def test_window_without_rtt_samples_does_not_poison_the_minimum(fresh_db):
    """
    A stats report can land with no completed probe in its window. That must
    not reset the running minimum to 0 — the same class of bug as
    link_speed's absent-means-not-sampled.
    """
    db.register_new_device("dev1", "10.0.0.9", "vtest")
    db.record_device_stats("dev1", {
        "cpuPct": 5.0, "memUsedMb": 100,
        "rttSumMs": 120, "rttSamples": 3, "rttMinMs": 35, "rttMaxMs": 50,
    })
    db.record_device_stats("dev1", {"cpuPct": 5.0, "memUsedMb": 100})
    m = db.get_device_metrics("dev1", 0)[-1]
    assert m["rtt_min_ms"] == 35
    assert m["rtt_max_ms"] == 50
    assert m["rtt_samples"] == 3


def test_driver_dead_counters_are_not_surfaced(fresh_db):
    """
    tx_errors/tx_dropped/rx_crc read zero on this hardware regardless of link
    quality, so they must not appear in the read API where a zero would be
    mistaken for health.
    """
    db.register_new_device("dev1", "10.0.0.9", "vtest")
    db.record_device_stats("dev1", {"cpuPct": 5.0, "memUsedMb": 100})
    m = db.get_device_metrics("dev1", 0)[-1]
    for dead in ("tx_errors", "tx_dropped", "rx_crc"):
        assert dead not in m


def test_excursion_rate_not_raw_count_is_the_discriminator(fresh_db):
    """
    "Every excursion happened while idle" is vacuous if almost every SAMPLE
    is idle — which is the normal case, since devices spend most of their
    life outside a turn. The read API must expose per-state RATES so the
    comparison is meaningful.
    """
    db.register_new_device("dev1", "10.0.0.9", "vtest")
    # 100 samples: 90 idle with 9 excursions (10%), 10 busy with 5 (50%).
    db.record_device_stats("dev1", {
        "cpuPct": 5.0, "memUsedMb": 100,
        "rttSumMs": 5000, "rttSamples": 100, "rttMinMs": 5, "rttMaxMs": 900,
        "rttExcursions": 14, "rttExcursionsIdle": 9, "rttSamplesIdle": 90,
    })
    m = db.get_device_metrics("dev1", 0)[-1]
    assert m["rtt_excursion_pct_idle"] == 10.0
    assert m["rtt_excursion_pct_busy"] == 50.0


def test_excursion_rates_are_none_without_samples_in_that_state(fresh_db):
    """No busy samples must read None, not 0% — absence of data is not
    evidence of a clean state."""
    db.register_new_device("dev1", "10.0.0.9", "vtest")
    db.record_device_stats("dev1", {
        "cpuPct": 5.0, "memUsedMb": 100,
        "rttSumMs": 500, "rttSamples": 10, "rttMinMs": 5, "rttMaxMs": 90,
        "rttExcursions": 1, "rttExcursionsIdle": 1, "rttSamplesIdle": 10,
    })
    m = db.get_device_metrics("dev1", 0)[-1]
    assert m["rtt_excursion_pct_idle"] == 10.0
    assert m["rtt_excursion_pct_busy"] is None


# ── v13 — on-device wake word shadow mode ────────────────────────────────────

def test_migrates_to_v13(fresh_db):
    assert int(db.get_config("schema_version")) >= 13


def test_turns_has_shadow_columns(fresh_db):
    cols = _cols("turns")
    for c in ("dev_wake_score", "dev_wake_delta_ms", "dev_shadow"):
        assert c in cols, f"turns.{c} missing"


def test_wake_counters_has_shadow_columns(fresh_db):
    cols = _cols("wake_counters")
    for c in ("dev_frames", "dev_drops", "dev_crossings", "dev_max_score"):
        assert c in cols, f"wake_counters.{c} missing"


def test_shadow_turn_fields_round_trip(fresh_db):
    """The v13 columns must survive insert_turn's dict→column mapping. A field
    present in the record but absent from _TURN_COLUMNS is silently discarded,
    which is exactly how a comparison feature ends up reporting nothing."""
    turn_id = db.insert_turn("dev1", {
        "ts": 1_800_000_000,
        "trigger": "wakeword(0.71)",
        "wake_score": 0.71,
        "outcome": "ok",
        "dev_wake_score": 0.63,
        "dev_wake_delta_ms": -140,
        "dev_shadow": 1,
    })
    row = db._q("SELECT * FROM turns WHERE id = ?", (turn_id,))[0]
    assert row["dev_wake_score"] == 0.63
    assert row["dev_wake_delta_ms"] == -140
    assert row["dev_shadow"] == 1


def test_shadow_counters_accumulate_and_max(fresh_db):
    """Counters sum; the max score takes the maximum. Getting the second one
    wrong (summing scores) produces a number that looks like a score and is
    not — the same class of bug as the near-miss max."""
    db.bump_wake_counters("dev2", dev_frames=100, dev_drops=2,
                          dev_crossings=1, dev_max_score=0.4)
    db.bump_wake_counters("dev2", dev_frames=50, dev_drops=1,
                          dev_crossings=2, dev_max_score=0.9)
    db.bump_wake_counters("dev2", dev_frames=25, dev_max_score=0.6)

    rows = db.get_wake_counters("dev2", 0)
    assert len(rows) == 1, "all three bumps belong to the same hour bucket"
    r = rows[0]
    assert r["dev_frames"] == 175
    assert r["dev_drops"] == 3
    assert r["dev_crossings"] == 3
    assert r["dev_max_score"] == 0.9


def test_shadow_counters_do_not_disturb_near_miss_columns(fresh_db):
    """The dev_* arguments were added to an existing upsert that the wake loop
    calls every 2s. A mistake in the ON CONFLICT list would corrupt near-miss
    accounting, which has nothing to do with this feature."""
    db.bump_wake_counters("dev3", near_misses=3, near_miss_max=0.44)
    db.bump_wake_counters("dev3", dev_frames=10, dev_max_score=0.2)
    r = db.get_wake_counters("dev3", 0)[0]
    assert r["near_misses"] == 3
    assert r["near_miss_max"] == 0.44
    assert r["dev_frames"] == 10


# ── v14 — thermals and CPU topology ──────────────────────────────────────────

def test_migrates_to_v14(fresh_db):
    assert int(db.get_config("schema_version")) >= 14


def test_device_metrics_has_thermal_columns(fresh_db):
    cols = _cols("device_metrics")
    for c in ("cpu_temp_sum", "cpu_temp_samples", "cpu_temp_max", "max_temp_max",
              "cores_online_last", "cores_online_min", "cores_total",
              "thermal_limit_min"):
        assert c in cols, f"device_metrics.{c} missing"


def test_thermal_stats_relay_and_rollup(fresh_db):
    """The relay guard: a device stat has to be named in DeviceStats (Go), the
    em_controller allowlist AND here, or it is silently dropped. This covers
    the third."""
    db.record_device_stats("dev-t", {
        "cpuPct": 25.0, "memUsedMb": 200, "cpuTempC": 33.0, "maxTempC": 34.2,
        "coresOnline": 2, "coresTotal": 4, "thermalCoreLimit": 4,
    })
    db.record_device_stats("dev-t", {
        "cpuPct": 51.0, "memUsedMb": 205, "cpuTempC": 39.0, "maxTempC": 41.0,
        "coresOnline": 1, "coresTotal": 4, "thermalCoreLimit": 3,
    })
    m = db.get_device_metrics("dev-t", 0)[0]
    assert m["cpu_temp_avg"] == 36.0, "mean of 33 and 39"
    assert m["cpu_temp_max"] == 39.0
    assert m["max_temp_max"] == 41.0
    assert m["cores_online_last"] == 1
    assert m["cores_online_min"] == 1, "the tightest the device ever ran"
    assert m["cores_total"] == 4
    assert m["thermal_limit_min"] == 3, "throttling happened this hour"


def test_unreadable_temp_does_not_dilute_the_mean(fresh_db):
    """A missing sensor reading must be skipped, not counted as 0C — averaging
    a zero in reads as a cool device, which is the wrong direction for a metric
    whose whole job is to warn."""
    db.record_device_stats("dev-u", {"cpuPct": 20.0, "cpuTempC": 40.0})
    db.record_device_stats("dev-u", {"cpuPct": 20.0})  # sensor unreadable
    m = db.get_device_metrics("dev-u", 0)[0]
    assert m["samples"] == 2
    assert m["cpu_temp_avg"] == 40.0, "one temp sample, not 20.0"


def test_metrics_without_thermals_stay_null(fresh_db):
    """Older firmware sends no thermal fields; those must read as absent rather
    than as a 0C device with 0 cores."""
    db.record_device_stats("dev-old", {"cpuPct": 22.0, "memUsedMb": 180})
    m = db.get_device_metrics("dev-old", 0)[0]
    assert m["cpu_temp_avg"] is None
    assert m["cores_online_last"] is None
    assert m["thermal_limit_min"] is None


# ── v15 — the device's own threshold, per turn ────────────────────────────────

def test_migrates_to_v15(fresh_db):
    assert int(db.get_config("schema_version")) >= 15


def test_turns_has_dev_threshold(fresh_db):
    assert "dev_threshold" in _cols("turns")


def test_barge_in_turn_records_the_threshold_it_actually_cleared(fresh_db):
    """
    A wake that fires during playback clears bargeInThreshold, not owwThreshold.
    Recording the nominal 0.5 produced rows that contradicted themselves —
    wake_score 0.055 against wake_threshold 0.5, i.e. "woke below its own bar" —
    and left the on-device comparison unable to tell a real miss from a question
    the device was never asked.
    """
    turn_id = db.insert_turn("dev-b", {
        "ts": 1_800_000_100,
        "trigger": "wakeword(0.055)",
        "wake_score": 0.055,
        "wake_threshold": 0.05,   # the barge bar it cleared, not the nominal 0.5
        "dev_shadow": 1,
        "dev_threshold": 0.5,     # what the device was scoring against
        "dev_wake_score": None,   # so it did not cross — but fairly so
        "outcome": "ok",
    })
    r = db._q("SELECT * FROM turns WHERE id = ?", (turn_id,))[0]
    assert r["wake_score"] >= r["wake_threshold"], \
        "a turn must not record a score below the threshold it cleared"
    assert r["dev_threshold"] == 0.5
    # The comparison logic in em_api treats this as NOT comparable, because the
    # controller's bar (0.10) is below the device's (0.5).
    assert r["wake_threshold"] < r["dev_threshold"]


# ── v20: BLE transport resets ────────────────────────────────────────────────
#
# The device has counted these since the proxy shipped and sent them inside
# the stats message's `ble` object; nothing stored them, so "how often does
# the transport reset" had no answer for the fleet. They matter because
# /dev/stpbt is the combo radio WiFi also uses.

def test_device_metrics_has_ble_transport_columns(fresh_db):
    cols = _cols("device_metrics")
    for c in ("ble_restarts_last", "ble_hci_errors_last"):
        assert c in cols, f"device_metrics.{c} missing"


def test_ble_counters_are_gauges_not_sums(fresh_db):
    """
    They are cumulative on the DEVICE, so summing them here multiplies one
    reset into hundreds — at a report every 30s, an hour of a device sitting
    at restarts=3 would store 360.
    """
    for _ in range(4):
        db.record_device_stats("D", {"cpuPct": 1.0, "ble": {"restarts": 3,
                                                            "hciErrors": 7}})
    row = db._conn.execute(
        "SELECT ble_restarts_last, ble_hci_errors_last, samples "
        "FROM device_metrics WHERE device_id = 'D'"
    ).fetchone()
    assert row["samples"] == 4, "precondition: four reports folded into the row"
    assert row["ble_restarts_last"] == 3, "restarts must be a gauge, not a sum"
    assert row["ble_hci_errors_last"] == 7, "errors must be a gauge, not a sum"


def test_a_later_report_replaces_the_earlier_count(fresh_db):
    db.record_device_stats("D", {"cpuPct": 1.0, "ble": {"restarts": 1, "hciErrors": 1}})
    db.record_device_stats("D", {"cpuPct": 1.0, "ble": {"restarts": 5, "hciErrors": 9}})
    row = db._conn.execute(
        "SELECT ble_restarts_last, ble_hci_errors_last "
        "FROM device_metrics WHERE device_id = 'D'"
    ).fetchone()
    assert (row["ble_restarts_last"], row["ble_hci_errors_last"]) == (5, 9)


def test_proxy_off_stores_null_not_zero(fresh_db):
    """
    A device with the proxy off never opens the transport, which is not the
    same fact as opening it and never faltering. Zero would make the two
    indistinguishable in exactly the query this column exists for.
    """
    db.record_device_stats("D", {"cpuPct": 1.0})
    row = db._conn.execute(
        "SELECT ble_restarts_last, ble_hci_errors_last "
        "FROM device_metrics WHERE device_id = 'D'"
    ).fetchone()
    assert row["ble_restarts_last"] is None
    assert row["ble_hci_errors_last"] is None


def test_zero_is_stored_as_zero(fresh_db):
    """The other side of the NULL rule: an enabled proxy reporting a clean
    transport must store 0, not fall through to NULL on falsiness."""
    db.record_device_stats("D", {"cpuPct": 1.0, "ble": {"restarts": 0, "hciErrors": 0}})
    row = db._conn.execute(
        "SELECT ble_restarts_last, ble_hci_errors_last "
        "FROM device_metrics WHERE device_id = 'D'"
    ).fetchone()
    assert row["ble_restarts_last"] == 0
    assert row["ble_hci_errors_last"] == 0
