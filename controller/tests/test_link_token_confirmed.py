"""
token_confirmed_at: recorded once a device presents its token, always about the
CURRENT token, and wired into every plane's auth.

Real temporary database, migrations included (schema v27).
"""

import ast
from pathlib import Path

import pytest

import em_db as db

CONTROLLER = Path(__file__).resolve().parents[1]
DEV = "G090LF1180571VVV"


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "test.db"))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_a_new_token_is_unconfirmed(fresh_db):
    tok = db.ensure_device_token(DEV)
    assert db.get_device_link_auth(DEV) == (tok, False)


def test_confirming_records_once(fresh_db):
    tok = db.ensure_device_token(DEV)
    assert db.confirm_device_token(DEV, tok) is True
    assert db.get_device_link_auth(DEV) == (tok, True)
    assert db.confirm_device_token(DEV, tok) is False   # already recorded


def test_a_stale_token_cannot_confirm_its_replacement(fresh_db):
    old = db.ensure_device_token(DEV)
    db.clear_device_token(DEV)
    new = db.ensure_device_token(DEV)
    assert new != old
    assert db.confirm_device_token(DEV, old) is False
    assert db.get_device_link_auth(DEV) == (new, False)


def test_changing_the_token_forgets_the_confirmation(fresh_db):
    tok = db.ensure_device_token(DEV)
    db.confirm_device_token(DEV, tok)
    db.clear_device_token(DEV)
    assert db.get_device_link_auth(DEV) == (None, False)
    new = db.ensure_device_token(DEV)
    assert db.get_device_link_auth(DEV) == (new, False)


def test_ensure_returns_the_existing_token_and_keeps_its_confirmation(fresh_db):
    tok = db.ensure_device_token(DEV)
    db.confirm_device_token(DEV, tok)
    assert db.ensure_device_token(DEV) == tok
    assert db.get_device_link_auth(DEV) == (tok, True)


def test_deleting_the_device_forgets_everything(fresh_db):
    tok = db.ensure_device_token(DEV)
    db.confirm_device_token(DEV, tok)
    db.delete_device(DEV)
    assert db.get_device_link_auth(DEV) == (None, False)


def test_an_unknown_device_has_nothing(fresh_db):
    assert db.get_device_link_auth("G090NOBODY000000") == (None, False)


# ── wiring, by ast (em_controller is not importable in this suite) ──────────

def _fn(name):
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls(fn, attr):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None)) == attr]


def test_link_auth_passes_the_confirmation_to_the_decision():
    fn = _fn("_link_auth_ok")
    decide = _calls(fn, "decide")
    assert len(decide) == 1
    assert "confirmed" in {k.arg for k in decide[0].keywords}
    src = ast.unparse(fn)
    assert "db.get_device_link_auth" in src and "db.confirm_device_token" in src


def test_data_and_shell_follow_the_control_connection():
    for name in ("handle_data", "handle_shell"):
        assert _calls(_fn(name), "_not_from_control"), (
            f"{name} must refuse a connection that is not from the device's control plane")
