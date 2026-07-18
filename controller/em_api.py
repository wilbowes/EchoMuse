"""
em_api.py — EchoMuse Controller HTTP API + Dashboard
=====================================================

aiohttp web application running in the same asyncio event loop as the
WebSocket controller. Serves:

  /                         — dashboard SPA (static/index.html)
  /setup                    — first-run admin account creation
  /api/auth/*               — login, logout, current user
  /api/devices/*            — fleet management, config, logs, OTA
  /api/releases/*           — GitHub release tracking and deployment
  /api/system/*             — controller status and config
  WS /api/events            — live push: device state, logs, pending
  WS /api/devices/{id}/shell — proxied root shell on device

Path routing is handled by the existing websockets router in
em_controller.py — aiohttp handles /api/* and /, websockets handles
/control, /data, and /shell/{device_id}.

Usage (from em_controller.py main()):
    import em_api
    runner = await em_api.create_runner(devices_ref)
    await runner.setup()
    site = web.TCPSite(runner, host, port + 1)   # or same port via middleware
    await site.start()
    ...
    await runner.cleanup()

The _devices dict reference is passed in so the API can merge live
state with persisted DB state without coupling to a global.
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3 as _sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
import websockets

import em_db as db
import em_auth as auth
import em_ble_proxy
import em_pki
import em_scenes
from version import VERSION as CONTROLLER_VERSION

log = logging.getLogger("echomuse.api")

# ─── Config ───────────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
# List endpoint, not /releases/latest: device firmware releases (v* tags with
# a `server` asset) share the repo with controller releases (controller-v*
# tags, GHCR image only). /releases/latest returns whichever was published
# most recently — _fetch_latest_release filters the list for the newest
# release that is actually a device firmware release.
GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases?per_page=10"

# How long to cache GitHub release info in memory (seconds).
# DB is the persistent cache; this avoids hitting the DB on every
# /api/releases/latest request.
_release_cache: dict = {}
_release_cache_ts: float = 0.0
RELEASE_CACHE_TTL = 60  # seconds

# Reference to the live devices dict from em_controller — set by init().
_devices: dict = {}

# Device-link TLS material directory — set by em_controller.main() once
# em_pki.ensure_pki() succeeds. None = TLS listener not running (no
# cryptography package / setup failure); credential endpoints then 503.
_tls_dir: str | None = None


def set_tls_dir(tls_dir: str) -> None:
    global _tls_dir
    _tls_dir = tls_dir

# Set of connected /api/events WebSocket clients.
_event_clients: set[web.WebSocketResponse] = set()

# Track in-progress OTA updates per device_id to enforce one-at-a-time.
_updates_in_progress: set[str] = set()

# Last OTA failure per device, surfaced as `update_error` in /api/devices so
# the dashboard (fleet deploy modal + per-device update log) can show *why* a
# tile stopped progressing instead of sitting at "updating…" forever. Set by
# _update_failed on every _run_update/_run_rollback failure path; cleared when
# a new update starts and on confirmed success. In-memory by design — a
# controller restart clears stale errors along with the update tasks
# themselves.
_update_errors: dict[str, str] = {}

# Pending local binary uploads — keyed by UUID token, expire after 10 minutes.
_pending_uploads: dict[str, bytes] = {}

# WiFi change state per device_id — {"pending": {...}|None, "last_result":
# {...}|None}. Deliberately NOT on the live Device object: the connection
# (and with it the Device) dies when the network switches, and the outcome
# arrives on the replacement connection. In-memory only — a controller
# restart mid-change just means the result event is lost, not the change
# itself (the device self-manages commit/rollback).
_wifi_states: dict[str, dict] = {}

# A change whose result never arrived (device bricked its network AND
# rollback failed, or controller restarted) must not block retries forever.
_WIFI_PENDING_TTL = 240  # device gates total ≤ ~135s + margin


def wifi_state(device_id: str) -> dict:
    """Current wifi change state for a device, with stale pending expiry."""
    st = _wifi_states.setdefault(device_id, {"pending": None, "last_result": None})
    pending = st.get("pending")
    if pending and time.time() - pending["started_at"] > _WIFI_PENDING_TTL:
        st["pending"] = None
        st["last_result"] = {
            "ok": False, "ssid": pending["ssid"],
            "error": "no result from device — change timed out (device may "
                     "be offline, or its rollback failed)",
            "at": time.time(),
        }
    return st


def wifi_record_result(device_id: str, ok: bool, ssid: str, error: str
                       ) -> tuple[dict, bool]:
    """
    Store a wifi_result reported by the device.

    Returns (state, duplicate). The device re-sends its result until the
    wifi_commit ack lands, so re-arrivals of the same outcome are flagged
    (duplicate=True) and don't refresh the timestamp — callers ack every
    arrival but log/record only the first.
    """
    st = wifi_state(device_id)
    last = st.get("last_result")
    if (last and last.get("ok") == ok and last.get("ssid") == ssid
            and last.get("error") == error and st.get("pending") is None):
        return st, True
    st["pending"] = None
    st["last_result"] = {"ok": ok, "ssid": ssid, "error": error, "at": time.time()}
    return st, False

# ─── Initialisation ───────────────────────────────────────────────────────────

_shell_pending:   dict = {}
_shell_dashboard: dict = {}
_shell_ws:        dict = {}   # device_id → live ws for programmatic sessions
_shell_lock:      dict = {}   # device_id → asyncio.Lock (one session at a time)

def init(devices_ref: dict, shell_pending_ref: dict, shell_dashboard_ref: dict) -> None:
    """
    Bind live shared state from em_controller.

    Must be called before create_app().
    """
    global _devices, _shell_pending, _shell_dashboard
    _devices         = devices_ref
    _shell_pending   = shell_pending_ref
    _shell_dashboard = shell_dashboard_ref


async def create_app() -> web.Application:
    """
    Build and return the aiohttp Application.

    Routes are registered here. The app is not started — the caller
    creates an AppRunner and TCPSite.
    """
    app = web.Application(middlewares=[_error_middleware])

    # Static / setup
    app.router.add_get("/",           _serve_spa)
    # /setup predates the state-aware landing page — / now shows the
    # first-run form itself when setup is pending, so just send people there.
    app.router.add_get("/setup",      _redirect_root)
    app.router.add_get("/dashboard",  _serve_dashboard)
    app.router.add_static("/static",  STATIC_DIR)
    app.router.add_post("/api/setup", _post_setup)
    # Public (pre-auth) — the landing page needs to know which form to show.
    # Exposes only the boolean; the bootstrap token itself stays in the logs.
    app.router.add_get("/api/system/setup-state", _get_setup_state)

    # Auth
    app.router.add_post("/api/auth/login",           _post_login)
    app.router.add_post("/api/auth/logout",          _post_logout)
    app.router.add_get("/api/auth/me",               _get_me)
    app.router.add_post("/api/auth/change-password", _post_change_password)

    # Devices — order matters: specific paths before parameterised ones
    app.router.add_get("/api/devices",                    _get_devices)
    app.router.add_get("/api/devices/pending",            _get_pending)
    app.router.add_get("/api/devices/{id}",               _get_device)
    app.router.add_patch("/api/devices/{id}",             _patch_device)
    app.router.add_delete("/api/devices/{id}",            _delete_device)
    app.router.add_post("/api/devices/{id}/approve",      _post_approve)
    app.router.add_get("/api/devices/{id}/config",        _get_device_config)
    app.router.add_post("/api/devices/{id}/config",       _post_device_config)
    app.router.add_get("/api/devices/{id}/logs",          _get_device_logs)
    app.router.add_get("/api/devices/{id}/turns",         _get_device_turns)
    app.router.add_get("/api/devices/{id}/activity",      _get_device_activity)
    app.router.add_post("/api/devices/{id}/wifi",         _post_device_wifi)
    app.router.add_post("/api/devices/{id}/wifi/scan",    _post_device_wifi_scan)
    app.router.add_post("/api/devices/{id}/update",       _post_device_update)
    app.router.add_post("/api/devices/{id}/rollback",     _post_device_rollback)
    app.router.add_post("/api/releases/upload",           _post_upload_binary)
    app.router.add_get("/api/devices/{id}/shell",         _ws_shell)

    # Releases
    app.router.add_get("/api/releases/latest",   _get_latest_release)
    app.router.add_post("/api/releases/check",   _post_check_release)
    app.router.add_post("/api/releases/deploy",  _post_deploy_all)

    # Global device config
    app.router.add_get("/api/global/config",   _get_global_config)
    app.router.add_post("/api/global/config",  _post_global_config)

    # System
    app.router.add_get("/api/system/status",    _get_system_status)
    app.router.add_get("/api/system/config",    _get_system_config)
    app.router.add_patch("/api/system/config",  _patch_system_config)

    # Provisioning
    app.router.add_get("/api/provision/start_script", _get_provision_start_script)
    app.router.add_get("/api/provision/debloat_script",   _get_provision_debloat_script)
    app.router.add_get("/api/provision/debloat_packages", _get_provision_debloat_packages)
    app.router.add_get("/api/provision/magisk_db",    _get_provision_magisk_db)
    app.router.add_get("/api/provision/latest_binary", _get_provision_latest_binary)
    app.router.add_post("/api/provision/tls_credentials", _post_provision_tls_credentials)
    app.router.add_post("/api/devices/{id}/secure_link",  _post_secure_link)

    # Live events WebSocket
    app.router.add_get("/api/events", _ws_events)

    return app


async def create_runner(devices_ref: dict, shell_pending_ref: dict,
                        shell_dashboard_ref: dict) -> web.AppRunner:
    """Convenience wrapper — init + create_app + AppRunner."""
    init(devices_ref, shell_pending_ref, shell_dashboard_ref)
    app = await create_app()
    return web.AppRunner(app)


# ─── Middleware ───────────────────────────────────────────────────────────────

@web.middleware
async def _error_middleware(request: web.Request, handler):
    """
    Catch unhandled exceptions and return a consistent error shape.

    AuthError from em_auth is also caught here so route handlers don't
    need to handle it explicitly.
    """
    try:
        return await handler(request)
    except auth.AuthError as e:
        return e.to_response()
    except web.HTTPException:
        raise  # let aiohttp handle its own HTTP exceptions normally
    except Exception as e:
        log.exception(f"Unhandled error in {request.method} {request.path}")
        return _error("internal_error", "An internal error occurred", 500)


# ─── Static / setup ───────────────────────────────────────────────────────────

async def _serve_spa(request: web.Request) -> web.Response:
    """Serve index.html for all SPA routes."""
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(
            status=503,
            text="Dashboard not built — static/index.html not found",
        )
    return web.FileResponse(index)


async def _serve_dashboard(request: web.Request) -> web.Response:
    """Serve dashboard.html for /dashboard."""
    dashboard = STATIC_DIR / "dashboard.html"
    if not dashboard.exists():
        return web.Response(status=503, text="dashboard.html not found in static/")
    return web.FileResponse(dashboard)


async def _redirect_root(request: web.Request) -> web.Response:
    raise web.HTTPFound("/")


async def _get_setup_state(request: web.Request) -> web.Response:
    """GET /api/system/setup-state — public: is first-run setup pending?"""
    return _ok({"needs_setup": auth.get_bootstrap_token() is not None})


async def _post_setup(request: web.Request) -> web.Response:
    """
    POST /api/setup — first-run admin account creation.

    Body: {token, username, password}
    Returns 201 + {token, role} on success so the client is immediately
    logged in after setup.
    """
    body = await _json_body(request)
    token    = _require_str(body, "token")
    username = _require_str(body, "username")
    password = _require_str(body, "password")

    await auth.create_first_admin(token, username, password)

    session_token, role = await auth.login(username, password)
    return _ok({"token": session_token, "role": role}, status=201)


# ─── Auth ─────────────────────────────────────────────────────────────────────

async def _post_login(request: web.Request) -> web.Response:
    """POST /api/auth/login — {username, password} → {token, role}"""
    body     = await _json_body(request)
    username = _require_str(body, "username")
    password = _require_str(body, "password")

    token, role = await auth.login(username, password)
    return _ok({"token": token, "role": role})


async def _post_logout(request: web.Request) -> web.Response:
    """POST /api/auth/logout — invalidate current session."""
    user = await auth.resolve_session(request)
    if user:
        await auth.logout(user["token"])
    return _ok({})


@auth.require_auth
async def _get_me(request: web.Request) -> web.Response:
    """GET /api/auth/me — current user info."""
    user = request["user"]
    return _ok({
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    })


# ─── Devices ──────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_devices(request: web.Request) -> web.Response:
    """GET /api/devices — all devices, live state merged with DB."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_all_devices)
    return _ok([_merge_device(row) for row in rows])


