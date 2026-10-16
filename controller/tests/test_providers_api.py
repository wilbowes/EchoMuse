"""
Tests for the cloud-provider API routes (em_routes_providers).

What is pinned: the three routes exist with the contracted paths and are
all admin-gated (the repo's test_route_auth fails the build for an
undecorated route — the AST test here names these three so a regression
points straight at them); PATCH honors the key-write protocol (new key is
validated then stored, the sentinel keeps, empty deletes, and a bad entry
writes nothing at all); no response on any route ever carries key material;
and /test dials with the STORED key, never one from the request.

No network, no real keys: test_connection is monkeypatched, and the keys
used are obvious fakes. The key store and the database both live under
tmp_path (DB_PATH env + em_db.init), following test_esphome_ports'
reload pattern.
"""

import ast
import asyncio
import importlib
import json
from pathlib import Path

import pytest

import em_auth
import em_db
import em_provider_keys as pk
import em_providers
import em_routes_providers as routes

HERE = Path(__file__).resolve()
CONTROLLER = HERE.parents[1]

CANARY_A = "sk-or-v1-fakecanary-alpha"
CANARY_B = "gsk_fakecanary-beta"


class FakeRequest(dict):
    """The two things handlers touch: an async JSON body and item storage."""

    def __init__(self, body):
        super().__init__()
        self._body = body

    async def json(self):
        return self._body


def _run(coro):
    return asyncio.run(coro)


def _body(resp):
    return json.loads(resp.body.decode())


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    """Fresh DB and key store per test; the shared em_db left as found."""
    importlib.reload(em_db)
    db_path = str(tmp_path / "t.db")
    em_db.init(db_path)
    monkeypatch.setenv("DB_PATH", db_path)
    yield
    importlib.reload(em_db)


@pytest.fixture()
def _admin(monkeypatch):
    async def _resolve(request):
        return {"id": 1, "username": "admin", "role": "admin", "token": "t"}
    monkeypatch.setattr(em_auth, "resolve_session", _resolve)


# ─── route table + auth gate (AST, no app start) ─────────────────────────────

def _tree():
    return ast.parse((CONTROLLER / "em_routes_providers.py").read_text())


def test_all_three_routes_registered_with_contracted_paths():
    found = {}
    for node in ast.walk(_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("add_")
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)):
            found[node.args[1].id] = (node.func.attr, node.args[0].value)
    assert found == {
        "_get_providers": ("add_get", "/api/system/providers"),
        "_patch_providers": ("add_patch", "/api/system/providers"),
        "_post_providers_test": ("add_post", "/api/system/providers/test"),
        "_post_providers_ask": ("add_post", "/api/system/providers/ask"),
    }


def test_all_three_handlers_require_admin():
    fns = {n.name: n for n in ast.walk(_tree())
           if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("_get_providers", "_patch_providers", "_post_providers_test",
                 "_post_providers_ask"):
        decs = {d.attr for d in fns[name].decorator_list
                if isinstance(d, ast.Attribute)}
        assert "require_admin" in decs, name


def test_register_adds_three_routes_to_a_real_application():
    from aiohttp import web
    app = web.Application()
    routes.register(app)
    got = sorted((r.canonical, method) for r in app.router.resources()
                 for method in (r._routes or ()) if method != "HEAD")
    assert got == [
        ("/api/system/providers", "GET"),
        ("/api/system/providers", "PATCH"),
        ("/api/system/providers/ask", "POST"),
        ("/api/system/providers/test", "POST"),
    ]


# ─── live-handler tests ──────────────────────────────────────────────────────

def test_unauthenticated_and_non_admin_are_refused(_isolated, monkeypatch):
    async def _none(request):
        return None
    monkeypatch.setattr(em_auth, "resolve_session", _none)
    for handler, body in (
            (routes._get_providers, None),
            (routes._patch_providers, {"providers": []}),
            (routes._post_providers_test, {"id": "openai"}),
            (routes._post_providers_ask, {"id": "openai", "prompt": "hi"})):
        resp = _run(handler(FakeRequest(body)))
        assert resp.status == 401, handler.__name__

    async def _viewer(request):
        return {"id": 2, "username": "v", "role": "viewer", "token": "t"}
    monkeypatch.setattr(em_auth, "resolve_session", _viewer)
    resp = _run(routes._get_providers(FakeRequest(None)))
    assert resp.status == 403


def test_get_shape_and_no_key_values(_isolated, _admin):
    pk.set_key("openai", CANARY_A)
    resp = _run(routes._get_providers(FakeRequest(None)))
    assert resp.status == 200
    data = _body(resp)
    by_id = {p["id"]: p for p in data["providers"]}
    assert set(by_id) == set(em_providers.PROVIDERS)
    for pid, p in by_id.items():
        assert set(p) == {"id", "label", "key_set", "models", "default_model"}
        assert isinstance(p["key_set"], bool)
        assert p["models"] == em_providers.models_for(pid)
    assert by_id["openai"]["key_set"] is True
    assert by_id["groq"]["key_set"] is False
    assert by_id["openai"]["default_model"] == \
        em_providers.default_model("openai")
    raw = resp.body.decode()
    assert CANARY_A not in raw


def test_patch_stores_validated_key_and_default_model(_isolated, _admin):
    model = em_providers.models_for("openai")[0]
    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "openai", "key": CANARY_A, "default_model": model},
    ]})))
    assert resp.status == 200
    assert pk.get_key("openai") == CANARY_A
    data = _body(resp)
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["openai"]["key_set"] is True
    assert by_id["openai"]["default_model"] == model
    assert CANARY_A not in resp.body.decode()


