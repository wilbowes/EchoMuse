"""
BYOK cloud-provider catalog: who we can route chat completions through, what
their keys look like, and whether a key works.

Ten providers, IDs fixed (other workers key off them): gemini, groq, openai,
claude, deepseek, nvidia, openrouter, agentrouter, fluxion, opencode_zen.

Key validation is deliberately weak: only the five distinctive prefixes
(gemini AIza, groq gsk_, claude sk-ant-, nvidia nvapi-, openrouter sk-or-v1-)
are enforced. openai/deepseek/agentrouter share the generic sk- shape and
fluxion / zen have no documented format, so those accept any non-empty string
— a wrong guess here locks out a paying user, while a wrong accept just fails
at Test Connection, which is the real check.

OpenCode Zen is not one OpenAI base: four families live under
https://opencode.ai/inference, all with `Authorization: Bearer <token>`:

    open chat models -> POST /inference/openai/v1/chat/completions
                        {"model": bare-slug, "messages": [...]}
    GPT / Grok / Muse-Spark -> POST /inference/openai/v1/responses
                        {"model", "input"} (Responses shape, NOT messages)
    Claude family -> POST /inference/anthropic/v1/messages
                        {"model", "max_tokens", "messages"}
    Gemini family -> POST /inference/google/v1beta/models/{model}:generateContent

Documented Zen defaults route as: open chat kimi-k2.6, glm-5.1, minimax-m2.7,
deepseek-v4-pro, mistral-large-4, qwen3.8-max; responses gpt-5.5, grok-4.7,
muse-spark-1.3; anthropic claude-sonnet-4-6; google gemini-3.1-pro. Routing
below is by slug prefix so future same-family models follow automatically,
except five documented Qwen slugs (qwen3.8-flash, qwen3.7-max, qwen3.7-plus,
qwen3.6-plus, qwen3.5-plus) that the Zen table routes to the anthropic
endpoint while qwen3.8-max stays on chat completions.

GET /inference/v1/models is PUBLIC (no auth), so Test Connection for Zen must
never use it for key validation: it posts a minimal authed call instead, reads
401 as invalid-key, and treats anything else non-2xx as a transport issue.

Pure stdlib at import time; aiohttp is imported lazily inside test_connection
so the catalog and validation work wherever the controller imports.
"""

from __future__ import annotations

