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

    record = re.search(r"def record_device_stats\(.*?\n(?=def )", dbsrc, re.S)
    assert record, "could not locate record_device_stats"
    consumed = set(re.findall(r'stats\.get\("(\w+)"\)', record.group(0)))

    missing = sorted(consumed - allowlist)
    assert not missing, (
        f"record_device_stats reads {missing} but the em_controller stats "
        f"handler never copies them — they will always be None"
    )