def test_patch_sentinel_keeps_and_empty_deletes(_isolated, _admin):
    pk.set_key("openai", CANARY_A)
    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "openai", "key": pk.SENTINEL},
    ]})))
    assert resp.status == 200
    assert pk.get_key("openai") == CANARY_A  # kept, not stored as sentinel
    assert pk.SENTINEL != CANARY_A

    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "openai", "key": ""},
    ]})))
    assert resp.status == 200
    assert pk.has_key("openai") is False


def test_patch_rejects_bad_key_and_writes_nothing(_isolated, _admin):
    # openai's entry is valid; groq's is not (needs the gsk_ prefix). The
    # whole write must fail WITHOUT storing openai's key first.
    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "openai", "key": CANARY_A},
        {"id": "groq", "key": "bogus-key"},
    ]})))
    assert resp.status == 400
    assert _body(resp)["code"] == "bad_provider_key"
    assert pk.has_key("openai") is False
    assert pk.has_key("groq") is False


def test_patch_rejects_unknown_provider_and_bad_shapes(_isolated, _admin):
    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "nope", "key": "x"},
    ]})))
    assert resp.status == 400
    assert _body(resp)["code"] == "unknown_provider"

    resp = _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "openai", "key": CANARY_A, "default_model": "not-a-model"},
    ]})))
    assert resp.status == 400
    assert _body(resp)["code"] == "bad_default_model"
    assert pk.has_key("openai") is False  # model failure also writes nothing

    resp = _run(routes._patch_providers(FakeRequest({})))
    assert resp.status == 400
    assert _body(resp)["code"] == "missing_field"


def test_default_model_override_round_trip_and_clear(_isolated, _admin):
    models = em_providers.models_for("groq")
    other = models[1] if len(models) > 1 else models[0]
    assert other != em_providers.default_model("groq") or len(models) == 1
    _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "groq", "default_model": models[-1]},
    ]})))
    data = _body(_run(routes._get_providers(FakeRequest(None))))
    assert {p["id"]: p for p in data["providers"]}["groq"]["default_model"] \
        == models[-1]

    _run(routes._patch_providers(FakeRequest({"providers": [
        {"id": "groq", "default_model": ""},
    ]})))
    data = _body(_run(routes._get_providers(FakeRequest(None))))
    assert {p["id"]: p for p in data["providers"]}["groq"]["default_model"] \
        == em_providers.default_model("groq")


def test_test_endpoint_uses_the_stored_key(_isolated, _admin, monkeypatch):
    pk.set_key("groq", CANARY_B)
    seen = {}

    async def _fake_test(provider_id, key):
        seen["provider_id"] = provider_id
        seen["key"] = key
        return True, "connection ok"

    monkeypatch.setattr(em_providers, "test_connection", _fake_test)
    resp = _run(routes._post_providers_test(FakeRequest({"id": "groq"})))
    assert resp.status == 200
    assert _body(resp) == {"ok": True, "message": "connection ok"}
    # The request carried no key — the stored one was used.
    assert seen == {"provider_id": "groq", "key": CANARY_B}
    assert CANARY_B not in resp.body.decode()


def test_test_endpoint_without_a_key_is_ok_false(_isolated, _admin,
                                                 monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not dial without a stored key")
    monkeypatch.setattr(em_providers, "test_connection", _boom)
    resp = _run(routes._post_providers_test(FakeRequest({"id": "openai"})))
    assert resp.status == 200
    data = _body(resp)
    assert data["ok"] is False and data["message"]


def test_test_endpoint_rejects_bad_id(_isolated, _admin):
    resp = _run(routes._post_providers_test(FakeRequest({"id": "nope"})))
    assert resp.status == 400
    assert _body(resp)["code"] == "unknown_provider"
    resp = _run(routes._post_providers_test(FakeRequest({})))
    assert resp.status == 400
