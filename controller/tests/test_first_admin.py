"""
First-run setup creates exactly one admin, however many requests race it.

create_first_admin checked the bootstrap token and the user count, awaited
bcrypt in the executor, then wrote. Two requests carrying the token both
passed the checks during that await and both created an admin.

em_auth imports aiohttp and bcrypt, which the minimal suite does not install,
so this stubs those two and em_db for the duration of each test and drives the
real function.
"""

import asyncio
import importlib
import sys
import types

import pytest


class FakeDB(types.ModuleType):
    def __init__(self):
        super().__init__("em_db")
        self.users = []

    def user_count(self):
        return len(self.users)

    def create_user(self, username, hashed, role):
        self.users.append((username, role))


@pytest.fixture
def auth(monkeypatch):
    db = FakeDB()

    bcrypt = types.ModuleType("bcrypt")
    bcrypt.gensalt = lambda rounds=12: b"salt"
    bcrypt.hashpw = lambda pw, salt: b"hash:" + pw.hex().encode()
    bcrypt.checkpw = lambda pw, h: h == b"hash:" + pw.hex().encode()

    web = types.ModuleType("aiohttp.web")
    web.Request = web.Response = object
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = web

    monkeypatch.setitem(sys.modules, "em_db", db)
    monkeypatch.setitem(sys.modules, "bcrypt", bcrypt)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)
    monkeypatch.setitem(sys.modules, "em_ingressauth",
                        types.ModuleType("em_ingressauth"))
    monkeypatch.delitem(sys.modules, "em_auth", raising=False)

    mod = importlib.import_module("em_auth")
    mod._bootstrap_token = "t" * 64
    yield mod, db
    sys.modules.pop("em_auth", None)


def test_concurrent_setups_create_one_admin(auth):
    mod, db = auth

    async def race():
        return await asyncio.gather(
            mod.create_first_admin("t" * 64, "alice", "password1"),
            mod.create_first_admin("t" * 64, "bob", "password2"),
            return_exceptions=True,
        )

    results = asyncio.run(race())

    assert db.users == [("alice", "admin")]
    assert results[0] is None
    assert isinstance(results[1], mod.AuthError)
    assert results[1].code == "setup_complete"


def test_a_failed_setup_leaves_the_token_usable(auth):
    mod, db = auth

    with pytest.raises(mod.AuthError) as e:
        asyncio.run(mod.create_first_admin("t" * 64, "alice", "short"))
    assert e.value.code == "invalid_input"

    asyncio.run(mod.create_first_admin("t" * 64, "alice", "password1"))
    assert db.users == [("alice", "admin")]
    assert mod.get_bootstrap_token() is None


def test_wrong_token_is_refused(auth):
    mod, db = auth
    with pytest.raises(mod.AuthError) as e:
        asyncio.run(mod.create_first_admin("x" * 64, "alice", "password1"))
    assert e.value.code == "invalid_token"
    assert db.users == []
