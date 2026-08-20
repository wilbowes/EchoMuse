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


# ── Reachable admins (#235) ───────────────────────────────────────────────────
#
# The bug that stranded a live add-on was a COUNT of the wrong thing: user
# rows rather than admins who can actually sign in through ingress. These run
# against a real database because the distinction is a SQL predicate, and the
# pure-function tests in test_ingressauth.py cannot see it.


def test_a_local_admin_is_not_a_reachable_admin(fresh_db):
    """
    The exact #235 shape. A local password admin cannot be signed into under
    ingress — the landing page authenticates through ingress before rendering
    any form — so it must not count, or the first Home Assistant user is
    created read-only and there is no way back.
    """
    db.create_user("admin", "$2b$dummy", "admin")
    assert db.user_count() == 1
    assert db.ha_admin_count() == 0


def test_an_ha_admin_counts(fresh_db):
    db.create_ha_user("ha-abc123", "Wil", "admin")
    assert db.ha_admin_count() == 1


def test_a_readonly_ha_user_is_not_an_admin(fresh_db):
    """Reachable is not enough — they have to be able to administer."""
    db.create_ha_user("ha-readonly", "Guest", "readonly")
    assert db.ha_admin_count() == 0


def test_the_migrator_case_end_to_end(fresh_db):
    """
    Carrying a container's /data across is what docs/migrate-to-addon.md
    tells people to do so their devices keep working, and it is what made
    every Home Assistant user read-only forever.
    """
    db.create_user("admin", "$2b$dummy", "admin")       # from the container
    db.create_user("family", "$2b$dummy", "readonly")
    assert db.user_count() == 2
    assert db.ha_admin_count() == 0                      # nobody can get in

    import em_ingressauth
    assert em_ingressauth.role_for(
        existing_ha_admins=db.ha_admin_count(),
        configured_default="readonly",
    ) == "admin"

    # ...and the SECOND Home Assistant user is still read-only, which is the
    # property the fix must not trade away.
    db.create_ha_user("ha-first", "Wil", "admin")
    assert db.ha_admin_count() == 1
    assert em_ingressauth.role_for(
        existing_ha_admins=db.ha_admin_count(),
        configured_default=None,
    ) == "readonly"
