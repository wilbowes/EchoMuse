"""
Tests for the Home Assistant user lookup path (em_db.get_user_by_ha_id /
create_ha_user).

get_user_by_ha_id shipped broken (`with _conn() as conn:` called the raw
connection object as a function instead of using the module's own _q1
helper) and stayed that way through several releases because nothing
exercised it — it is only reached via ingress login, which the test suite
had no coverage for either. These tests run it against a real database, the
same discipline test_db_instrumentation.py uses.
"""

import pytest

import em_db as db


@pytest.fixture()
def fresh_db(tmp_path):
    """A real database migrated from scratch to the current schema."""
    path = tmp_path / "test.db"
    db.init(str(path))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_ha_user_found_by_ha_id(fresh_db):
    user_id = db.create_ha_user("ha-abc123", "wil", "admin")

    row = db.get_user_by_ha_id("ha-abc123")

    assert row is not None
    assert row["id"] == user_id
    assert row["username"] == "wil"
    assert row["role"] == "admin"
    # No password can ever match this account via the standalone login form
    # — see create_ha_user's docstring.
    assert row["password_hash"] == db.HA_USER_PASSWORD_SENTINEL


def test_unknown_ha_id_returns_none(fresh_db):
    assert db.get_user_by_ha_id("no-such-id") is None
