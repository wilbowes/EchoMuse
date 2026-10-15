"""
Accounts and sessions for OpenID Connect sign-ins (em_db users, em_auth).

Runs against a real, freshly migrated database, like test_db_users.py, so the
schema v32 migration is exercised by the same tests that depend on it.

The properties pinned here are the ones that decide who is let in:

  * an account is keyed on (issuer, subject), never on a name — names are
    editable at the provider, and keying on one would hand a renamed user a
    fresh account, or hand somebody else's account to whoever took the name;
  * the role is re-derived from groups on EVERY sign-in, unlike Home
    Assistant ingress, because here the provider is the source of truth;
  * losing access at the provider ends existing sessions on the next
    sign-in attempt, and a role change ends the old sessions too, so a
    demotion is not undone by a token that was already issued.
"""

import asyncio

import pytest

import em_auth
import em_db as db
import em_oidc

ISSUER = "https://auth.example.test/application/o/echomuse/"


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "test.db"))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def identity(sub="sub-1", username="anthony", groups=("Family",), issuer=ISSUER):
    return em_oidc.Identity(issuer=issuer, subject=sub, username=username,
                            groups=list(groups))


def sign_in(ident, role, hours=12):
    return asyncio.run(em_auth.login_via_oidc(ident, role, session_hours=hours))


# ── Database ──────────────────────────────────────────────────────────────────

def test_oidc_user_is_found_by_issuer_and_subject(fresh_db):
    user_id = db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")

    row = db.get_user_by_oidc(ISSUER, "sub-1")

    assert row["id"] == user_id
    assert row["username"] == "anthony"
    assert row["role"] == "readonly"


def test_unknown_subject_is_none(fresh_db):
    assert db.get_user_by_oidc(ISSUER, "nobody") is None


def test_same_subject_from_another_issuer_is_a_different_person(fresh_db):
    db.create_oidc_user(ISSUER, "sub-1", "anthony", "admin")

    assert db.get_user_by_oidc("https://other.example.test/", "sub-1") is None


def test_oidc_account_can_never_match_the_password_form(fresh_db):
    db.create_oidc_user(ISSUER, "sub-1", "anthony", "admin")
    row = db.get_user_by_oidc(ISSUER, "sub-1")

    assert row["password_hash"] == db.HA_USER_PASSWORD_SENTINEL
    assert em_auth.verify_password("", row["password_hash"]) is False
    assert em_auth.verify_password(db.HA_USER_PASSWORD_SENTINEL,
                                   row["password_hash"]) is False


def test_username_collision_is_suffixed_not_merged(fresh_db):
    first = db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")
    second = db.create_oidc_user(ISSUER, "sub-2", "anthony", "readonly")

    assert first != second
    assert db.get_user_by_id(second)["username"] == "anthony-2"


def test_a_local_account_name_is_not_taken_over(fresh_db):
    local = db.create_user("admin", "$2b$12$notarealhash", "admin")

    oidc = db.create_oidc_user(ISSUER, "sub-9", "admin", "readonly")

    assert db.get_user_by_id(local)["role"] == "admin"
    assert db.get_user_by_id(oidc)["username"] == "admin-2"


def test_session_lifetime_can_be_shorter_than_a_day(fresh_db):
    uid = db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")
    now = db._now()

    db.create_session("tok", uid, expiry_days=12 / 24)

    expires = db.get_session("tok")["expires_at"]
    assert 11 * 3600 < expires - now <= 12 * 3600 + 5
    assert isinstance(expires, int)


# ── Sign-in ───────────────────────────────────────────────────────────────────

def test_first_sign_in_creates_the_account_with_the_mapped_role(fresh_db):
    token, role = sign_in(identity(), "readonly")

    assert role == "readonly"
    row = db.get_user_by_oidc(ISSUER, "sub-1")
    assert row is not None
    assert db.get_session(token)["user_id"] == row["id"]


def test_second_sign_in_reuses_the_account(fresh_db):
    sign_in(identity(), "readonly")
    sign_in(identity(), "readonly")

    assert len(db.get_all_users()) == 1


def test_renaming_at_the_provider_does_not_create_a_second_account(fresh_db):
    sign_in(identity(username="anthony"), "readonly")
    sign_in(identity(username="tony"), "readonly")

    users = db.get_all_users()
    assert len(users) == 1
    assert users[0]["username"] == "anthony"


def test_role_follows_the_groups_on_every_sign_in(fresh_db):
    sign_in(identity(), "readonly")

    _, role = sign_in(identity(groups=("Admins",)), "admin")

    assert role == "admin"
    assert db.get_user_by_oidc(ISSUER, "sub-1")["role"] == "admin"


def test_demotion_ends_the_sessions_issued_before_it(fresh_db):
    old_token, _ = sign_in(identity(groups=("Admins",)), "admin")

    new_token, role = sign_in(identity(), "readonly")

    assert role == "readonly"
    assert db.get_session(old_token) is None
    assert db.get_session(new_token) is not None


def test_a_sign_in_with_no_change_leaves_other_sessions_alone(fresh_db):
    first, _ = sign_in(identity(), "readonly")

    sign_in(identity(), "readonly")

    assert db.get_session(first) is not None


def test_no_mapped_group_is_refused_and_creates_nothing(fresh_db):
    with pytest.raises(em_auth.AuthError) as err:
        sign_in(identity(groups=("Friends",)), None)

    assert err.value.code == "no_access"
    assert err.value.status == 403
    assert db.get_all_users() == []


def test_losing_every_group_ends_the_existing_sessions(fresh_db):
    token, _ = sign_in(identity(), "readonly")

    with pytest.raises(em_auth.AuthError):
        sign_in(identity(groups=()), None)

    assert db.get_session(token) is None


def test_session_lifetime_comes_from_the_configured_hours(fresh_db):
    token, _ = sign_in(identity(), "readonly", hours=2)

    remaining = db.get_session(token)["expires_at"] - db._now()
    assert 3600 < remaining <= 2 * 3600 + 5


def test_a_local_admin_is_untouched_by_oidc_accounts(fresh_db):
    local = db.create_user("break-glass", "$2b$12$notarealhash", "admin")

    sign_in(identity(), "readonly")

    assert db.get_user_by_id(local)["role"] == "admin"
    assert db.admin_count() == 1


def test_two_simultaneous_first_sign_ins_make_one_account_not_a_500(fresh_db, monkeypatch):
    # Both requests look the person up, both find nothing, both try to
    # create. The unique index lets exactly one in; the other must carry on
    # as an ordinary sign-in rather than surfacing the constraint error.
    db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")
    real = db.get_user_by_oidc
    calls = {"n": 0}

    def stale_first_lookup(issuer, subject):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(issuer, subject)
    monkeypatch.setattr(db, "get_user_by_oidc", stale_first_lookup)

    token, role = sign_in(identity(), "readonly")

    assert role == "readonly"
    assert db.get_session(token) is not None
    assert len(db.get_all_users()) == 1


def test_the_race_loser_still_gets_the_role_it_earned(fresh_db, monkeypatch):
    db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")
    real = db.get_user_by_oidc
    calls = {"n": 0}

    def stale_first_lookup(issuer, subject):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(issuer, subject)
    monkeypatch.setattr(db, "get_user_by_oidc", stale_first_lookup)

    _, role = sign_in(identity(groups=("Admins",)), "admin")

    assert role == "admin"
    assert db.get_user_by_oidc(ISSUER, "sub-1")["role"] == "admin"
