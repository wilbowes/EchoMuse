"""
Tests for the provider query client (em_provider_query) and the ask route.

What is pinned: ask() routes Zen chat-family models through the chat shape
and Zen gpt-/grok-/spark- models through the responses shape (both via the
em_providers family helpers); the plain OpenAI chat shape is used for the
OpenAI-compatible providers against their catalog base URLs; a missing stored
key fails without touching the network; providers with no chat shape in
em_providers (gemini/claude/nvidia) fail instead of guessing a wire format;
the ask route 400s an empty prompt with the {error, code} shape; and no key,
prompt, or answer material ever reaches the logs.

No network, no real keys: transport is a fake session, keys/prompts/answers
are obvious canaries. The key store and the database live under tmp_path
(DB_PATH env + em_db.init), following test_providers_api's pattern.
"""

import asyncio
import importlib
import json
import logging

import pytest

import em_auth
import em_db
import em_provider_keys as pk
import em_provider_query as pq
import em_providers
import em_routes_providers as routes

KEY_CANARY = "sk-fakecanary-querykey"
PROMPT_CANARY = "PROMPT-CANARY-9f31"
ANSWER_CANARY = "ANSWER-CANARY-77ab"


class FakeRequest(dict):
    def __init__(self, body):
        super().__init__()
        self._body = body

    async def json(self):
        return self._body


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Stands in for aiohttp: scripted status/payload, records calls."""

    def __init__(self, status=200, payload=None, exc=None):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.exc = exc
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.status, self.payload)


def _run(coro):
    return asyncio.run(coro)


def _body(resp):
    return json.loads(resp.body.decode())


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
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


def _with_key(monkeypatch, key=KEY_CANARY):
    monkeypatch.setattr(pk, "get_key", lambda pid: key)


def _chat_payload(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# ─── ask(): OpenAI-compatible chat shape ────────────────────────────────────

def test_openai_ask_posts_chat_completions_and_returns_text(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload(ANSWER_CANARY))
    out = _run(pq.ask("openai", PROMPT_CANARY, model="gpt-5-mini", session=s))
    assert out == ANSWER_CANARY
    assert len(s.calls) == 1
    method, url, kwargs = s.calls[0]
    assert method == "POST"
    assert url == "https://api.openai.com/v1/chat/completions"
    assert kwargs["headers"] == {"Authorization": f"Bearer {KEY_CANARY}"}
    body = kwargs["json"]
    assert body["model"] == "gpt-5-mini"
    assert body["messages"] == [{"role": "user", "content": PROMPT_CANARY}]


def test_explicit_model_wins_over_catalog_default(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload("x"))
    _run(pq.ask("groq", "hi", model="llama-3.1-8b-instant", session=s))
    assert s.calls[0][2]["json"]["model"] == "llama-3.1-8b-instant"


def test_no_model_uses_catalog_default(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload("x"))
    _run(pq.ask("groq", "hi", session=s))
    assert s.calls[0][2]["json"]["model"] == \
        em_providers.default_model("groq")


# ─── ask(): Zen families ────────────────────────────────────────────────────

def test_zen_chat_family_uses_chat_completions(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload(ANSWER_CANARY))
    out = _run(pq.ask("opencode_zen", PROMPT_CANARY, model="kimi-k2.6",
                      session=s))
    assert out == ANSWER_CANARY
    _, url, kwargs = s.calls[0]
    assert url == "https://opencode.ai/inference/openai/v1/chat/completions"
    assert kwargs["json"]["model"] == "kimi-k2.6"
    assert kwargs["json"]["messages"] == \
        [{"role": "user", "content": PROMPT_CANARY}]


def test_zen_responses_family_uses_input_shape(_isolated, monkeypatch):
    _with_key(monkeypatch)
    payload = {"output": [{
        "type": "message",
        "content": [{"type": "output_text", "text": ANSWER_CANARY}],
    }]}
    s = _FakeSession(payload=payload)
    out = _run(pq.ask("opencode_zen", PROMPT_CANARY, model="gpt-5.5",
                      session=s))
    assert out == ANSWER_CANARY
    _, url, kwargs = s.calls[0]
    assert url == "https://opencode.ai/inference/openai/v1/responses"
    assert kwargs["json"]["model"] == "gpt-5.5"
    assert "messages" not in kwargs["json"]
    assert kwargs["json"]["input"] == \
        [{"role": "user", "content": PROMPT_CANARY}]


def test_zen_anthropic_family_parses_content_blocks(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(
        payload={"content": [{"type": "text", "text": ANSWER_CANARY}]})
    out = _run(pq.ask("opencode_zen", PROMPT_CANARY,
                      model="claude-sonnet-4-6", session=s))
    assert out == ANSWER_CANARY
    _, url, _ = s.calls[0]
    assert url == "https://opencode.ai/inference/anthropic/v1/messages"


# ─── ask(): errors ──────────────────────────────────────────────────────────

def test_key_unset_fails_without_touching_the_network(_isolated):
    s = _FakeSession(payload=_chat_payload("x"))
    with pytest.raises(pq.ProviderQueryError) as ei:
        _run(pq.ask("openai", "hi", session=s))
    assert ei.value.code == "no_provider_key"
    assert s.calls == []


def test_empty_prompt_fails_before_any_io(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload("x"))
    for bad in ("", "   "):
        with pytest.raises(pq.ProviderQueryError) as ei:
            _run(pq.ask("openai", bad, session=s))
        assert ei.value.code == "missing_field"
    assert s.calls == []


def test_native_only_providers_raise_instead_of_guessing(_isolated, monkeypatch):
    _with_key(monkeypatch)
    for pid in ("gemini", "claude", "nvidia"):
        s = _FakeSession(payload={})
        with pytest.raises(pq.ProviderQueryError) as ei:
            _run(pq.ask(pid, "hi", session=s))
        assert ei.value.code == "unsupported_operation", pid
        assert s.calls == [], pid


def test_unknown_provider_never_dials(_isolated):
    s = _FakeSession(payload={})
    with pytest.raises(pq.ProviderQueryError) as ei:
        _run(pq.ask("nope", "hi", session=s))
    assert ei.value.code == "unknown_provider"
    assert s.calls == []


def test_provider_401_maps_to_bad_key(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(status=401, payload={})
    with pytest.raises(pq.ProviderQueryError) as ei:
        _run(pq.ask("openai", "hi", session=s))
    assert ei.value.code == "bad_provider_key"


def test_transport_failure_maps_to_connection_failed(_isolated, monkeypatch):
    _with_key(monkeypatch)
    s = _FakeSession(exc=OSError("boom"))
    with pytest.raises(pq.ProviderQueryError) as ei:
        _run(pq.ask("openai", "hi", session=s))
    assert ei.value.code == "connection_failed"


# ─── ask(): log hygiene ─────────────────────────────────────────────────────

def test_logs_carry_id_model_latency_but_no_secrets(
        _isolated, monkeypatch, caplog):
    _with_key(monkeypatch)
    s = _FakeSession(payload=_chat_payload(ANSWER_CANARY))
    with caplog.at_level(logging.INFO, logger="echomuse.provider_query"):
        _run(pq.ask("openai", PROMPT_CANARY, model="gpt-5-mini", session=s))
    assert KEY_CANARY not in caplog.text
    assert PROMPT_CANARY not in caplog.text
    assert ANSWER_CANARY not in caplog.text
    assert "openai" in caplog.text and "gpt-5-mini" in caplog.text


# ─── route: POST /api/system/providers/ask ──────────────────────────────────

def test_ask_route_empty_prompt_is_400_shape(_isolated, _admin):
    for body in ({"id": "openai", "prompt": ""},
                 {"id": "openai", "prompt": "   "},
                 {"id": "openai"}):
        resp = _run(routes._post_providers_ask(FakeRequest(body)))
        assert resp.status == 400
        data = _body(resp)
        assert data["code"] == "missing_field"
        assert set(data) == {"error", "code"}


def test_ask_route_without_a_key_is_400_and_never_dials(
        _isolated, _admin, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not dial without a stored key")
    monkeypatch.setattr(pq, "ask", _boom)
    resp = _run(routes._post_providers_ask(
        FakeRequest({"id": "openai", "prompt": "hi"})))
    assert resp.status == 400
    assert _body(resp)["code"] == "no_provider_key"


def test_ask_route_returns_answer_and_uses_stored_key(
        _isolated, _admin, monkeypatch):
    pk.set_key("openai", KEY_CANARY)
    seen = {}

    async def _fake_ask(provider_id, prompt, model=None, timeout=30):
        seen["args"] = (provider_id, prompt, model)
        return ANSWER_CANARY

    monkeypatch.setattr(pq, "ask", _fake_ask)
    resp = _run(routes._post_providers_ask(
        FakeRequest({"id": "openai", "prompt": "hi"})))
    assert resp.status == 200
    assert _body(resp) == {"answer": ANSWER_CANARY}
    assert seen["args"][0] == "openai" and seen["args"][1] == "hi"
    assert KEY_CANARY not in resp.body.decode()


def test_ask_route_rejects_unknown_provider(_isolated, _admin):
    resp = _run(routes._post_providers_ask(
        FakeRequest({"id": "nope", "prompt": "hi"})))
    assert resp.status == 400
    assert _body(resp)["code"] == "unknown_provider"
