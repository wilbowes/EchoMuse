"""
Changing a password ends every other session for that user, and only those.

Sessions last 30 days and were left alive by a password change, so the one
action someone takes after suspecting their password was used did not lock
out the session using it. Real temporary database, as test_db_instrumentation.
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


def _tokens():
    return {r["token"] for r in db._q("SELECT token FROM sessions")}


def test_other_sessions_end_and_the_current_one_survives(fresh_db):
    alice = db.create_user("alice", "h1", "admin")
    bob = db.create_user("bob", "h2", "readonly")
    for t in ("a-here", "a-laptop", "a-phone"):
        db.create_session(t, alice)
    db.create_session("b-1", bob)

    revoked = db.update_user_password(alice, "h1-new", keep_session="a-here")

    assert revoked == 2
    assert _tokens() == {"a-here", "b-1"}   # bob untouched
    assert db.get_user_by_id(alice)["password_hash"] == "h1-new"


def test_no_kept_session_ends_them_all(fresh_db):
    # NULL must not match "IS NOT ?" by accident and keep everything.
    alice = db.create_user("alice", "h1", "admin")
    db.create_session("a-1", alice)
    db.create_session("a-2", alice)

    assert db.update_user_password(alice, "h", keep_session=None) == 2
    assert _tokens() == set()


def test_unknown_user_changes_nothing(fresh_db):
    alice = db.create_user("alice", "h1", "admin")
    db.create_session("a-1", alice)

    with pytest.raises(ValueError):
        db.update_user_password(alice + 99, "h", keep_session=None)
    assert _tokens() == {"a-1"}


def test_keep_session_is_required():
    with pytest.raises(TypeError):
        db.update_user_password(1, "h")
