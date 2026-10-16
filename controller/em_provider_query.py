"""
em_provider_query.py — one-shot "ask a cloud provider" calls for BYOK chat.

ask() sends a single user prompt to the provider's chat endpoint and returns
the reply text. Key and model resolve the same way the providers routes do:
the key comes from em_provider_keys storage (never the request), and the
model is the explicit arg, else the stored per-provider override
(`provider_default_model_<id>` in system_config, the em_routes_providers
key), else the em_providers catalog default.

Routing: OpenCode Zen goes through em_providers' zen family helpers (chat vs
responses vs anthropic vs google shapes); the six OpenAI-compatible providers
(openai, groq, deepseek, openrouter, agentrouter, fluxion) share the plain
chat-completions shape against their catalog base_url. gemini/claude/nvidia
have no native request shape in em_providers, so asking through them raises
instead of guessing a wire format.

Privacy: prompts, answers, and keys are never logged — provider id, model,
and latency only. All transport is one-shot aiohttp with a short timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time

import em_provider_keys
import em_providers

log = logging.getLogger("echomuse.provider_query")

# Plain OpenAI chat-completions shape. Base URLs come from the catalog at call
# time — nothing is hardcoded here.
_OPENAI_CHAT_PROVIDERS = frozenset({
    "openai", "groq", "deepseek", "openrouter", "agentrouter", "fluxion",
})

# system_config key for a provider's default-model override, same key the
# providers routes read/write.
_DEFAULT_MODEL_KEY = "provider_default_model_{}"


class ProviderQueryError(Exception):
    """A failed ask(), carrying the {error, code} shape and HTTP status the
    route replies with."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


async def _stored_default(provider_id: str) -> str:
    """The admin's stored default-model override, or "" when none."""
    try:
        import em_db  # lazy: keeps import-time dependencies stdlib-only
        loop = asyncio.get_event_loop()
        value = await loop.run_in_executor(
            None, em_db.get_config, _DEFAULT_MODEL_KEY.format(provider_id), "")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def _text_from_parts(data: object) -> str:
    """Best-effort text extraction for the non-chat response shapes. Returns
    "" when nothing readable is present; the caller reports that."""
    texts: list[str] = []

    def _collect(node: object) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            for value in node.values():
                _collect(value)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    if isinstance(data, dict):
        shortcut = data.get("output_text")  # Responses API convenience field
        if isinstance(shortcut, str) and shortcut:
            return shortcut
        for section in ("output", "content", "candidates"):
            _collect(data.get(section))
    return "".join(texts)


def _reply_text(provider_id: str, family: str | None, data: object) -> str:
    """Pull the reply text out of a decoded provider response."""
    if not isinstance(data, dict):
        raise ProviderQueryError(
            "provider_error", "Provider returned an unreadable reply", 502)
    if provider_id == "opencode_zen" and family == "responses":
        text = _text_from_parts(data)
    elif provider_id == "opencode_zen" and family in ("anthropic", "google"):
        text = _text_from_parts(data)
    else:  # plain chat-completions shape (the six, and Zen openai-chat)
        text = ""
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = ""
        if not isinstance(text, str):
            text = ""
    if not text:
        raise ProviderQueryError(
            "provider_error", "Provider returned no reply text", 502)
    return text


async def ask(provider_id: str, prompt: str, model: str | None = None,
              timeout: float = 30, session: object | None = None) -> str:
    """Send one prompt to a provider and return the reply text.

    session is injectable for tests (a fake with .post returning an
    async-context-manager response with .status and .json()); omitted, a
    short-timeout aiohttp session is opened and closed for the single call.
    """
    if provider_id not in em_providers.PROVIDERS:
        raise ProviderQueryError(
            "unknown_provider", f"Unknown provider: {provider_id}", 400)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProviderQueryError(
            "missing_field", "Missing or empty required field: prompt", 400)
    if provider_id != "opencode_zen" \
            and provider_id not in _OPENAI_CHAT_PROVIDERS:
        raise ProviderQueryError(
            "unsupported_operation",
            f"Provider {provider_id} has no chat query shape; "
            "asking through it is not supported", 400)

    key = em_provider_keys.get_key(provider_id)
    if not key:
        raise ProviderQueryError(
            "no_provider_key", f"No API key stored for {provider_id}", 400)

    if isinstance(model, str) and model.strip():
        model = model.strip()
    else:
        model = await _stored_default(provider_id) \
            or em_providers.default_model(provider_id)

    family: str | None = None
    if provider_id == "opencode_zen":
        family = em_providers.zen_family_for(model)
        path, body = em_providers.zen_request(
            model, [{"role": "user", "content": prompt}])
        # zen_request's google shape carries a fixed "ping" probe text (it
        # exists for Test Connection); a real question must replace it.
        if family == "google":
            body = {"contents": [{"parts": [{"text": prompt}]}]}
        url = f"{em_providers.PROVIDERS[provider_id]['base_url']}/{path}"
    else:
        url = f"{em_providers.PROVIDERS[provider_id]['base_url']}/chat/completions"
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}]}

    headers = {"Authorization": f"Bearer {key}"}
    own_session = session is None
    if own_session:
        import aiohttp  # lazy: catalog use never needs the dependency

        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout))

    start = time.monotonic()
    try:
        try:
            async with session.post(url, headers=headers, json=body) as resp:
                status = resp.status
                data = await resp.json()
        except ProviderQueryError:
            raise
        except Exception as exc:  # transport failure, DNS, timeout, ...
            raise ProviderQueryError(
                "connection_failed", f"connection failed: {exc}", 502)
    finally:
        if own_session:
            await session.close()

    if status in (401, 403):
        raise ProviderQueryError(
            "bad_provider_key",
            f"invalid API key (provider returned {status})", 502)
    if status < 200 or status >= 300:
        raise ProviderQueryError(
            "provider_error", f"provider returned {status}", 502)

    answer = _reply_text(provider_id, family, data)
    # Prompt, answer, and key never appear here: id + model + latency only.
    log.info("Provider ask for %s model %s took %.2fs",
             provider_id, model, time.monotonic() - start)
    return answer