PROVIDERS: dict = {
    "gemini": {
        "label": "Gemini",
        "key_prefixes": ["AIza"],
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "groq": {
        "label": "Groq",
        "key_prefixes": ["gsk_"],
        # llama-3.3-70b-versatile was decommissioned Aug 2026; the gpt-oss
        # pair is Groq's featured/production chat lineup.
        "default_model": "openai/gpt-oss-120b",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openai": {
        "label": "OpenAI",
        "key_prefixes": ["sk-"],
        "default_model": "gpt-5-mini",
        "models": ["gpt-5-mini", "gpt-5.5", "gpt-4o-mini"],
        "base_url": "https://api.openai.com/v1",
    },
    "claude": {
        "label": "Claude",
        "key_prefixes": ["sk-ant-"],
        "default_model": "claude-sonnet-4-6",
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
        "base_url": "https://api.anthropic.com/v1",
    },
    "deepseek": {
        "label": "DeepSeek",
        "key_prefixes": ["sk-"],
        "default_model": "deepseek-flash",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        # No /v1: docs base is https://api.deepseek.com, i.e. {base}/chat/completions.
        "base_url": "https://api.deepseek.com",
    },
    "nvidia": {
        "label": "NVIDIA",
        "key_prefixes": ["nvapi-"],
        "default_model": "meta/llama-3.3-70b-instruct",
        "models": [
            "meta/llama-3.3-70b-instruct",
            "nvidia/llama-3.1-nemotron-70b-instruct",
        ],
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_prefixes": ["sk-or-v1-"],
        "default_model": "anthropic/claude-sonnet-4",
        "models": [
            "anthropic/claude-sonnet-4",
            "openai/gpt-4o",
            "google/gemini-2.5-flash",
        ],
        "base_url": "https://openrouter.ai/api/v1",
    },
    "agentrouter": {
        "label": "AgentRouter",
        # Anthropic-compatible Claude relay at agentrouter.org (not
        # api.agentrouter.ai); keys are sk-... (generic shape, unenforced).
        "key_prefixes": ["sk-"],
        "default_model": "claude-opus-4-6",
        "models": [
            "claude-opus-4-6",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
        ],
        "base_url": "https://agentrouter.org/v1",
    },
    "fluxion": {
        "label": "Fluxion",
        "key_prefixes": [],
        "default_model": "fluxion-large",
        "models": ["fluxion-large", "fluxion-medium"],
        "base_url": "https://api.fluxion.ai/v1",
    },
    "opencode_zen": {
        "label": "OpenCode Zen",
        "key_prefixes": [],
        "default_model": "kimi-k2.6",
        "models": [
            "kimi-k2.6",
            "glm-5.1",
            "minimax-m2.7",
            "deepseek-v4-pro",
            "mistral-large-4",
            "qwen3.8-max",
            "gpt-5.5",
            "grok-4.7",
            "muse-spark-1.3",
            "claude-sonnet-4-6",
            "gemini-3.1-pro",
        ],
        # Family root, not a chat base: zen_request() appends the per-family path.
        "base_url": "https://opencode.ai/inference",
    },
}

# Only these reject a key outright; everything else accepts any non-empty
# string (see module docstring for why).
_DISTINCTIVE_PREFIXES = {
    "gemini": ("AIza",),
    "groq": ("gsk_",),
    "claude": ("sk-ant-",),
    "nvidia": ("nvapi-",),
    "openrouter": ("sk-or-v1-",),
}

# Short, one-shot: matches the total=10 pattern in em_releases.py.
_TEST_TIMEOUT_S = 10

# The Zen table routes these Qwen slugs to the anthropic endpoint while other
# qwen* slugs stay on chat completions; exact set so future qwen slugs still
# default to openai-chat.
_ZEN_ANTHROPIC_QWEN = frozenset({
    "qwen3.8-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
})


def validate_key(provider_id: str, key: object) -> str | None:
    """Error message when the key is unusable, else None (key looks fine)."""
    if provider_id not in PROVIDERS:
        return f"unknown provider: {provider_id}"
    if not isinstance(key, str) or not key.strip():
        return "API key is empty"
    prefixes = _DISTINCTIVE_PREFIXES.get(provider_id, ())
    if prefixes and not key.startswith(prefixes):
        want = " or ".join(prefixes)
        return f"API key should start with {want!r}"
    return None


def models_for(provider_id: str) -> list[str]:
    """Known model slugs for a provider ([] when unknown)."""
    spec = PROVIDERS.get(provider_id)
    return list(spec["models"]) if spec else []


def default_model(provider_id: str) -> str:
    """Default model slug for a provider ("" when unknown)."""
    spec = PROVIDERS.get(provider_id)
    return spec["default_model"] if spec else ""


def zen_family_for(model: str) -> str:
    """Which Zen family a model slug belongs to: openai-chat, responses,
    anthropic, or google. Prefix-based so new same-family slugs just work."""
    slug = model.lower()
    if slug.startswith(("gpt-", "grok-", "muse-spark-")):
        return "responses"
    if slug in _ZEN_ANTHROPIC_QWEN:
        return "anthropic"
    if slug.startswith("claude-"):
        return "anthropic"
    if slug.startswith("gemini-"):
        return "google"
    return "openai-chat"


def zen_request(model: str, messages: list | None = None) -> tuple[str, dict]:
    """(path, json-body) for a minimal Zen chat call, relative to the Zen
    base_url. Path picks the model's family; body is the family's shape."""
    msgs = messages if messages is not None else [{"role": "user", "content": "ping"}]
    family = zen_family_for(model)
    if family == "responses":
        return "openai/v1/responses", {"model": model, "input": msgs}
    if family == "anthropic":
        return ("anthropic/v1/messages",
                {"model": model, "max_tokens": 16, "messages": msgs})
    if family == "google":
        return (f"google/v1beta/models/{model}:generateContent",
                {"contents": [{"parts": [{"text": "ping"}]}]})
    return "openai/v1/chat/completions", {"model": model, "messages": msgs}


async def test_connection(provider_id: str, key: str,
                           session: object | None = None) -> tuple[bool, str]:
    """One-shot reachability + key check: (True, msg) or (False, reason).

    session is injectable for tests (a fake with .get/.post returning an
    async-context-manager response with .status); omitted, a short-timeout
    aiohttp session is opened and closed for the single call. Never hits the
    network with a fake, and never touches Zen's public /models listing.
    """
    if provider_id not in PROVIDERS:
        return False, f"unknown provider: {provider_id}"
    err = validate_key(provider_id, key)
    if err is not None:
        return False, err

    if provider_id == "claude":
        # Anthropic authenticates with x-api-key, not Bearer: a Bearer
        # probe always reads 401, so Test Connection could never pass.
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}
    own_session = session is None
    if own_session:
        import aiohttp  # lazy: catalog use never needs the dependency

        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TEST_TIMEOUT_S))

    try:
        if provider_id == "opencode_zen":
            # The /v1/models listing is public, so it says nothing about the
            # key: post a minimal authed call and read the status instead.
            path, body = zen_request(default_model(provider_id))
            url = f"{PROVIDERS[provider_id]['base_url']}/{path}"
            async with session.post(url, headers=headers, json=body) as resp:
                status = resp.status
        else:
            url = f"{PROVIDERS[provider_id]['base_url']}/models"
            async with session.get(url, headers=headers) as resp:
                status = resp.status
    except Exception as exc:  # transport failure, proxy, DNS, timeout, ...
        return False, f"connection failed: {exc}"
    finally:
        if own_session:
            await session.close()

    if status == 200:
        return True, "connection ok"
    if status in (401, 403):
        return False, f"invalid API key (provider returned {status})"
    return False, f"provider returned {status}; key not verified"