@auth.require_auth
async def _get_pending(request: web.Request) -> web.Response:
    """GET /api/devices/pending — unapproved devices."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_pending_devices)
    return _ok([_merge_device(row) for row in rows])


@auth.require_auth
async def _get_device(request: web.Request) -> web.Response:
    """GET /api/devices/{id}"""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    return _ok(_merge_device(row))


@auth.require_auth
async def _get_device_turns(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/turns — recent voice-turn traces, newest last.
    Served from the persistent turns table (survives controller and device
    restarts). Powers the Activity tab's observability panel.

    Query params: limit (default 50, max 1000), since (epoch seconds)."""
    device_id = request.match_info["id"]
    try:
        limit = min(int(request.query.get("limit", 50)), 1000)
        since = request.query.get("since")
        since = float(since) if since is not None else None
    except ValueError:
        return _error("bad_request", "limit/since must be numeric", 400)
    loop  = asyncio.get_event_loop()
    turns = await loop.run_in_executor(
        None, lambda: db.get_turns(device_id, limit, since)
    )
    return _ok(turns)


@auth.require_auth
async def _get_device_activity(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/activity?days=7 — aggregated activity stats
    for trend review: per-day turn buckets (counts, outcomes, latency
    percentiles, wake scores, underruns), per-wake-model rollups, hourly
    near-miss counters, and hourly hardware metrics (CPU/RAM/storage/RSSI)."""
    device_id = request.match_info["id"]
    try:
        days = min(int(request.query.get("days", 7)), 180)
    except ValueError:
        return _error("bad_request", "days must be an integer", 400)
    since = time.time() - days * 86400

    loop     = asyncio.get_event_loop()
    turns    = await loop.run_in_executor(
        None, lambda: db.get_turns(device_id, 50_000, since)
    )
    counters = await loop.run_in_executor(
        None, lambda: db.get_wake_counters(device_id, since)
    )
    metrics  = await loop.run_in_executor(
        None, lambda: db.get_device_metrics(device_id, since)
    )

    def pct(sorted_vals, p):
        if not sorted_vals:
            return None
        return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]

    # Per-day buckets (local time), oldest first.
    day_buckets: dict[str, list[dict]] = {}
    for t in turns:
        day = time.strftime("%Y-%m-%d", time.localtime(t["ts"]))
        day_buckets.setdefault(day, []).append(t)

    days_out = []
    for day in sorted(day_buckets):
        ts_list   = day_buckets[day]
        ok        = [t for t in ts_list if t["outcome"] == "ok"]
        totals    = sorted(t["total_ms"] for t in ok if (t["total_ms"] or 0) > 0)
        scores    = [t["wake_score"] for t in ts_list if t["wake_score"] is not None]
        underruns = sum(t["underruns"] or 0 for t in ts_list)
        outcomes: dict[str, int] = {}
        for t in ts_list:
            outcomes[t["outcome"] or "?"] = outcomes.get(t["outcome"] or "?", 0) + 1
        days_out.append({
            "date":           day,
            "turns":          len(ts_list),
            "ok":             len(ok),
            "outcomes":       outcomes,
            "total_ms_p50":   pct(totals, 0.50),
            "total_ms_p95":   pct(totals, 0.95),
            "wake_score_avg": round(sum(scores) / len(scores), 3) if scores else None,
            "wake_score_min": round(min(scores), 3) if scores else None,
            "underruns":      underruns,
        })

    # Per-wake-model rollup — supports A/B-ing custom OWW models.
    models: dict[str, dict] = {}
    for t in turns:
        if not t["wake_model"]:
            continue
        m = models.setdefault(
            t["wake_model"], {"turns": 0, "score_sum": 0.0, "score_min": None}
        )
        m["turns"] += 1
        if t["wake_score"] is not None:
            m["score_sum"] += t["wake_score"]
            m["score_min"] = (
                t["wake_score"] if m["score_min"] is None
                else min(m["score_min"], t["wake_score"])
            )
    models_out = {
        name: {
            "turns":     m["turns"],
            "score_avg": round(m["score_sum"] / m["turns"], 3) if m["turns"] else None,
            "score_min": m["score_min"],
        }
        for name, m in models.items()
    }

    return _ok({
        "days":          days_out,
        "wake_models":   models_out,
        "wake_counters": [dict(r) for r in counters],
        "metrics":       metrics,
    })


@auth.require_admin
async def _patch_device(request: web.Request) -> web.Response:
    """PATCH /api/devices/{id} — update label."""
    device_id = request.match_info["id"]
    body  = await _json_body(request)
    label = _require_str(body, "label")

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.set_device_label, device_id, label)
    await _push_event({"type": "device_update", "device_id": device_id,
                       "state": {"label": label}})
    return _ok({"device_id": device_id, "label": label})


@auth.require_admin
async def _delete_device(request: web.Request) -> web.Response:
    """DELETE /api/devices/{id} — remove from registry."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.delete_device, device_id)
    # Row gone → reconcile tears down any BT proxy listener/mDNS for it.
    await em_ble_proxy.reconcile(device_id)
    await _push_event({"type": "device_deleted", "device_id": device_id})
    return _ok({})


@auth.require_admin
async def _post_approve(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/approve

    Body: {label, config?}
    Approves the device, assigns a label, and optionally overrides config.
    If the device is currently connected in pending state it will be
    accepted on its next retry (within 30s).
    """
    device_id = request.match_info["id"]
    body   = await _json_body(request)
    label  = _require_str(body, "label")
    config = body.get("config")  # optional

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    if row["approved"]:
        return _error("already_approved", "Device is already approved", 409)

    await loop.run_in_executor(None, db.approve_device, device_id, label, config)
    await _push_event({"type": "device_approved", "device_id": device_id,
                       "label": label})
    return _ok({"device_id": device_id, "label": label})


@auth.require_auth
async def _get_device_config(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/config — returns effective config and override flag."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    config = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    return _ok({"config": config, "use_global_config": bool(row["use_global_config"])})


@auth.require_admin
async def _post_device_config(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/config

    Body may include use_global_config (bool) and any config fields.

    If use_global_config=true: revert device to fleet defaults. The config
    fields in the body are ignored — the effective config is the global one.

    If use_global_config=false: enable per-device override. Config fields
    in the body are persisted as the device's own config and pushed live.
    If the device was previously on global, set_device_use_global(False)
    is called first, which marks the flag without touching the config
    column — the supplied config is then written over it.

    If use_global_config is absent: behave as before (update config only,
    leave the flag unchanged). This path is used by the global config push
    to all on-global devices — it should not alter per-device flag state.
    """
    device_id = request.match_info["id"]
    body = await _json_body(request)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    use_global = body.pop("use_global_config", None)  # extract flag, not a config key

    if use_global is True:
        # Revert to global: reset flag + config column, ignore body
        await loop.run_in_executor(None, db.set_device_use_global, device_id, True)
        config = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    elif use_global is False:
        # Enable per-device override: mark flag, then write supplied config
        await loop.run_in_executor(None, db.set_device_use_global, device_id, False)
        config = body
        await loop.run_in_executor(None, db.set_device_config, device_id, config)
    else:
        # No flag in body — plain config update, flag unchanged
        config = body
        await loop.run_in_executor(None, db.set_device_config, device_id, config)

    # Push effective config to live device if connected
    pushed = False
    live = _devices.get(device_id)
    if live is not None:
        await live.send_control({"type": "config", **config})
        if "owwThreshold" in config:
            live.oww_threshold = float(config["owwThreshold"])
        if "owwModel" in config:
            live.oww_model = config["owwModel"]
            # Refresh HA's wake-word dropdown (lazy import — em_esphome
            # imports em_api at module level).
            import em_esphome
            em_esphome.update_oww_model(device_id, config["owwModel"])
        if "owwSpeexNs" in config:
            live.oww_speex_ns = bool(config["owwSpeexNs"])
        if "nsAsr" in config:
            live.ns_asr = bool(config["nsAsr"])
        if "bargeInEnabled" in config:
            live.barge_in_enabled = bool(config["bargeInEnabled"])
        if "bargeInThreshold" in config:
            live.barge_threshold = float(config["bargeInThreshold"])
        if "eqBands" in config:
            live.eq_bands = config["eqBands"]
        if "eqLoudness" in config:
            live.eq_loudness = bool(config["eqLoudness"])
        if any(k in config for k in ("ledScene", "ledListenColor", "ledThinkColor")):
            # Scene resolution needs the full config (custom colours may not
            # be in a partial body) — re-read the effective config.
            eff = await loop.run_in_executor(
                None, db.get_effective_device_config, device_id
            )
            live.led_scene = em_scenes.resolve(eff)
        log.info(f"[api] Config pushed to live device: {device_id}")
        pushed = True

    # BT proxy lifecycle follows bleProxyEnabled in the *effective* config —
    # reconcile unconditionally (idempotent): a revert-to-global changes the
    # effective value without the key appearing in the body.
    await em_ble_proxy.reconcile(device_id)

    effective_use_global = use_global if use_global is not None else bool(row["use_global_config"])
    await _push_event({"type": "device_update", "device_id": device_id,
                       "state": {"config": config, "use_global_config": effective_use_global}})
    return _ok({"device_id": device_id, "config": config,
                "use_global_config": effective_use_global, "pushed": pushed})


@auth.require_admin
async def _post_device_wifi(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/wifi — switch the device to a new WiFi network.

    Body: {"ssid": "...", "psk": "..."} (empty/absent psk = open network).

    Returns 202 immediately: the device owns the whole switch (associate →
    DHCP → reconnect gates, auto-rollback on any failure — see the device's
    internal/wifi package). The outcome arrives asynchronously as a
    wifi_result control message and is surfaced via the device_update
    event / the "wifi" field on the device object.
    """
    device_id = request.match_info["id"]
    body = await _json_body(request)
    ssid = _require_str(body, "ssid")
    psk  = str(body.get("psk") or "")

    # Mirror the device's own validation so obvious mistakes fail fast
    # with a readable message instead of a full switch/rollback cycle.
    if any(ch in ssid or ch in psk for ch in ('"', "\\")):
        return _error("invalid_credentials",
                      "SSID/passphrase cannot contain double-quote or "
                      "backslash characters (wpa_supplicant.conf cannot "
                      "represent them safely)", 400)
    if psk and not 8 <= len(psk) <= 63:
        return _error("invalid_credentials",
                      f"WPA passphrase must be 8–63 characters (got {len(psk)})", 400)

    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)

    st = wifi_state(device_id)
    if st["pending"]:
        return _error("wifi_change_in_progress",
                      f"A change to \"{st['pending']['ssid']}\" is already "
                      f"in progress", 409)

    st["pending"] = {"ssid": ssid, "started_at": time.time()}
    st["last_result"] = None
    await live.send_control({"type": "wifi_change", "ssid": ssid, "psk": psk})
    db.log_device(device_id, "info", "controller", f'WiFi change to "{ssid}" requested')
    await _push_event({"type": "device_update", "device_id": device_id,
                       "state": {"wifi": st}})
    return _ok({"device_id": device_id, "ssid": ssid, "status": "switching"},
               status=202)


@auth.require_admin
async def _post_device_wifi_scan(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/wifi/scan — ask the device for visible networks.

    Synchronous from the dashboard's point of view: sends wifi_scan and
    awaits the wifi_scan_result control message (the device's scan itself
    takes ~5s).
    """
    device_id = request.match_info["id"]
    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)
    if getattr(live, "wifi_scan_future", None) is not None:
        return _error("scan_in_progress", "A scan is already running", 409)

    fut = asyncio.get_event_loop().create_future()
    live.wifi_scan_future = fut
    try:
        await live.send_control({"type": "wifi_scan"})
        msg = await asyncio.wait_for(fut, timeout=20)
    except asyncio.TimeoutError:
        return _error("scan_timeout",
                      "Device did not return scan results within 20s "
                      "(old firmware without WiFi support?)", 504)
    finally:
        live.wifi_scan_future = None
    if msg.get("error"):
        return _error("scan_failed", msg["error"], 502)
    return _ok({"networks": msg.get("networks") or []})


@auth.require_auth
async def _get_device_logs(request: web.Request) -> web.Response:
    """
    GET /api/devices/{id}/logs

    Query params:
      limit  — max rows (default 100, max 1000)
      before — cursor: return entries with ts < before (unix ms)
    """
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()

    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    try:
        limit = int(request.rel_url.query.get("limit", "100"))
    except ValueError:
        return _error("invalid_param", "limit must be an integer", 400)

    before_param = request.rel_url.query.get("before")
    before_ts = None
    if before_param:
        try:
            before_ts = int(before_param)
        except ValueError:
            return _error("invalid_param", "before must be a unix ms timestamp", 400)

    rows = await loop.run_in_executor(
        None, db.get_device_logs, device_id, limit, before_ts
    )
    entries = [
        {
            "id":        r["id"],
            "ts":        r["ts"],
            "level":     r["level"],
            "source":    r["source"],
            "message":   r["message"],
        }
        for r in rows
    ]
    return _ok(entries)


# ─── OTA: update + rollback ───────────────────────────────────────────────────

@auth.require_admin
async def _post_device_update(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/update

    Deploys a new binary to the device using A/B slots.
    Accepts an optional JSON body with {"upload_token": "..."} to deploy a
    locally uploaded binary instead of the latest GitHub release.

    Returns 202 Accepted — update runs in the background.
    """
    device_id = request.match_info["id"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    upload_token = body.get("upload_token")
    binary_override = None
    release = None

    if upload_token:
        binary_override = _pending_uploads.pop(upload_token, None)
        if binary_override is None:
            return _error("invalid_token", "Upload token not found or expired", 404)
        _embedded = _extract_binary_version(binary_override)
        _ver      = _embedded or f"local-{time.strftime('%Y%m%d-%H%M')}"
        release   = {"version": _ver, "url": None}
    else:
        release = await _get_cached_release()
        if release is None:
            return _error("no_release", "No release information available", 409)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)

    if device_id in _updates_in_progress:
        return _error("update_in_progress", "An update is already in progress", 409)

    asyncio.create_task(_run_update(device_id, release, binary_override))
    return _ok({"status": "started", "version": release["version"]}, status=202)


@auth.require_admin
async def _post_device_rollback(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/rollback

    Flips the inactive A/B slot back to active. Instant — no binary transfer.
    Requires firmware_previous to be set.
    Returns 202 Accepted.
    """
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    if not row["firmware_previous"]:
        return _error("no_rollback_available",
                      "No previous version recorded — cannot roll back", 404)

    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)

    if device_id in _updates_in_progress:
        return _error("update_in_progress", "An update is already in progress", 409)

    asyncio.create_task(_run_rollback(device_id, row["firmware_previous"]))
    return _ok({"status": "started", "rolling_back_to": row["firmware_previous"]}, status=202)


@auth.require_admin
async def _post_upload_binary(request: web.Request) -> web.Response:
    """
    POST /api/releases/upload (multipart: field name "binary")

    Upload a local binary for deployment. Returns an upload_token valid for
    10 minutes. Pass the token to /api/devices/{id}/update or
    /api/releases/deploy to deploy it.
    """
    import uuid as _uuid
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "binary":
            return _error("invalid_upload", "Expected multipart field 'binary'", 400)
        binary = await field.read()
        if not binary:
            return _error("empty_upload", "Uploaded binary is empty", 400)
        if len(binary) > 50 * 1024 * 1024:
            return _error("too_large", "Binary exceeds 50 MB limit", 413)

        token = str(_uuid.uuid4())
        _pending_uploads[token] = binary
        log.info(f"[api] Binary uploaded: {len(binary):,} bytes token={token[:8]}…")

        async def _expire():
            await asyncio.sleep(600)
            _pending_uploads.pop(token, None)
        asyncio.create_task(_expire())

        return _ok({"upload_token": token, "size": len(binary)})
    except Exception as e:
        log.error(f"[api] Upload error: {e}")
        return _error("upload_failed", str(e), 500)


# ─── OTA background tasks ─────────────────────────────────────────────────────


def _extract_binary_version(binary: bytes) -> str | None:
    """
    Scan a compiled Go binary for its embedded EchoMuse version string.
    The version is compiled in via -ldflags "-X ...Version=YYYYMMDD-HHMM-suffix".
    Pattern matches e.g. 20260614-1152-dev, 20260614-0513-release, etc.
    Falls back to None if not found, caller generates a local-YYYYMMDD-HHMM label.
    """
    import re as _re
    match = _re.search(rb'20\d{6}-\d{4}-[a-z][a-z0-9]*', binary)
    return match.group(0).decode("ascii") if match else None


async def _update_failed(device_id: str, reason: str) -> None:
    """
    Record and broadcast an OTA failure: device log line, in-memory
    update_error (read back via /api/devices), and a device_update_failed
    event for the dashboard WS. Every abort path in _run_update/_run_rollback
    must come through here — a log-only failure leaves the dashboard tile at
    "updating…" indefinitely.
    """
    _update_errors[device_id] = reason
    await _push_log_event(device_id, "error", "controller", reason)
    await _push_event({
        "type":      "device_update_failed",
        "device_id": device_id,
        "error":     reason,
    })


async def _run_update(device_id: str, release: dict,
                      binary_override: bytes | None = None) -> None:
    """
    Background task: A/B slot update.

    1. Fetch binary (GitHub or pre-uploaded).
    2. Detect active slot via readlink; migrate legacy layout if needed.
    3. Stream binary to inactive slot.
    4. Flip symlink atomically.
    5. Restart service and monitor reconnect.
    6. Detect auto-rollback (start_server.sh retry exhausted).
    """
    _updates_in_progress.add(device_id)
    _update_errors.pop(device_id, None)  # fresh attempt clears the last failure
    loop = asyncio.get_event_loop()
    version = release["version"]

    try:
        await _push_log_event(device_id, "info", "controller",
                              f"OTA update starting → {version}")

        # Fetch binary
        if binary_override is not None:
            binary = binary_override
            await _push_log_event(device_id, "info", "controller",
                                  f"Using uploaded binary ({len(binary):,} bytes)")
        else:
            binary = await _fetch_binary(release["url"])
            if binary is None:
                await _update_failed(device_id,
                                     "Failed to fetch binary from GitHub")
                return

        # Record current version as previous before anything changes
        row = await loop.run_in_executor(None, db.get_device, device_id)
        current_ver = row["firmware_ver"] if row else None
        await loop.run_in_executor(None, db.set_firmware_previous, device_id, current_ver)

        live = _devices.get(device_id)
        if live is None:
            await _update_failed(device_id,
                                 "Device disconnected before update could start")
            return

        # Detect active slot and migrate legacy layout if needed — single shell
        # session to avoid the race condition of two sequential open/close cycles.
        detect_cmd = (
            "CURRENT=$(readlink /data/local/bin/server 2>/dev/null); "
            "if [ \"$CURRENT\" = \"server_a\" ] || [ \"$CURRENT\" = \"server_b\" ]; then "
            "  echo \"SLOT:$CURRENT\"; "
            "else "
            "  cp /data/local/bin/server /data/local/bin/server_a 2>&1 && "
            "  chmod 755 /data/local/bin/server_a && "
            "  ln -sf server_a /data/local/bin/server && "
            "  echo \"SLOT:server_a MIGRATED\" || echo \"MIGRATE_FAILED\"; "
            "fi"
        )
        detect_result = await _shell_run(live, detect_cmd, timeout=60.0)
        log.info(f"[api] Slot detect result for {device_id}: {detect_result!r}")

        if "MIGRATE_FAILED" in detect_result:
            await _update_failed(device_id,
                                 "A/B migration failed — aborting update")
            return

        active_slot = None
        for line in detect_result.splitlines():
            if "SLOT:" in line:
                candidate = line.split("SLOT:")[-1].strip().split()[0]
                if candidate in ("server_a", "server_b"):
                    active_slot = candidate
                    break

        if active_slot is None:
            await _update_failed(device_id,
                                 f"Could not determine active slot — output: {detect_result!r}")
            return

        if "MIGRATED" in detect_result:
            await _push_log_event(device_id, "info", "controller",
                                  "A/B migration complete — active slot: server_a")

        # Sync the startup script while we're here — OTA is the only update
        # path existing devices have for it (see _sync_start_script).
        await _sync_start_script(live, device_id)

        inactive_slot = "server_b" if active_slot == "server_a" else "server_a"
        await _push_log_event(device_id, "info", "controller",
                              f"Deploying to slot {inactive_slot} (active: {active_slot})")

        # Stream binary to inactive slot
        ok = await _stream_binary_to_slot(live, binary, inactive_slot)
        if not ok:
            await _update_failed(device_id,
                                 f"Binary transfer to {inactive_slot} failed")
            return

        # Brief pause so device can cleanly close the transfer shell before
        # we open a new one for the symlink flip.
        await asyncio.sleep(1.0)

        # Atomic symlink flip + service restart
        await _push_log_event(device_id, "info", "controller",
                              f"Flipping symlink → {inactive_slot} and restarting")
        result = await _shell_run(live,
            f"ln -sf {inactive_slot} /data/local/bin/server && "
            f"kill $PPID"
        )
        # Shell dies when the server process is killed — FLIP_OK will never arrive.
        # _monitor_reconnect below detects whether the restart succeeded.

        # Wait for device to come back
        confirmed = await _monitor_reconnect(device_id, version, previous_version=current_ver, timeout=90)

        if confirmed:
            _update_errors.pop(device_id, None)
            await _push_log_event(device_id, "info", "controller",
                                  f"✓ Update confirmed: {version}")
            await _push_event({
                "type":      "device_updated",
                "device_id": device_id,
                "version":   version,
            })
        else:
            row     = await loop.run_in_executor(None, db.get_device, device_id)
            running = row["firmware_ver"] if row else "unknown"

            if running == current_ver:
                # Device came back on old version — auto-rollback by start_server.sh
                await loop.run_in_executor(
                    None, db.set_firmware_previous, device_id, None
                )
                _update_errors[device_id] = (
                    f"auto-rolled back to {running} — new binary failed to start"
                )
                await _push_log_event(device_id, "warn", "controller",
                    f"Device auto-rolled back to {running} "
                    f"— new binary failed {3} start attempts")
                await _push_event({
                    "type":      "device_auto_rolled_back",
                    "device_id": device_id,
                    "version":   running,
                })
            else:
                _update_errors[device_id] = (
                    f"timed out — device running {running}"
                )
                await _push_log_event(device_id, "warn", "controller",
                    f"Update timed out — device running: {running}")
                await _push_event({
                    "type":      "device_update_failed",
                    "device_id": device_id,
                    "error":     _update_errors[device_id],
                    "running":   running,
                })

    except Exception as e:
        log.exception(f"[api] OTA update error for {device_id}: {e}")
        await _update_failed(device_id, f"OTA exception: {e}")
    finally:
        _updates_in_progress.discard(device_id)


async def _run_rollback(device_id: str, target_version: str) -> None:
    """
    Background task: flip to inactive A/B slot.

    No binary transfer needed — the old binary is already in the inactive slot.
    """
    _updates_in_progress.add(device_id)
    _update_errors.pop(device_id, None)  # fresh attempt clears the last failure
    try:
        await _push_log_event(device_id, "info", "controller",
                              f"Rolling back to {target_version}")

        live = _devices.get(device_id)
        if live is None:
            await _update_failed(device_id,
                                 "Device disconnected before rollback")
            return

        active_slot = None
        detect_result = await _shell_run(live,
            "CURRENT=$(readlink /data/local/bin/server 2>/dev/null); "
            "if [ \"$CURRENT\" = \"server_a\" ] || [ \"$CURRENT\" = \"server_b\" ]; then "
            "  echo \"SLOT:$CURRENT\"; "
            "else echo \"SLOT_UNKNOWN\"; fi"
        )
        for line in detect_result.splitlines():
            if "SLOT:" in line:
                candidate = line.split("SLOT:")[-1].strip().split()[0]
                if candidate in ("server_a", "server_b"):
                    active_slot = candidate
                    break

        if active_slot is None:
            await _update_failed(device_id,
                                 "Cannot determine active slot — is A/B set up?")
            return

        inactive_slot = "server_b" if active_slot == "server_a" else "server_a"
        await _push_log_event(device_id, "info", "controller",
                              f"Flipping {active_slot} → {inactive_slot}")

        result = await _shell_run(live,
            f"ln -sf {inactive_slot} /data/local/bin/server && "
            f"kill $PPID"
        )
        # Shell dies when the server process is killed — ROLLBACK_OK will never arrive.

        loop = asyncio.get_event_loop()
        row_pre = await loop.run_in_executor(None, db.get_device, device_id)
        current_fw = row_pre["firmware_ver"] if row_pre else None
        confirmed = await _monitor_reconnect(
            device_id, target_version,
            previous_version=current_fw,
            timeout=90,
        )

        if confirmed:
            _update_errors.pop(device_id, None)
            await loop.run_in_executor(
                None, db.set_firmware_previous, device_id, None
            )
            await _push_log_event(device_id, "info", "controller",
                                  f"✓ Rollback confirmed: {target_version}")
            await _push_event({
                "type":      "device_rolled_back",
                "device_id": device_id,
                "version":   target_version,
            })
        else:
            await _update_failed(device_id,
                                 "Rollback did not reconnect within 90s")

    except Exception as e:
        log.exception(f"[api] Rollback error for {device_id}: {e}")
        await _update_failed(device_id, f"Rollback exception: {e}")
    finally:
        _updates_in_progress.discard(device_id)


async def _monitor_reconnect(
    device_id: str,
    expected_version: str,
    previous_version: str | None = None,
    timeout: int = 90,
) -> bool:
    """
    Poll until the device reconnects on a new version, or timeout elapses.

    Accepts success if the device reports expected_version exactly (GitHub
    releases where the tag matches the binary's embedded version), OR any
    version that differs from previous_version (local uploads where the
    binary reports its own version string, not the controller's local-YYYYMMDD
    tracking string).
    """
    loop     = asyncio.get_event_loop()
    deadline = time.monotonic() + timeout
    await asyncio.sleep(8)  # give device time to stop and restart

    while time.monotonic() < deadline:
        if device_id in _devices:
            row = await loop.run_in_executor(None, db.get_device, device_id)
            if row:
                running = row["firmware_ver"]
                if running == expected_version:
                    return True
                if previous_version is not None and running != previous_version:
                    return True
        await asyncio.sleep(2)

    return False


# ─── Shell helpers ────────────────────────────────────────────────────────────

async def _get_device_shell_ws(live) -> object:
    """
    Request a programmatic shell connection from the device.

    Acquires a per-device lock so sessions are strictly sequential.
    handle_shell resolves the future with the ws, then waits for ws.close()
    before returning — so the connection stays alive while we use it.
    """
    device_id = live.device_id
    loop      = asyncio.get_event_loop()

    if device_id not in _shell_lock:
        _shell_lock[device_id] = asyncio.Lock()

    try:
        await asyncio.wait_for(_shell_lock[device_id].acquire(), timeout=20.0)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Shell lock acquisition timed out for {device_id}")

    future = loop.create_future()
    _shell_pending[device_id] = future
    # Deliberately do NOT set _shell_dashboard — signals programmatic mode.

    await live.send_control({"type": "shell_open"})
    try:
        ws = await asyncio.wait_for(future, timeout=15.0)
        _shell_ws[device_id] = ws
        return ws
    except asyncio.TimeoutError:
        _shell_pending.pop(device_id, None)
        _shell_ws.pop(device_id, None)
        _shell_lock[device_id].release()
        raise


async def _release_shell_ws(device_id: str, live=None) -> None:
    """
    Close the programmatic shell session.

    Closing ws wakes handle_shell's ws.wait_closed(), which then returns
    and lets the device clean up its side too.
    """
    ws = _shell_ws.pop(device_id, None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass
    _shell_pending.pop(device_id, None)
    if live is not None:
        await live.send_control({"type": "shell_close"})
    lock = _shell_lock.get(device_id)
    if lock and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass


async def _shell_run(live, cmd: str, timeout: float = 30.0) -> str:
    """
    Run a shell command on the device and return its stdout as a string.

    Appends a sentinel marker to detect when output is complete.
    """
    SENTINEL = "__CMD_DONE_9f3a__"
    device_id = live.device_id
    output: list[str] = []
    try:
        ws = await _get_device_shell_ws(live)
        await ws.send(f"{cmd} ; echo '{SENTINEL}'\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5.0)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                if SENTINEL in text:
                    output.append(text[:text.index(SENTINEL)])
                    break
                output.append(text)
            except asyncio.TimeoutError:
                break
        return "".join(output).strip()
    except Exception as e:
        log.error(f"[api] shell_run failed ({cmd!r}): {e}")
        return ""
    finally:
        await _release_shell_ws(device_id, live)


async def _stream_binary_to_slot(live, binary: bytes, slot: str) -> bool:
    """Transfer a firmware binary to /data/local/bin/{slot}."""
    return await _stream_file_to_device(live, binary, f"/data/local/bin/{slot}")


async def _stream_file_to_device(live, data: bytes, dest: str,
                                 mode: str = "755") -> bool:
    """
    Transfer a file to `dest` on the device via shell heredoc (default mode 755).

    Detects available base64 decoder (busybox base64, python3, python) before
    transferring, since 'base64' is not always in PATH on Android/FireOS.
    Uses a heredoc so no intermediate .b64 file is needed.
    The heredoc delimiter contains '_' which is not in the base64 alphabet.
    """
    import base64 as _b64

    device_id     = live.device_id
    DELIM         = "__END_B64_42__"
    DETECT_MARKER = "__DETECT_DONE__"

    try:
        ws = await _get_device_shell_ws(live)

        # Remove any previous attempt
        await ws.send(f"rm -f {dest}\n")
        await asyncio.sleep(0.2)

        # ── Detect available base64 decoder ──────────────────────────────────
        # Try busybox first (Magisk provides it), then python3/python.
        # We run a round-trip sanity test so we know the decode flag works.
        await ws.send(
            "if echo dGVzdA== | busybox base64 -d >/dev/null 2>&1; then echo DECODER:busybox; "
            "elif python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python3; "
            "elif python  -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python; "
            f"else echo DECODER:none; fi; echo {DETECT_MARKER}\n"
        )

        detect_buf = ""
        detect_dl  = time.monotonic() + 15
        while time.monotonic() < detect_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=2)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                detect_buf += text
                if DETECT_MARKER in detect_buf:
                    break
            except asyncio.TimeoutError:
                continue

        if "DECODER:busybox" in detect_buf:
            decode_cmd = "busybox base64 -d"
        elif "DECODER:python3" in detect_buf:
            decode_cmd = ("python3 -c "
                          "'import sys,base64; "
                          "sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))'")
        elif "DECODER:python" in detect_buf:
            decode_cmd = ("python -c "
                          "'import sys,base64; "
                          "sys.stdout.write(base64.b64decode(sys.stdin.read()))'")
        else:
            log.error(f"[api] No base64 decoder found on device. "
                      f"Detection output: {detect_buf!r}")
            return False

        log.info(f"[api] Decoder: {decode_cmd.split()[0]} {decode_cmd.split()[1]}")

        # ── Heredoc transfer ─────────────────────────────────────────────────
        lines = _b64.encodebytes(data).decode("ascii").splitlines(keepends=True)
        log.info(f"[api] Transferring {len(data):,} bytes to {dest} "
                 f"({len(lines)} base64 lines via heredoc)")

        # Single shell command: decode heredoc → dest, set permissions, confirm
        await ws.send(
            f"{decode_cmd} << '{DELIM}' > {dest} && "
            f"chmod {mode} {dest} && "
            f"echo TRANSFER_OK\n"
        )

        # Stream base64 data — each line already ends with \n from encodebytes
        for line in lines:
            await ws.send(line)

        # Close heredoc; shell now executes the decode pipeline
        await ws.send(f"{DELIM}\n")
        log.info(f"[api] Heredoc sent — waiting for TRANSFER_OK")

        # Wait for confirmation (decode of ~13 MB on ARM takes a few seconds)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                if "TRANSFER_OK" in text:
                    log.info(f"[api] Transfer to {dest} confirmed")
                    return True
                if text.strip():
                    log.debug(f"[api] Shell output during transfer: {text!r}")
            except asyncio.TimeoutError:
                continue

        log.error(f"[api] Transfer to {dest} timed out waiting for TRANSFER_OK")
        return False

    except Exception as e:
        log.error(f"[api] File transfer to {dest} failed: {e}")
        return False
    finally:
        await _release_shell_ws(device_id, live)



async def _sync_start_script(live, device_id: str) -> None:
    """
    OTA-time payload sync: heal /data/local/bin/start_server.sh drift.

    The startup script is installed at provisioning and — unlike the server
    binary — had no other update path, so fleet drift accumulates (found
    2026-07-11: Lounge was a script revision behind Office). Every OTA now
    compares the device's script against the canonical payload
    (controller/device_payloads/) and pushes it when they differ.

    Replacement is rename-based on purpose: the running script's shell keeps
    reading the OLD inode, so the update only takes effect at the next
    device reboot — safe to do while the script sits in its `wait` loop.
    Best-effort: a sync failure logs but never blocks the firmware update.
    """
    path = "/data/local/bin/start_server.sh"
    try:
        script = (PAYLOADS_DIR / "start_server.sh").read_bytes()
    except OSError as e:
        log.error(f"[api] start_server.sh payload unreadable — skipping sync: {e}")
        return
    want = hashlib.md5(script).hexdigest()

    out = await _shell_run(live, f"busybox md5sum {path} 2>/dev/null")
    if want in out:
        return  # in sync — the common case
    await asyncio.sleep(1.0)  # let the md5 shell session close cleanly

    await _push_log_event(device_id, "info", "controller",
                          "start_server.sh out of date — syncing canonical version")
    tmp = path + ".new"
    if not await _stream_file_to_device(live, script, tmp):
        await _push_log_event(device_id, "warn", "controller",
                              "start_server.sh sync failed (transfer) — continuing OTA")
        return
    await asyncio.sleep(1.0)

    res = await _shell_run(live,
        f'NEW=$(busybox md5sum {tmp} | busybox cut -d" " -f1); '
        f'if [ "$NEW" = "{want}" ]; then '
        f'mv {tmp} {path} && chmod 755 {path} && echo SCRIPT_SYNCED; '
        f'else rm -f {tmp}; echo SCRIPT_MD5_MISMATCH:$NEW; fi')
    if "SCRIPT_SYNCED" in res:
        await _push_log_event(device_id, "info", "controller",
                              "start_server.sh synced — takes effect on next device reboot")
    else:
        await _push_log_event(device_id, "warn", "controller",
                              f"start_server.sh sync failed ({res.strip() or 'no output'}) — continuing OTA")
    await asyncio.sleep(1.0)


async def _exec_shell(live, cmd: str) -> None:
    """Send a command to the device shell and return immediately (fire-and-forget)."""
    try:
        ws = await _get_device_shell_ws(live)
        await ws.send(cmd + "\n")
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning(f"[api] Shell exec failed ({cmd!r}): {e}")
    finally:
        await _release_shell_ws(live.device_id, live)


# ─── Shell WebSocket proxy (interactive dashboard terminal) ───────────────────

async def _ws_shell(request: web.Request) -> web.WebSocketResponse:
    """
    WS /api/devices/{id}/shell — interactive shell terminal for dashboard.

    Auth is handled via ws_resolve_session (checks cookie then ?token= query
    param) because browser WebSocket clients cannot set custom headers.
    Do NOT add @auth.require_admin here — _extract_token doesn't read query
    params and would reject every connection before this function runs.

    Sets _shell_dashboard so handle_shell proxies in interactive mode.
    """
    device_id = request.match_info["id"]

    user = await auth.ws_resolve_session(request)
    if user is None:
        raise web.HTTPUnauthorized()
    if user["role"] != "admin":
        raise web.HTTPForbidden()

    live = _devices.get(device_id)
    if live is None:
        raise web.HTTPConflict(reason="Device is not connected")

    # Refuse if a programmatic shell session (e.g. OTA transfer) is in progress.
    # Opening a terminal mid-transfer sends shell_open to the device, which cancels
    # the current shell context and kills the transfer.
    lock = _shell_lock.get(device_id)
    if lock and lock.locked():
        raise web.HTTPConflict(reason="Device shell is busy — an OTA update is in progress")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    log.info(f"[api] Shell session requested: {device_id} by {user['username']}")
    await _push_log_event(device_id, "info", "controller",
                          f"Shell session opened by {user['username']}")

    loop = asyncio.get_event_loop()
    done_future = loop.create_future()
    _shell_pending[device_id]   = done_future
    _shell_dashboard[device_id] = ws
    # Do NOT set _shell_ws or acquire _shell_lock — interactive sessions
    # bypass the programmatic shell mechanism entirely.

    try:
        # pty:true — interactive terminal wants a real PTY (mksh prompt,
        # line editing, top/vi, resize). Old firmware ignores the field and
        # opens the legacy pipe; handle_shell reports the established mode
        # to the dashboard via shell_meta. Programmatic sessions
        # (_get_device_shell_ws) deliberately do not set it.
        await live.send_control({"type": "shell_open", "pty": True})
        await done_future
    except Exception as e:
        log.warning(f"[api] Shell session error ({device_id}): {e}")
    finally:
        _shell_pending.pop(device_id, None)
        _shell_dashboard.pop(device_id, None)
        await live.send_control({"type": "shell_close"})
        log.info(f"[api] Shell session closed: {device_id}")
        await _push_log_event(device_id, "info", "controller",
                              f"Shell session closed by {user['username']}")

    return ws


# ─── Releases ─────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_latest_release(request: web.Request) -> web.Response:
    """GET /api/releases/latest — latest GitHub release, from cache."""
    release = await _get_cached_release()
    if release is None:
        return _error("no_release", "No release information available", 404)
    return _ok(release)


@auth.require_admin
async def _post_check_release(request: web.Request) -> web.Response:
    """POST /api/releases/check — force re-poll GitHub."""
    release = await _fetch_latest_release(force=True)
    if release is None:
        return _error("no_release", "Could not fetch release from GitHub", 502)
    return _ok(release)


@auth.require_admin
async def _post_deploy_all(request: web.Request) -> web.Response:
    """
    POST /api/releases/deploy

    Deploy to all connected, approved, non-current devices.
    Accepts optional {"upload_token": "..."} to deploy a local binary
    to the whole fleet instead of the latest GitHub release.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    upload_token = body.get("upload_token")
    binary_override = None
    release = None

    if upload_token:
        binary_override = _pending_uploads.pop(upload_token, None)
        if binary_override is None:
            return _error("invalid_token", "Upload token not found or expired", 404)
        release = {"version": f"local-{time.strftime('%Y%m%d-%H%M')}", "url": None}
    else:
        release = await _get_cached_release()
        if release is None:
            return _error("no_release", "No release information available", 409)

    started = []
    skipped = []
    loop = asyncio.get_event_loop()

    for device_id, live in list(_devices.items()):
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row is None or not row["approved"]:
            skipped.append({"device_id": device_id, "reason": "not_approved"})
            continue
        if not upload_token and row["firmware_ver"] == release["version"]:
            skipped.append({"device_id": device_id, "reason": "already_current"})
            continue
        if device_id in _updates_in_progress:
            skipped.append({"device_id": device_id, "reason": "update_in_progress"})
            continue

        asyncio.create_task(_run_update(device_id, release, binary_override))
        started.append(device_id)

    return _ok({
        "version": release["version"],
        "started": started,
        "skipped": skipped,
    }, status=202)


# ─── Provisioning ─────────────────────────────────────────────────────────────

# Device payloads — files the controller distributes to devices (provisioning
# wizard today; script/component OTA tomorrow). One canonical copy on disk,
# read per-request so edits ship without a restart. device/scripts/
# start_server.sh is a symlink into this directory.
PAYLOADS_DIR = Path(__file__).parent / "device_payloads"


def _read_payload(name: str) -> str:
    path = PAYLOADS_DIR / name
    if not path.is_file():
        raise web.HTTPInternalServerError(
            text=f"Payload {name} missing from {PAYLOADS_DIR} — broken install/image"
        )
    return path.read_text()


@auth.require_admin
async def _get_provision_start_script(request: web.Request) -> web.Response:
    """GET /api/provision/start_script — serves the EchoMuse startup script."""
    return web.Response(
        text=_read_payload("start_server.sh"),
        content_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="start_server.sh"'},
    )


@auth.require_admin
async def _get_provision_debloat_script(request: web.Request) -> web.Response:
    """GET /api/provision/debloat_script — the Magisk service.d boot script
    that re-stops init-launched daemons each boot (Debloat wizard step)."""
    return web.Response(
        text=_read_payload("echomuse-debloat.sh"),
        content_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="echomuse-debloat.sh"'},
    )


@auth.require_admin
async def _get_provision_debloat_packages(request: web.Request) -> web.Response:
    """GET /api/provision/debloat_packages — the pm-hide package list as JSON.

    Parsed server-side (comments/blank lines stripped) so the wizard never
    has to understand the file format and list edits ship without a
    dashboard rebuild.
    """
    packages = [
        line.strip()
        for line in _read_payload("debloat_packages.txt").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return _ok({"packages": packages})


@auth.require_admin
async def _get_provision_latest_binary(request: web.Request) -> web.Response:
    """
    GET /api/provision/latest_binary — serves the bytes of the latest
    GitHub release binary, for the provisioning wizard's "Install latest
    from GitHub" step.

    Distinct from /api/releases/latest (metadata only: {version, url}) —
    this route does the actual download from GitHub on the server side
    and streams the binary back, since a freshly-flashed device isn't
    registered in _devices yet and can't go through the
    /api/devices/{id}/update fleet-OTA path (that requires a live
    WebSocket session). Reuses the same cache/fetch machinery as OTA.
    """
    release = await _get_cached_release()
    if release is None:
        return _error("no_release", "No release information available", 404)

    binary = await _fetch_binary(release["url"])
    if binary is None:
        return _error("fetch_failed", "Could not download binary from GitHub", 502)

    return web.Response(
        body=binary,
        content_type='application/octet-stream',
        headers={
            'Content-Disposition': 'attachment; filename="server"',
            'X-Release-Version': release["version"],
        },
    )


@auth.require_admin
async def _get_provision_magisk_db(request: web.Request) -> web.Response:
    """GET /api/provision/magisk_db — generates a pre-seeded Magisk grant DB.

    Grants uid 2000 (adb shell) and uid 0 (root) unconditional su access so
    the screenless Echo Dot never shows a grant dialog.
    """
    def _build_db() -> bytes:
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            con = _sqlite3.connect(path)
            # Schema confirmed against a real Magisk v17.3 device dump
            # (sqlite> .schema on a working /data/adb/magisk.db) — NOT
            # guessed. magiskd queries settings and strings on every su
            # request regardless of whether anything's stored in them;
            # the previous version of this function only created
            # `policies` (and with the wrong columns — no package_name in
            # the real schema, PRIMARY KEY is uid alone). Missing
            # settings/strings meant every single su call hit
            # "sqlite3_exec: no such table" on each of those two tables
            # and got hard-rejected — which looked like a hang from the
            # wizard side because su was taking up to ~60s per rejection
            # cycle, far longer than the wizard's retry loop accounted for.
            con.execute(
                "CREATE TABLE policies ("
                "  uid INT,"
                "  policy INT,"
                "  until INT,"
                "  logging INT,"
                "  notification INT,"
                "  PRIMARY KEY(uid)"
                ")"
            )
            con.execute(
                "CREATE TABLE settings (key TEXT, value INT, PRIMARY KEY(key))"
            )
            con.execute(
                "CREATE TABLE strings (key TEXT, value TEXT, PRIMARY KEY(key))"
            )
            con.execute(
                "CREATE TABLE denylist (package_name TEXT, process TEXT, "
                "PRIMARY KEY(package_name, process))"
            )
            # policy=2 → always grant. Matches the confirmed real schema:
            # uid, policy, until, logging, notification — no package_name.
            con.execute("INSERT INTO policies (uid, policy, until, logging, notification) VALUES (2000, 2, 0, 1, 1)")
            con.execute("INSERT INTO policies (uid, policy, until, logging, notification) VALUES (0, 2, 0, 1, 1)")
            con.commit()
            con.close()
            return Path(path).read_bytes()
        finally:
            os.unlink(path)

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _build_db)
    return web.Response(
        body=data,
        content_type='application/octet-stream',
        headers={'Content-Disposition': 'attachment; filename="magisk.db"'},
    )


# ─── Device-link TLS credentials ──────────────────────────────────────────────

# Canonical on-device credential paths — coupled with
# device/internal/client/tlscreds.go. The Go client re-reads them on every
# dial attempt, so pushed credentials take effect on the next reconnect
# without a firmware restart.
DEVICE_TLS_DIR = "/data/local/etc/echomuse"


@auth.require_admin
async def _post_provision_tls_credentials(request: web.Request) -> web.Response:
    """
    POST /api/provision/tls_credentials  {device_id}

    Provisioning-wizard path: returns the CA cert plus the device's link
    token (minting one — and a pending device row — if needed) so the
    wizard can install them over adb before the device's first contact.
    """
    if _tls_dir is None:
        return _error("tls_unavailable",
                      "Device-link TLS is not active on this controller", 503)
    body      = await _json_body(request)
    device_id = _require_str(body, "device_id")

    loop  = asyncio.get_event_loop()
    token = await loop.run_in_executor(None, db.ensure_device_token, device_id)
    return _ok({
        "ca_pem": em_pki.ca_pem(_tls_dir),
        "token":  token,
        "dir":    DEVICE_TLS_DIR,
    })


@auth.require_admin
async def _post_secure_link(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/secure_link

    Fleet path for already-provisioned devices: pushes ca.pem + token to
    the device over the (still-plain) shell plane, then bounces the
    control connection so the device redials — over wss, now that the CA
    file exists. Requires the device to be connected.
    """
    if _tls_dir is None:
        return _error("tls_unavailable",
                      "Device-link TLS is not active on this controller", 503)
    device_id = request.match_info["id"]
    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", f"Device not connected: {device_id}", 409)

    task = asyncio.create_task(_run_secure_link(device_id))
    task.add_done_callback(_log_task_exception_api)
    return _ok({"started": True})


def _log_task_exception_api(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"[api] Unhandled exception in background task: {exc}", exc_info=exc)


async def _run_secure_link(device_id: str) -> None:
    """Background task: install TLS credentials on a live device."""
    loop = asyncio.get_event_loop()
    live = _devices.get(device_id)
    if live is None:
        return
    try:
        await _push_log_event(device_id, "info", "controller",
                              "Secure link: pushing TLS credentials")
        token = await loop.run_in_executor(None, db.ensure_device_token, device_id)
        ca    = em_pki.ca_pem(_tls_dir)

        await _shell_run(live, f"mkdir -p {DEVICE_TLS_DIR}")
        await asyncio.sleep(1.0)  # let the shell session close cleanly

        ok = await _stream_file_to_device(
            live, ca.encode("ascii"), f"{DEVICE_TLS_DIR}/ca.pem", mode="644")
        if ok:
            await asyncio.sleep(1.0)
            ok = await _stream_file_to_device(
                live, token.encode("ascii"), f"{DEVICE_TLS_DIR}/token", mode="600")
        if not ok:
            await _push_log_event(device_id, "error", "controller",
                                  "Secure link: credential transfer failed")
            return

        await _push_log_event(
            device_id, "info", "controller",
            "Secure link: credentials installed — bouncing connection to switch to wss")
        # The Go client reloads credentials on every dial, so a reconnect
        # is enough to move to the TLS listener.
        try:
            await live.control_ws.close()
        except Exception:
            pass
    except Exception as e:
        log.exception(f"[api] Secure link failed for {device_id}: {e}")
        await _push_log_event(device_id, "error", "controller",
                              f"Secure link failed: {e}")


# ─── System ───────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_system_status(request: web.Request) -> web.Response:
    """GET /api/system/status"""
    loop = asyncio.get_event_loop()
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    release = await _get_cached_release()

    return _ok({
        "controller_version": CONTROLLER_VERSION,
        "connected":      len(_devices),
        "total_devices":  len(all_rows),
        "pending":        sum(1 for r in all_rows if not r["approved"]),
        "approval_mode":  db.get_config("device_approval", "strict"),
        "latest_release": release["version"] if release else None,
        "updates_available": sum(
            1 for r in all_rows
            if r["firmware_ver"] and release
            and r["firmware_ver"] != release["version"]
        ),
    })


@auth.require_admin
async def _get_system_config(request: web.Request) -> web.Response:
    """GET /api/system/config — full system_config table."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_all_config)
    # Don't expose schema_version — internal detail
    config.pop("schema_version", None)
    return _ok(config)


@auth.require_admin
async def _patch_system_config(request: web.Request) -> web.Response:
    """
    PATCH /api/system/config

    Body: {key: value, ...}
    Only known, mutable keys are accepted.
    """
    MUTABLE_KEYS = {
        "device_approval",
        "session_expiry_days",
        "update_check_interval",
        "github_repo",
    }
    body = await _json_body(request)
    loop = asyncio.get_event_loop()

    updated = {}
    unknown = []
    for key, value in body.items():
        if key not in MUTABLE_KEYS:
            unknown.append(key)
            continue
        await loop.run_in_executor(None, db.set_config, key, str(value))
        updated[key] = value

    if unknown:
        return _error(
            "unknown_config_key",
            f"Unknown or immutable config key(s): {', '.join(unknown)}",
            400,
        )
    return _ok(updated)


# ─── Global device config ─────────────────────────────────────────────────────

@auth.require_auth
async def _get_global_config(request: web.Request) -> web.Response:
    """GET /api/global/config — fleet-wide default device config."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_global_device_config)
    return _ok(config)


@auth.require_admin
async def _post_global_config(request: web.Request) -> web.Response:
    """
    POST /api/global/config

    Persists new fleet-wide device defaults, then pushes the updated config
    to every currently-connected device that still has use_global_config=1.
    Devices with per-device overrides are not affected.
    """
    config = await _json_body(request)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.set_global_device_config, config)

    # Push to all connected devices on global config
    pushed = []
    for device_id, live in list(_devices.items()):
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row is None or not row["use_global_config"]:
            continue
        await live.send_control({"type": "config", **config})
        if "owwThreshold" in config:
            live.oww_threshold = float(config["owwThreshold"])
        if "owwModel" in config:
            live.oww_model = config["owwModel"]
            # Refresh HA's wake-word dropdown (lazy import — em_esphome
            # imports em_api at module level).
            import em_esphome
            em_esphome.update_oww_model(device_id, config["owwModel"])
        if "owwSpeexNs" in config:
            live.oww_speex_ns = bool(config["owwSpeexNs"])
        if "nsAsr" in config:
            live.ns_asr = bool(config["nsAsr"])
        if "bargeInEnabled" in config:
            live.barge_in_enabled = bool(config["bargeInEnabled"])
        if "bargeInThreshold" in config:
            live.barge_threshold = float(config["bargeInThreshold"])
        if "eqBands" in config:
            live.eq_bands = config["eqBands"]
        if "eqLoudness" in config:
            live.eq_loudness = bool(config["eqLoudness"])
        live.led_scene = em_scenes.resolve(config)
        pushed.append(device_id)

    if pushed:
        log.info(f"[api] Global config pushed to {len(pushed)} device(s): {pushed}")

    # Reconcile BT proxies for every approved on-global device — offline
    # ones included (proxy mDNS/port lifecycle is independent of the
    # device connection, unlike the config push above).
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    for row in all_rows:
        if row["approved"] and row["use_global_config"]:
            await em_ble_proxy.reconcile(row["device_id"])

    return _ok({"config": config, "pushed_to": pushed})


# ─── Auth — change password ───────────────────────────────────────────────────

@auth.require_auth
async def _post_change_password(request: web.Request) -> web.Response:
    """
    POST /api/auth/change-password

    Body: {current_password, new_password}
    Any authenticated user can change their own password.
    Verifies current password before accepting the new one.
    """
    user = request["user"]
    body = await _json_body(request)
    current_password = _require_str(body, "current_password")
    new_password     = _require_str(body, "new_password")

    if len(new_password) < 8:
        return _error("invalid_input", "New password must be at least 8 characters", 400)

    loop = asyncio.get_event_loop()
    db_user = await loop.run_in_executor(None, db.get_user_by_id, user["id"])
    if db_user is None:
        return _error("user_not_found", "User not found", 404)

    if not await auth.verify_password_async(current_password, db_user["password_hash"]):
        return _error("invalid_credentials", "Current password is incorrect", 401)

    new_hash = await auth.hash_password_async(new_password)
    await loop.run_in_executor(None, db.update_user_password, user["id"], new_hash)
    log.info(f"[api] Password changed for user: {user['username']}")
    return _ok({"ok": True})


# ─── Live events WebSocket ────────────────────────────────────────────────────

async def _ws_events(request: web.Request) -> web.WebSocketResponse:
    """
    WS /api/events

    Readonly access required. Dashboard connects once on load.
    Controller pushes device state changes, logs, and pending alerts
    in real time — no polling needed.
    """
    user = await auth.ws_resolve_session(request)
    if user is None:
        raise web.HTTPUnauthorized()

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _event_clients.add(ws)
    log.debug(f"[api] Events client connected ({user['username']}) "
              f"— {len(_event_clients)} total")

    try:
        # Send full device snapshot on connect so the dashboard has
        # immediate state without waiting for the first push event.
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, db.get_all_devices)
        await ws.send_str(json.dumps({
            "type":    "snapshot",
            "devices": [_merge_device(r) for r in rows],
        }))

        async for msg in ws:
            # Client shouldn't send anything, but handle gracefully
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    finally:
        _event_clients.discard(ws)
        log.debug(f"[api] Events client disconnected — "
                  f"{len(_event_clients)} remaining")

    return ws


async def _push_event(event: dict) -> None:
    """
    Broadcast a JSON event to all connected /api/events clients.

    Called by route handlers and background tasks whenever device
    state changes.
    """
    if not _event_clients:
        return
    payload = json.dumps(event)
    dead = set()
    for ws in _event_clients:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    _event_clients.difference_update(dead)


async def _push_log_event(
    device_id: str,
    level: str,
    source: str,
    message: str,
) -> None:
    """
    Persist a controller-generated log entry and push it to event clients.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.log_device, device_id, level, source, message)
    await _push_event({
        "type":      "device_log",
        "device_id": device_id,
        "entry": {
            "ts":      int(time.time() * 1000),
            "level":   level,
            "source":  source,
            "message": message,
        },
    })


# ─── GitHub release fetching ──────────────────────────────────────────────────

async def _get_cached_release() -> Optional[dict]:
    """
    Return the latest release info, using the in-memory cache if fresh.
    Falls back to the DB cache if the in-memory cache is cold.
    Triggers a background fetch if the DB cache is stale.
    """
    global _release_cache, _release_cache_ts

    # In-memory cache hit
    if _release_cache and (time.monotonic() - _release_cache_ts) < RELEASE_CACHE_TTL:
        return _release_cache

    # Load from DB cache
    version = db.get_config("latest_version")
    url     = db.get_config("latest_binary_url")
    last_check = db.get_config("last_update_check")

    if version and url:
        _release_cache = {"version": version, "url": url}
        _release_cache_ts = time.monotonic()

        # Re-poll in background if DB cache is older than check interval
        interval = int(db.get_config("update_check_interval", "3600") or 3600)
        if not last_check or (time.time() - float(last_check)) > interval:
            asyncio.create_task(_fetch_latest_release())

        return _release_cache

    # No cache at all — fetch synchronously
    return await _fetch_latest_release()


async def _fetch_latest_release(force: bool = False) -> Optional[dict]:
    """
    Poll the GitHub releases API and update the DB cache.

    Returns the release dict or None on failure.
    """
    global _release_cache, _release_cache_ts

    repo = db.get_config("github_repo", "wilbowes/EchoMuse")
    url  = GITHUB_API_URL.format(repo=repo)

    log.info(f"[api] Polling GitHub releases: {url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[api] GitHub API returned {resp.status}")
                    return None
                releases = await resp.json()

        # Newest device firmware release: plain v* tag (controller releases
        # use controller-v* and ship no binary), published, with the compiled
        # `server` asset attached. The list is newest-first.
        tag = None
        binary = None
        for data in releases:
            if data.get("draft") or data.get("prerelease"):
                continue
            candidate_tag = data.get("tag_name", "")
            if not candidate_tag.startswith("v"):
                continue
            candidate_binary = next(
                (a for a in data.get("assets", []) if a.get("name") == "server"),
                None,
            )
            if candidate_binary is None:
                continue
            tag, binary = candidate_tag, candidate_binary
            break

        if binary is None:
            log.warning("[api] No device firmware release with a 'server' asset found")
            return None

        download_url = binary["browser_download_url"]

        # Persist to DB
        db.set_config("latest_version",    tag)
        db.set_config("latest_binary_url", download_url)
        db.set_config("last_update_check", str(time.time()))

        # Update in-memory cache
        _release_cache    = {"version": tag, "url": download_url}
        _release_cache_ts = time.monotonic()

        log.info(f"[api] Latest release: {tag}")
        return _release_cache

    except Exception as e:
        log.error(f"[api] GitHub release fetch failed: {e}")
        return None


async def _fetch_binary(download_url: str) -> Optional[bytes]:
    """Download the binary from a GitHub release asset URL."""
    log.info(f"[api] Fetching binary: {download_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                download_url,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    log.error(f"[api] Binary download failed: HTTP {resp.status}")
                    return None
                return await resp.read()
    except Exception as e:
        log.error(f"[api] Binary download exception: {e}")
        return None


# ─── Periodic background tasks ────────────────────────────────────────────────

async def release_poll_loop() -> None:
    """
    Periodically poll GitHub for new releases.

    Runs as an asyncio task started from em_controller.main().
    Interval is read from system_config each iteration so it can be
    changed at runtime without restart.
    """
    # Initial delay — let the controller finish starting up
    await asyncio.sleep(30)

    while True:
        try:
            await _fetch_latest_release()
        except Exception as e:
            log.error(f"[api] Release poll loop error: {e}")

        interval = int(db.get_config("update_check_interval", "3600") or 3600)
        await asyncio.sleep(interval)


async def session_prune_loop() -> None:
    """Prune expired sessions hourly."""
    while True:
        await asyncio.sleep(3600)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, db.prune_sessions)
        except Exception as e:
            log.error(f"[api] Session prune error: {e}")


# ─── Helpers shared across em_controller ─────────────────────────────────────

async def notify_device_connected(device_id: str, version: str | None = None) -> None:
    """
    Called by em_controller when a device successfully registers.

    Includes firmware_ver in the event so the dashboard's device cache is
    updated immediately on reconnect — prevents a stale-cache false-positive
    where the frontend sees the old version during an OTA reconnect window
    and incorrectly shows an auto-rollback warning.

    Pass version directly from the device handshake (preferred — no DB round-trip).
    If omitted, falls back to a DB lookup; assumes em_controller has already
    written the new firmware_ver before calling this.
    """
    event: dict = {"type": "device_connected", "device_id": device_id}
    if version is not None:
        event["firmware_ver"] = version
    else:
        loop = asyncio.get_event_loop()
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row:
            event["firmware_ver"] = row["firmware_ver"]
    await _push_event(event)


async def notify_device_disconnected(device_id: str) -> None:
    """Called by em_controller when a device disconnects."""
    await _push_event({"type": "device_disconnected", "device_id": device_id})


async def notify_device_pending(device_id: str, ip: str) -> None:
    """Called by em_controller when an unapproved device attempts connection."""
    await _push_event({
        "type":      "device_pending",
        "device_id": device_id,
        "ip":        ip,
    })


# ─── Response helpers ─────────────────────────────────────────────────────────

def _ok(data, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps(data),
    )


def _error(code: str, message: str, status: int) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps({"error": message, "code": code}),
    )


# ─── Request helpers ──────────────────────────────────────────────────────────

async def _json_body(request: web.Request) -> dict:
    """
    Parse the request body as JSON.
    Returns 400 if body is missing or not valid JSON.
    """
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({
                "error": "Request body must be valid JSON",
                "code":  "invalid_json",
            }),
        )


def _require_str(body: dict, key: str) -> str:
    """Extract a required string field from a parsed JSON body."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({
                "error": f"Missing or empty required field: {key}",
                "code":  "missing_field",
            }),
        )
    return value.strip()


