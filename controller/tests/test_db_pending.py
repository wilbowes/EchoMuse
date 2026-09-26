"""
#453: a device the provisioning wizard credentialed is not pending approval
until it has connected. The wizard creates the row to hold the token before
first contact, and stamping it "seen" made an abandoned provisioning run look
like a device asking to be approved.
"""

import pytest

import em_db as db


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "test.db"))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _pending_ids():
    return [r["device_id"] for r in db.get_pending_devices()]


def test_a_credentialed_device_that_never_connected_is_not_pending(fresh_db):
    db.ensure_device_token("G090LF00000000AA")
    row = db.get_device("G090LF00000000AA")
    assert row["first_seen"] is None and row["last_seen"] is None
    assert _pending_ids() == []


def test_its_first_connection_makes_it_pending_and_stamps_first_seen(fresh_db):
    db.ensure_device_token("G090LF00000000AA")
    db.upsert_device_seen("G090LF00000000AA", "10.0.0.5", "v2.17.0")
    row = db.get_device("G090LF00000000AA")
    assert row["first_seen"] is not None and row["last_seen"] is not None
    assert _pending_ids() == ["G090LF00000000AA"]


def test_first_seen_is_not_moved_by_later_connections(fresh_db):
    db.register_new_device("G090LF00000000BB", "10.0.0.6", "v2.17.0")
    first = db.get_device("G090LF00000000BB")["first_seen"]
    with db._tx() as conn:
        conn.execute("UPDATE devices SET first_seen = first_seen - 1000")
    db.upsert_device_seen("G090LF00000000BB", "10.0.0.6", "v2.17.0")
    assert db.get_device("G090LF00000000BB")["first_seen"] == first - 1000
