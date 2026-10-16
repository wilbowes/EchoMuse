"""
em_providers: catalog shape, weak-only key validation, Zen family routing,
and Test Connection semantics (401 means bad key, anything else non-2xx is
transport). All transport is faked — nothing here touches the network, and no
real keys appear anywhere.
"""
import asyncio

import em_providers


# --- catalog ---

def test_all_ten_provider_ids_present_with_required_fields():
    assert set(em_providers.PROVIDERS) == {
        "gemini", "groq", "openai", "claude", "deepseek", "nvidia",
        "openrouter", "agentrouter", "fluxion", "opencode_zen",
    }
    for pid, spec in em_providers.PROVIDERS.items():
        for field in ("label", "default_model", "models", "base_url"):
            assert spec[field], (pid, field)
        assert spec["default_model"] in spec["models"], pid


def test_models_for_returns_a_copy_and_default_model_matches():
    for pid in em_providers.PROVIDERS:
        models = em_providers.models_for(pid)
        assert models == em_providers.PROVIDERS[pid]["models"]
        assert em_providers.default_model(pid) == em_providers.PROVIDERS[pid]["default_model"]
    models = em_providers.models_for("gemini")
    models.append("mutated")
    assert "mutated" not in em_providers.models_for("gemini")


def test_unknown_provider_helpers():
    assert em_providers.validate_key("nope", "k") == "unknown provider: nope"
    assert em_providers.models_for("nope") == []
    assert em_providers.default_model("nope") == ""


# --- validation ---

def test_distinctive_prefixes_accepted():
    good = {
        "gemini": "AIzaABC",
        "groq": "gsk_ABC",
        "claude": "sk-ant-ABC",
        "nvidia": "nvapi-ABC",
        "openrouter": "sk-or-v1-ABC",
    }
    for pid, key in good.items():
        assert em_providers.validate_key(pid, key) is None, pid


def test_distinctive_prefixes_reject_wrong_shape():
    for pid in ("gemini", "groq", "claude", "nvidia", "openrouter"):
        err = em_providers.validate_key(pid, "bogus-key")
        assert err is not None and "should start with" in err, pid


def test_weak_and_unknown_formats_accept_any_non_empty_string():
    for pid in ("openai", "deepseek", "agentrouter", "fluxion", "opencode_zen"):
        assert em_providers.validate_key(pid, "whatever-abc-123") is None, pid
        assert em_providers.validate_key(pid, "sk-anything") is None, pid


def test_empty_keys_rejected_everywhere():
    for pid in em_providers.PROVIDERS:
        for bad in ("", "   ", None, 123):
            assert em_providers.validate_key(pid, bad) == "API key is empty", (pid, bad)


# --- Zen routing ---

def test_zen_open_chat_models_use_chat_completions_with_bare_slug():
    for model in ("kimi-k2.6", "glm-5.1", "minimax-m2.7", "deepseek-v4-pro",
                  "mistral-large-4", "qwen3.8-max", "some-future-chat-model"):
        assert em_providers.zen_family_for(model) == "openai-chat", model
        path, body = em_providers.zen_request(model)
        assert path == "openai/v1/chat/completions", model
        assert body["model"] == model and "messages" in body, model
        assert "input" not in body, model


def test_zen_gpt_grok_spark_use_responses_shape():
    for model in ("gpt-5.5", "gpt-whatever", "grok-4.7", "muse-spark-1.3"):
        assert em_providers.zen_family_for(model) == "responses", model
        path, body = em_providers.zen_request(model)
        assert path == "openai/v1/responses", model
        assert body["model"] == model and "input" in body, model
        assert "messages" not in body, model


def test_zen_claude_uses_anthropic_messages_shape():
    assert em_providers.zen_family_for("claude-sonnet-4-6") == "anthropic"
    assert em_providers.zen_family_for("claude-opus-99") == "anthropic"
    path, body = em_providers.zen_request("claude-sonnet-4-6")
    assert path == "anthropic/v1/messages"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["max_tokens"] and "messages" in body


def test_zen_gemini_uses_generate_content_path():
    assert em_providers.zen_family_for("gemini-3.1-pro") == "google"
    path, body = em_providers.zen_request("gemini-3.1-pro")
    assert path == "google/v1beta/models/gemini-3.1-pro:generateContent"
    assert "contents" in body


def test_zen_anthropic_routed_qwen_slugs_use_messages_shape():
    for model in ("qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
                  "qwen3.6-plus", "qwen3.5-plus"):
        assert em_providers.zen_family_for(model) == "anthropic", model
        path, body = em_providers.zen_request(model)
        assert path == "anthropic/v1/messages", model
        assert body["model"] == model and "messages" in body, model