# ─── Device state merge ───────────────────────────────────────────────────────

def _merge_device(row) -> dict:
    """
    Merge a DB device row with live in-memory state.

    DB row provides persistent fields (label, config, firmware_ver etc).
    Live _devices dict provides transient state (connected, speaking,
    muted, listening, thinking).
    """
    device_id = row["device_id"]
    live = _devices.get(device_id)

    return {
        # Persistent
        "device_id":          device_id,
        "label":              row["label"],
        "approved":           bool(row["approved"]),
        "ip":                 row["ip"],
        "firmware_ver":       row["firmware_ver"],
        "firmware_previous":  row["firmware_previous"],
        "first_seen":         row["first_seen"],
        "last_seen":          row["last_seen"],
        "config":             json.loads(row["config"] or "{}"),
        "use_global_config":  bool(row["use_global_config"]),
        "esphome_port":       row["esphome_api_port"],
        "ble_proxy_port":     row["ble_proxy_port"],
        # Live — defaults when device is not connected
        "connected":        live is not None,
        "speaking":         live.speaking  if live else False,
        "muted":            getattr(live, "muted",     False) if live else False,
        "listening":        getattr(live, "listening", False) if live else False,
        "thinking":         getattr(live, "thinking",  False) if live else False,
        "stats":            live.stats if live else None,
        # Controller-side BT proxy state — non-None only while the device's
        # bleProxyEnabled config has a proxy server instantiated.
        "bleProxy":         em_ble_proxy.get_status(device_id),
        # Device-link security: token issued (persistent) + whether the
        # current control connection came in over the TLS listener (live).
        "linkTokenIssued":  bool(row["token"]) if "token" in row.keys() else False,
        "linkTls":          getattr(live, "secure", False) if live else False,
        # Q4 fix (2026-07-05 review): near-miss counter — same lifecycle as
        # the rest of this "Live" section (resets on reconnect, since it
        # lives on the per-connection Device object, not the DB row).
        "owwNearMisses":    getattr(live, "oww_near_misses", 0) if live else 0,
        # WiFi change state (survives the reconnect a change causes)
        "wifi":             wifi_state(device_id),
        # Update state
        "update_in_progress": device_id in _updates_in_progress,
        # Last OTA/rollback failure (None when the last attempt succeeded or
        # none was made) — lets the dashboard show a terminal ✗ state instead
        # of "updating…" forever when an update aborts.
        "update_error":       _update_errors.get(device_id),
    }
