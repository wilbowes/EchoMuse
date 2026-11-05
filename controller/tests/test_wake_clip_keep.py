"""
wakeClipKeep: per-Echo wake-sample retention with fleet inheritance.

Retention is a policy, not a preference: every clip kept is speech recorded in
someone's home, so the value is clamped at both write paths and again when it
is read. A stored value outside 10-500 — written before the clamp existed, or
straight into the database — is never honoured.
"""

import asyncio
import json
import sqlite3

import pytest
from aiohttp.test_utils import make_mocked_request

import em_db as db
import em_routes_activity
import em_wake_samples as samples


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "keep.db")
    db.init(path)
    monkeypatch.setenv("DB_PATH", path)
    db.register_new_device("dev1", "127.0.0.1", "v1")
    yield path
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _stored(path, device_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT config FROM devices WHERE device_id = ?",
                           (device_id,)).fetchone()
        return json.loads(row[0])
    finally:
        conn.close()


def _override_wakeword(device_id):
    db.set_device_config_sections(device_id, ["wakeword"])


def _sample(ts):
    db.insert_wake_sample("dev1", {
        "ts": ts, "audio_file": samples.filename("dev1"), "kind": "candidate",
    })


def test_the_default_is_fifty(fresh_db):
    assert db.DEFAULT_DEVICE_CONFIG["wakeClipKeep"] == 50
    assert samples.DEFAULT_KEEP_PER_DEVICE == 50
    assert db.effective_wake_clip_keep("dev1") == 50


def test_an_echo_inherits_the_fleet_until_it_overrides_wakeword(fresh_db):
    db.register_new_device("dev2", "127.0.0.2", "v1")
    db.set_global_device_config({"wakeClipKeep": 120})
    assert db.effective_wake_clip_keep("dev1") == 120

    _override_wakeword("dev2")
    db.set_device_config("dev2", {"wakeClipKeep": 200})
    assert db.effective_wake_clip_keep("dev2") == 200
    assert db.effective_wake_clip_keep("dev1") == 120


@pytest.mark.parametrize("sent,expected", [
    (9, 10),
    (10, 10),
    (500, 500),
    (501, 500),
])
def test_a_device_write_is_clamped(fresh_db, sent, expected):
    _override_wakeword("dev1")
    db.set_device_config("dev1", {"wakeClipKeep": sent})
    assert _stored(fresh_db, "dev1")["wakeClipKeep"] == expected


@pytest.mark.parametrize("sent,expected", [
    (9, 10),
    (10, 10),
    (500, 500),
    (501, 500),
])
def test_the_fleet_write_is_clamped(fresh_db, sent, expected):
    db.set_global_device_config({"wakeClipKeep": sent})
    assert db.get_global_device_config()["wakeClipKeep"] == expected


def test_the_callers_dict_is_not_edited_underneath_them(fresh_db):
    sent = {"wakeClipKeep": 501}
    db.set_device_config("dev1", sent)
    assert sent["wakeClipKeep"] == 501


@pytest.mark.parametrize("stored,effective", [
    (5000, 500),
    (0, 10),
    (True, 50),
    ("200", 50),
])
def test_a_stored_value_outside_the_range_is_never_honoured(fresh_db, stored, effective):
    _override_wakeword("dev1")
    db._conn.execute(
        "UPDATE devices SET config = ? WHERE device_id = 'dev1'",
        (json.dumps({"wakeClipKeep": stored}),),
    )
    db._conn.commit()
    assert db.effective_wake_clip_keep("dev1") == effective


def test_the_prune_keeps_the_effective_count(fresh_db):
    _override_wakeword("dev1")
    db.set_device_config("dev1", {"wakeClipKeep": 12})
    for i in range(15):
        _sample(i)

    rows = db.get_wake_samples("dev1", db.WAKE_CLIP_KEEP_MAX)
    assert len(rows) == 12
    assert rows[0]["ts"] == 14


def test_the_prune_follows_the_fleet_for_an_inheriting_echo(fresh_db):
    db.set_global_device_config({"wakeClipKeep": 11})
    for i in range(14):
        _sample(i)
    assert len(db.get_wake_samples("dev1", db.WAKE_CLIP_KEEP_MAX)) == 11


def test_the_review_list_ceiling_is_the_clamp_max(monkeypatch):
    seen = []
    monkeypatch.setattr(
        em_routes_activity.db, "get_wake_samples",
        lambda device_id, limit: seen.append(limit) or [],
    )
    request = make_mocked_request(
        "GET", "/api/devices/dev1/wake-samples?limit=100000",
        match_info={"id": "dev1"},
    )
    response = asyncio.run(
        em_routes_activity._get_wake_samples.__wrapped__(request)
    )
    assert response.status == 200
    assert seen == [db.WAKE_CLIP_KEEP_MAX] == [500]


def test_the_db_limit_cannot_exceed_the_clamp_max(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        db, "_q", lambda sql, params: seen.setdefault("limit", params[1]) or []
    )
    db.get_wake_samples("dev1", 10 ** 6)
    assert seen["limit"] == db.WAKE_CLIP_KEEP_MAX
