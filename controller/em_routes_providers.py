"""
Cloud provider routes: which cloud providers are configured, their stored
API keys (presence only — values never leave the server), and per-provider
default models.

Key storage itself lives in em_provider_keys (0600 file beside the DB);
per-provider default-model overrides live in system_config as
`provider_default_model_<id>`, falling back to em_providers' built-ins.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_provider_keys
import em_provider_query
import em_providers

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/system/providers", _get_providers)
    app.router.add_patch("/api/system/providers", _patch_providers)
    app.router.add_post("/api/system/providers/test", _post_providers_test)
    app.router.add_post("/api/system/providers/ask", _post_providers_ask)


# system_config key holding a provider's default-model override, on top of
# em_providers' built-in. No db.delete_config exists, so "" clears it and
# every read treats "" as absent.
_DEFAULT_MODEL_KEY = "provider_default_model_{}"


async def _listing(loop: asyncio.AbstractEventLoop) -> list[dict]:
    """The GET response body, shared with PATCH's reply. Key presence only."""
    masked = await loop.run_in_executor(None, em_provider_keys.masked_list)
    config = await loop.run_in_executor(None, db.get_all_config)
    return [{
        "id": pid,
        "label": spec.get("label", pid),
        "key_set": bool(masked.get(pid, False)),
        "models": em_providers.models_for(pid),
        "default_model": (
            config.get(_DEFAULT_MODEL_KEY.format(pid))
            or em_providers.default_model(pid)
        ),
    } for pid, spec in em_providers.PROVIDERS.items()]


@auth.require_admin
async def _get_providers(request: web.Request) -> web.Response:
    """GET /api/system/providers — catalog with key presence, never values."""
    return em_http.ok({"providers": await _listing(asyncio.get_event_loop())})


@auth.require_admin
async def _patch_providers(request: web.Request) -> web.Response:
    """
    PATCH /api/system/providers

    Body: {"providers": [{"id", "key", "default_model"}]}. `key` is a new
    plaintext key, SENTINEL ("__unchanged__") to keep the stored one, or ""
    to delete it; absent/null also keeps. `default_model` must be one of
    the provider's known models, "" clears the override. Absent/null keeps.

    Validates everything before writing anything, so a bad entry does not
    leave half the list applied.
    """
    body = await em_http.json_body(request)
    entries = body.get("providers")
    if not isinstance(entries, list):
        return em_http.error(
            "missing_field", "Missing or invalid required field: providers", 400)

    sentinel = em_provider_keys.SENTINEL
    plans = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            return em_http.error(
                "missing_field", "Each provider entry needs a string id", 400)
        pid = entry["id"]
        if pid not in em_providers.PROVIDERS:
            return em_http.error(
                "unknown_provider", f"Unknown provider: {pid}", 400)
        key = entry.get("key", sentinel)
        if key is None:
            key = sentinel
        if isinstance(key, str):
            stripped = key.strip()
            key = sentinel if stripped == sentinel else stripped
        if key != sentinel and key != "":
            err = em_providers.validate_key(pid, key)
            if err is not None:
                return em_http.error("bad_provider_key", err, 400)
        model = entry.get("default_model")
        if model is not None and model != "":
            if not isinstance(model, str) or model not in em_providers.models_for(pid):
                return em_http.error(
                    "bad_default_model",
                    f"Unknown model {model!r} for provider {pid}", 400)
        plans.append((pid, key, model))

    loop = asyncio.get_event_loop()
    for pid, key, model in plans:
        await loop.run_in_executor(None, em_provider_keys.resolve_write, pid, key)
        if model is not None:
            await loop.run_in_executor(
                None, db.set_config,
                _DEFAULT_MODEL_KEY.format(pid), model if model else "")

    return em_http.ok({"providers": await _listing(loop)})


@auth.require_admin
async def _post_providers_test(request: web.Request) -> web.Response:
    """
    POST /api/system/providers/test — body {"id"}. Dials the provider with
    the STORED key (the request carries no key material) and reports
    {"ok", "message"}. No stored key is a plain ok:false, not an error.
    """
    body = await em_http.json_body(request)
    pid = body.get("id")
    if not isinstance(pid, str) or not pid.strip():
        return em_http.error(
            "missing_field", "Missing or empty required field: id", 400)
    pid = pid.strip()
    if pid not in em_providers.PROVIDERS:
        return em_http.error(
            "unknown_provider", f"Unknown provider: {pid}", 400)
    loop = asyncio.get_event_loop()
    key = await loop.run_in_executor(None, em_provider_keys.get_key, pid)
    if not key:
        return em_http.ok({"ok": False, "message": f"No API key stored for {pid}"})
    # Network call — awaited directly, never in the DB executor. Logs the
    # outcome only, never the key.
    ok, message = await em_providers.test_connection(pid, key)
    log.info("Provider connection test for %s: %s", pid, "ok" if ok else "failed")
    return em_http.ok({"ok": ok, "message": message})


@auth.require_admin
async def _post_providers_ask(request: web.Request) -> web.Response:
    """
    POST /api/system/providers/ask — body {"id", "prompt", "model"?}.
    Asks the provider one question with the STORED key (the request carries
    no key material) and reports {"answer"}. An empty prompt is a 400; no
    stored key is a 400; provider failures map to {error, code}.
    """
    body = await em_http.json_body(request)
    pid = body.get("id")
    if not isinstance(pid, str) or not pid.strip():
        return em_http.error(
            "missing_field", "Missing or empty required field: id", 400)
    pid = pid.strip()
    if pid not in em_providers.PROVIDERS:
        return em_http.error(
            "unknown_provider", f"Unknown provider: {pid}", 400)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return em_http.error(
            "missing_field", "Missing or empty required field: prompt", 400)
    model = body.get("model")
    if model is None or (isinstance(model, str) and not model.strip()):
        model = None
    elif not isinstance(model, str):
        return em_http.error(
            "bad_model", "Field model must be a string", 400)
    else:
        model = model.strip()

    loop = asyncio.get_event_loop()
    key = await loop.run_in_executor(None, em_provider_keys.get_key, pid)
    if not key:
        return em_http.error(
            "no_provider_key", f"No API key stored for {pid}", 400)
    if model is None:
        stored = await loop.run_in_executor(
            None, db.get_config, _DEFAULT_MODEL_KEY.format(pid), "")
        model = stored if isinstance(stored, str) and stored \
            else em_providers.default_model(pid)
    # Network call — awaited directly, never in the DB executor. ask() logs
    # provider id + model + latency only, never prompt, answer, or key.
    try:
        answer = await em_provider_query.ask(pid, prompt.strip(), model=model)
    except em_provider_query.ProviderQueryError as exc:
        return em_http.error(exc.code, exc.message, exc.status)
    return em_http.ok({"answer": answer})