def test_zen_other_qwen_slugs_stay_on_chat_completions():
    assert em_providers.zen_family_for("qwen3.8-max") == "openai-chat"
    assert em_providers.zen_family_for("some-future-qwen-model") == "openai-chat"


def test_docs_verified_bases_models_and_defaults():
    assert (em_providers.PROVIDERS["deepseek"]["base_url"]
            == "https://api.deepseek.com")
    assert (em_providers.PROVIDERS["agentrouter"]["base_url"]
            == "https://agentrouter.org/v1")
    assert em_providers.PROVIDERS["groq"]["models"] == [
        "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert em_providers.default_model("groq") == "openai/gpt-oss-120b"
    assert em_providers.default_model("deepseek") == "deepseek-flash"
    assert em_providers.default_model("agentrouter") == "claude-opus-4-6"


def test_agentrouter_documents_generic_sk_prefix_without_enforcing():
    assert em_providers.PROVIDERS["agentrouter"]["key_prefixes"] == ["sk-"]
    assert em_providers.validate_key("agentrouter", "whatever-abc-123") is None


# --- Test Connection with mocked transport ---

class _FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for aiohttp: scripted status, explodes, records calls."""
    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.status)

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


def _run(coro):
    return asyncio.run(coro)


def test_connection_ok_on_200_with_bearer_auth():
    s = _FakeSession(status=200)
    ok, _ = _run(em_providers.test_connection("openai", "sk-fake", session=s))
    assert ok is True
    assert len(s.calls) == 1
    _, url, kwargs = s.calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert kwargs["headers"] == {"Authorization": "Bearer sk-fake"}


def test_connection_claude_uses_x_api_key_not_bearer():
    s = _FakeSession(status=200)
    ok, _ = _run(em_providers.test_connection("claude", "sk-ant-fake",
                                              session=s))
    assert ok is True
    assert len(s.calls) == 1
    _, url, kwargs = s.calls[0]
    assert url == "https://api.anthropic.com/v1/models"
    assert kwargs["headers"] == {"x-api-key": "sk-ant-fake",
                                 "anthropic-version": "2023-06-01"}
    assert "Authorization" not in kwargs["headers"]


def test_connection_401_means_invalid_key():
    for pid, key in (("openai", "sk-fake"), ("groq", "gsk_fake"),
                     ("opencode_zen", "zen-fake-key")):
        s = _FakeSession(status=401)
        ok, msg = _run(em_providers.test_connection(pid, key, session=s))
        assert ok is False and "invalid API key" in msg, (pid, msg)


def test_connection_non_auth_status_is_transport_not_invalid_key():
    s = _FakeSession(status=500)
    ok, msg = _run(em_providers.test_connection("openai", "sk-fake", session=s))
    assert ok is False
    assert "invalid" not in msg.lower()


def test_connection_transport_exception_reports_connection_failed():
    s = _FakeSession(exc=OSError("boom"))
    ok, msg = _run(em_providers.test_connection("openai", "sk-fake", session=s))
    assert ok is False and "connection failed" in msg


def test_connection_rejects_bad_key_before_any_http():
    s = _FakeSession(status=200)
    ok, msg = _run(em_providers.test_connection("groq", "not-a-groq-key", session=s))
    assert ok is False and s.calls == []


def test_connection_unknown_provider_never_dials():
    s = _FakeSession(status=200)
    ok, msg = _run(em_providers.test_connection("nope", "k", session=s))
    assert ok is False and "unknown provider" in msg and s.calls == []


def test_zen_connection_never_touches_public_models_listing():
    s = _FakeSession(status=200)
    ok, _ = _run(em_providers.test_connection("opencode_zen", "zen-fake-key", session=s))
    assert ok is True
    assert len(s.calls) == 1
    method, url, kwargs = s.calls[0]
    assert method == "POST"
    assert url == ("https://opencode.ai/inference/openai/v1/chat/completions")
    assert "/v1/models" not in url
    assert kwargs["headers"] == {"Authorization": "Bearer zen-fake-key"}
    assert kwargs["json"]["model"] == em_providers.default_model("opencode_zen")


def test_zen_connection_500_is_transport_issue():
    s = _FakeSession(status=500)
    ok, msg = _run(em_providers.test_connection("opencode_zen", "zen-fake-key", session=s))
    assert ok is False
    assert "invalid" not in msg.lower()
