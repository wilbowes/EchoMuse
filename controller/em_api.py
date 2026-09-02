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
import html as _html
import json
import logging
import os
import platform
import re
import shutil
import sqlite3 as _sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
import websockets

import em_db as db
import em_auth as auth
import em_ble_proxy
import em_config_sections as sections_mod
import em_firmware
import em_ingressauth
import em_oww_assets
import em_oww_models
import em_pki
import em_player
import em_recordings
import em_volume
import em_scenes
import em_shadow
import em_support
from version import VERSION as CONTROLLER_VERSION
from version import compare as _compare_versions
from version import parse as _parse_version

log = logging.getLogger("echomuse.api")

# Import time, which is startup: em_controller imports this module before it
# serves anything. Close enough to process start for "how long has it been up",
# and it needs no procfs.
_PROCESS_START = time.time()

# CPU over 1m/5m/1h, fed by the event-loop lag monitor (see sample_cpu). The
# ring is bounded by its longest window; nothing here runs per request.
_cpu_history = em_support.CpuHistory()
CPU_SAMPLE_INTERVAL_S = em_support.CpuHistory.INTERVAL_S

# The controller's own recent log, kept in memory for support bundles. Every
# line that would have explained #62 goes to stdout, and stdout was not in
# the bundle at all.
_log_ring = em_support.LogRing()


def install_log_ring(fmt: str) -> None:
    """
    Attach the in-memory log ring to the root logger.

    Called from em_controller once logging is configured, with the same
    format string, so a bundle reads exactly like the console does.
    """
    _log_ring.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(_log_ring)


def sample_cpu() -> None:
    """
    Take one CPU sample. Called from em_controller's existing 1s ticker, not
    on a task of its own — the cost is one os.times() every INTERVAL_S.
    """
    _cpu_history.add(time.monotonic(), sum(os.times()[:2]))

# ─── Config ───────────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
# Set when running as a Home Assistant add-on (config.yaml's `environment`
# block) — gates the ingress-only middleware below. Unset for every other
# deployment (docker-compose, bare python), which keeps serving the
# dashboard directly exactly as before.
INGRESS_ONLY = os.environ.get("ECHOMUSE_HOME_ASSISTANT_INGRESS") == "true"
# Home Assistant Supervisor's ingress reverse proxy always calls in from this
# fixed address on the internal hassio Docker network.
INGRESS_GATEWAY_IP = "172.30.32.2"
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

# Controller releases are `controller-v*` TAGS with no GitHub Release behind
# them — controller-release.yml publishes a GHCR image and nothing else (see
# "Versioning / releases" in CLAUDE.md). So the notes come from the tag's own
# annotation: matching-refs lists the tags, and an annotated tag's object
# carries the message.
#
# Deliberately NOT solved by publishing GitHub Releases for controller tags.
# The releases list is the DEVICE firmware's update feed and
# _fetch_latest_release scans it for the newest v* tag carrying a `server`
# asset; adding controller rows puts non-firmware entries in front of that
# scan for no gain, when the annotation we already write says the same thing.
GITHUB_TAGS_URL = (
    "https://api.github.com/repos/{repo}/git/matching-refs/tags/controller-v"
)
GITHUB_TAG_OBJECT_URL = "https://api.github.com/repos/{repo}/git/tags/{sha}"

_controller_cache: dict = {}
_controller_cache_ts: float = 0.0

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

# Firmware updates are SERIALISED across the whole controller, not just per
# device. Three concurrent OTAs stalled the event loop for 11.1 seconds on
# 2026-09-02 — measured, in the `[loop] event loop stalled` warnings — and
# that loop is what sends speaker periods and LED frames, so a device mid
# response pays for a device being updated. The transfer is base64 over the
# shell plane and CPU-bound in this process; the fix is to stop doing several
# at once, not to make one cheaper.
#
# A device-level guard cannot do this: `_updates_in_progress` stops one device
# being updated twice, and says nothing about two devices being updated at
# once. So the lock is global and BOTH entry points go through it — the fleet
# deploy and a hand-clicked single update, which collide identically.
#
# A failure does NOT stop the queue. Wil's call, 2026-09-02: mark it, carry on,
# report at the end — one device that will not come back should not strand the
# rest of a fleet update behind it.
_ota_lock = asyncio.Lock()

# Longest one device may hold the OTA queue. A real update is a ~10MB transfer,
# a reboot and a 90s reconnect watch, so this is a deadlock cap rather than a
# performance budget: it exists because serialising made one wedged device able
# to block the whole fleet's updates until the controller restarted.
OTA_MAX_HOLD_S = 300.0

# Devices waiting on `_ota_lock`. Reported separately from
# `update_in_progress` so the dashboard says "queued" rather than claiming
# work that has not started — the same rule as everywhere else here: a control
# that cannot act must say so rather than appear to work.
_updates_queued: set[str] = set()

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

# Largest firmware binary /api/releases/upload will accept. Roughly 5x the
# current ~10.7 MB build, so it bounds memory (the upload is held in RAM until
# deployed or expired) without needing revision every release. The aiohttp
# transport limit is set ABOVE this in create_app so this is the ceiling a
# user actually meets, with a message that names it.
UPLOAD_MAX_BYTES = 50 * 1024 * 1024

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
    # client_max_size defaults to 1 MB in aiohttp and the firmware is ~10.7 MB,
    # so /api/releases/upload rejects every real binary without this. Both
    # callers post there: the dashboard's Local Build panel and
    # controller/tools/ota.py.
    #
    # THIS IS A REGRESSION FROM AN AIOHTTP BUMP, NOT AN OLD BUG, and the
    # difference matters for what else to distrust. Measured across the two
    # pinned versions with a 3 MB multipart POST at a default Application:
    #
    #   aiohttp 3.13.5  -> 200   (streaming multipart bypassed the limit)
    #   aiohttp 3.14.3  -> 413   HTTPRequestEntityTooLarge
    #
    # 3.13.5 held from May until 2026-08-18, when #129's routine half moved to
    # 3.14.3 and broke local deploys silently — no CI job posts a real-sized
    # body at this endpoint, and the ordinary release path never touches it
    # (em_firmware fetches controller-side and pushes from there), so only a
    # developer deploying a local build ever met it.
    #
    # Set ABOVE the handler's limit on purpose: the handler's 413 names the
    # actual ceiling ("Binary exceeds 50 MB limit"), and it can only be the
    # error a user sees if the transport lets the body through first. A
    # transport limit equal to the application limit means the useful message
    # is unreachable by construction.
    app = web.Application(
        middlewares=[_ingress_only_middleware, _error_middleware],
        client_max_size=UPLOAD_MAX_BYTES + 8 * 1024 * 1024,
    )

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
    # Public: decides for itself whether the request is a genuine ingress
    # request. Requiring a session here would defeat the purpose.
    app.router.add_post("/api/auth/ingress",          _post_ingress_login)
    app.router.add_post("/api/auth/logout",          _post_logout)
    app.router.add_get("/api/auth/me",               _get_me)
    app.router.add_post("/api/auth/change-password", _post_change_password)

    # Users — roles. There was no way to change one at all until 2026-08-14,
    # which only became load-bearing when ingress started provisioning
    # accounts the operator never created.
    app.router.add_get("/api/users",        _get_users)
    app.router.add_patch("/api/users/{id}", _patch_user)

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
    app.router.add_get("/api/devices/{id}/turns/{turn}/audio", _get_turn_audio)
    app.router.add_post("/api/devices/{id}/wifi",         _post_device_wifi)
    app.router.add_post("/api/devices/{id}/wifi/scan",    _post_device_wifi_scan)
    app.router.add_post("/api/devices/{id}/update",       _post_device_update)
    app.router.add_post("/api/devices/{id}/rollback",     _post_device_rollback)
    app.router.add_post("/api/releases/upload",           _post_upload_binary)

    # Custom wake-word models (oww_forge output → data/oww_models/)
    app.router.add_get("/api/oww_models",             _get_oww_models)
    app.router.add_post("/api/oww_models/upload",     _post_oww_model_upload)
    app.router.add_delete("/api/oww_models/{file}",   _delete_oww_model)
    app.router.add_get("/api/devices/{id}/shell",         _ws_shell)
    app.router.add_get("/api/devices/{id}/oww_assets",    _get_oww_assets)
    app.router.add_post("/api/devices/{id}/oww_assets",   _post_oww_assets)

    # Releases
    app.router.add_get("/api/releases/latest",   _get_latest_release)
    app.router.add_get("/api/releases/controller", _get_controller_release)
    app.router.add_post("/api/releases/check",   _post_check_release)
    app.router.add_post("/api/releases/deploy",  _post_deploy_all)

    # Global device config
    app.router.add_get("/api/global/config",   _get_global_config)
    app.router.add_post("/api/global/config",  _post_global_config)

    # System
    app.router.add_get("/api/support/bundle",  _get_support_bundle)
    app.router.add_get("/api/system/status",    _get_system_status)
    app.router.add_get("/api/system/config",    _get_system_config)
    app.router.add_patch("/api/system/config",  _patch_system_config)

    # Provisioning
    app.router.add_get("/api/provision/start_script", _get_provision_start_script)
    app.router.add_get("/api/provision/debloat_script",   _get_provision_debloat_script)
    app.router.add_get("/api/provision/debloat_packages", _get_provision_debloat_packages)
    app.router.add_get("/api/provision/magisk_db",    _get_provision_magisk_db)
    app.router.add_get("/api/provision/latest_binary", _get_provision_latest_binary)
    app.router.add_get("/api/provision/oww_assets",    _get_provision_oww_manifest)
    app.router.add_get("/api/provision/oww_asset/{name}", _get_provision_oww_asset)
    app.router.add_post("/api/provision/tls_credentials", _post_provision_tls_credentials)
    app.router.add_post("/api/provision/diagnostics",     _post_provision_diagnostics)
    app.router.add_post("/api/devices/{id}/secure_link",  _post_secure_link)
    app.router.add_post("/api/devices/{id}/debloat",      _post_debloat)

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
async def _ingress_only_middleware(request: web.Request, handler):
    """
    As a Home Assistant add-on, the dashboard/API must only be reachable
    through the authenticated ingress gateway — the add-on has no other
    auth in front of it on the LAN otherwise. No-op (INGRESS_ONLY unset)
    for every deployment that isn't the add-on.
    """
    if INGRESS_ONLY and request.remote != INGRESS_GATEWAY_IP:
        log.warning("Rejected non-ingress request from %s", request.remote)
        raise web.HTTPForbidden(text="Home Assistant Ingress is required")
    return await handler(request)


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

def _with_ingress_base(page: str, request: web.Request) -> str:
    """
    Inject a <base href> so the page's relative asset/API paths resolve
    under Home Assistant's generated ingress path (e.g.
    /api/hassio_ingress/<token>/) instead of the site root. A no-op string
    (base_path "/") outside ingress, where the page is already at root.
    """
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    base_path = f"{ingress_path}/" if ingress_path else "/"
    base_tag = f'<base href="{_html.escape(base_path, quote=True)}">'
    return page.replace("<head>", f"<head>\n  {base_tag}", 1)


async def _serve_spa(request: web.Request) -> web.Response:
    """Serve index.html for all SPA routes."""
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(
            status=503,
            text="Dashboard not built — static/index.html not found",
        )
    return web.Response(
        text=_with_ingress_base(index.read_text(encoding="utf-8"), request),
        content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


async def _serve_dashboard(request: web.Request) -> web.Response:
    """
    Serve dashboard.html for /dashboard, with the JS bundle cache-busted.

    add_static sends Last-Modified and ETag but no Cache-Control, so browsers
    apply HEURISTIC freshness and serve a cached dashboard.js without
    revalidating. The failure mode is nasty because it is invisible from the
    server side: the deploy is correct, the file on disk is correct, the
    compiled bundle is correct, and the browser shows the previous UI — which
    reads as "my change did not work" and sends you looking in the wrong place.
    It cost exactly that on 2026-07-30 when the new thermal row did not appear.

    So the bundle URL carries the file's mtime. That changes on every rebuild
    regardless of version numbering (controller_version is "dev" for local
    builds and would not bust between two dev deploys), and the wrapper itself
    is sent no-cache so the new URL is always seen — it is 3KB, revalidating it
    costs nothing.
    """
    dashboard = STATIC_DIR / "dashboard.html"
    if not dashboard.exists():
        return web.Response(status=503, text="dashboard.html not found in static/")
    page = _with_ingress_base(dashboard.read_text(encoding="utf-8"), request)
    bundle = STATIC_DIR / "dashboard.js"
    if bundle.exists():
        page = page.replace(
            "static/dashboard.js",
            f"static/dashboard.js?v={int(bundle.stat().st_mtime)}",
        )
    return web.Response(
        text=page,
        content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


async def _redirect_root(request: web.Request) -> web.Response:
    # A relative Location preserves Home Assistant's generated ingress path
    # instead of bouncing the browser to the site root.
    raise web.HTTPFound(".")


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

@auth.require_admin
async def _get_users(request: web.Request) -> web.Response:
    """
    GET /api/users — accounts and their roles. ADMIN ONLY.

    Never returns password_hash. `ha_linked` says whether Home Assistant
    governs this account's role, which is what makes a refused PATCH
    explicable rather than arbitrary.
    """
    loop  = asyncio.get_event_loop()
    users = await loop.run_in_executor(None, db.get_all_users)
    return _ok([{
        "id":         u["id"],
        "username":   u["username"],
        "role":       u["role"],
        "ha_linked":  bool(u["ha_user_id"]),
        "created_at": u["created_at"],
    } for u in users])


@auth.require_admin
async def _patch_user(request: web.Request) -> web.Response:
    """
    PATCH /api/users/{id}  {role} — change a user's role. ADMIN ONLY.

    This is the ONLY way to promote someone, including accounts Home
    Assistant created through ingress — roles are not mirrored from HA, so
    nothing here is later overwritten by a login.

    One refusal: **never leave the install with no admin.** On the standalone
    container local accounts are the only auth, so an install with no admin
    has no way back in — and this endpoint is the one an admin reaches for
    while tidying up.
    """
    body = await _json_body(request)
    role = _require_str(body, "role")
    if role not in ("admin", "readonly"):
        return _error("bad_request", "role must be 'admin' or 'readonly'", 400)

    try:
        user_id = int(request.match_info["id"])
    except ValueError:
        return _error("bad_request", "user id must be numeric", 400)

    loop = asyncio.get_event_loop()
    user = await loop.run_in_executor(None, db.get_user_by_id, user_id)
    if user is None:
        return _error("user_not_found", f"No user: {user_id}", 404)

    if user["role"] == role:
        return _ok({"id": user_id, "role": role, "changed": False})

    if user["role"] == "admin":
        admins = await loop.run_in_executor(None, db.admin_count)
        if admins <= 1:
            return _error(
                "last_admin",
                "This is the only admin — promote someone else first", 409)

    await loop.run_in_executor(None, db.set_user_role, user_id, role)
    log.info(f"[api] {request['user']['username']} set "
             f"{user['username']} to {role}")
    return _ok({"id": user_id, "role": role, "changed": True})


async def _post_ingress_login(request: web.Request) -> web.Response:
    """
    POST /api/auth/ingress — authenticate as the Home Assistant user that
    Supervisor forwarded. → {token, role} or 401.

    Home Assistant has already authenticated this person; a second EchoMuse
    password would be a lock on a door that is already locked. Supervisor
    strips client-supplied copies of these headers before proxying, so their
    presence on a request that genuinely came from the gateway is proof of
    an authenticated HA session.

    Whether it *did* come from the gateway is em_ingressauth.decide's
    judgement, made from the deployment mode and the peer address together.
    A 401 here is not a failure — it is the ordinary answer everywhere that
    is not the add-on, and the dashboard falls back to the login form.
    """
    identity = em_ingressauth.decide(
        ingress_only=INGRESS_ONLY,
        remote=request.remote,
        user_id=request.headers.get("X-Remote-User-Id"),
        username=request.headers.get("X-Remote-User-Name"),
        display_name=request.headers.get("X-Remote-User-Display-Name"),
    )
    if identity is None:
        return _error("not_authenticated",
                      "Home Assistant authentication is not available", 401)

    token, role = await auth.login_via_ingress(identity)
    return _ok({"token": token, "role": role, "via": "ingress"})


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
    return _ok(_redact_turns_for(turns, request["user"]))


def _redact_turns_for(turns: list, user: dict) -> list:
    """
    Remove transcripts for a non-admin session.

    A transcript is the content of what someone said in their home — the
    same class of data as the recording it came from, differing only in
    format. Stripped HERE rather than hidden in the dashboard, because the
    dashboard is not what protects it: /api/devices/{id}/turns is a plain
    GET with a session token, so a UI-only rule protects nothing from
    anyone who opens the network tab.

    The rest of the row — timings, scores, outcome — is what the Activity
    tab is for and stays visible, so read-only access keeps its diagnostic
    value.
    """
    if user.get("role") == "admin":
        return turns
    return [{k: v for k, v in t.items() if k != "stt_text"} for t in turns]


@auth.require_admin
async def _get_turn_audio(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/turns/{turn}/audio — the saved mic audio for
    one voice turn, as a downloadable WAV. ADMIN ONLY.

    This is recognisable speech recorded in someone's home — the most
    sensitive thing the controller stores, and the reason saveUtterances is
    off by default. Under the add-on every Home Assistant user in the
    household can reach the dashboard (Supervisor's ingress view sets
    requires_auth=False and panel_admin only hides the sidebar entry), so
    read-only is no longer a synonym for "someone the operator trusts with
    the recordings".

    Only turns captured while saveUtterances was on have one, and only the
    newest em_recordings.KEEP_PER_DEVICE per device survive — a turn row
    older than that window still carries the filename but the file is gone,
    so a 404 here is an ordinary outcome, not an error state.

    The filename is derived from (device, turn) rather than taken from the
    row: em_recordings.resolve then re-checks that the file belongs to the
    device in the URL, so a turn id from another device can't be used to
    reach its audio."""
    device_id = request.match_info["id"]
    try:
        turn_id = int(request.match_info["turn"])
    except ValueError:
        return _error("bad_request", "turn must be an integer", 400)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    name = em_recordings.filename(device_id, turn_id)
    path = em_recordings.resolve(device_id, name) if name else None
    if path is None:
        return _error("no_recording",
                      "No saved audio for this turn", 404)

    label = _slug(row["label"] or device_id)
    return web.FileResponse(
        path,
        headers={
            "Content-Type":        "audio/wav",
            "Content-Disposition": f'attachment; filename="{label}-turn{turn_id}.wav"',
            # Recordings are immutable once written and their names are
            # unique per turn, but the retention window means a name can
            # stop resolving — so cache privately and briefly, never shared.
            "Cache-Control":       "private, max-age=60",
        },
    )


def _slug(text: str) -> str:
    """Lowercase ASCII slug, safe for a Content-Disposition filename."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return out or "device"


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

    # On-device shadow comparison (schema v13) — the verdict, computed here so
    # a reader is not left to derive it from raw columns.
    #
    # Denominator is turns where the device was KNOWN to be scoring
    # (dev_shadow=1); a NULL score on those is a genuine miss, whereas a NULL
    # anywhere else is absence of data and must not be counted either way.
    scoring    = [t for t in turns if (t["dev_shadow"] or 0) == 1]
    # A turn is only COMPARABLE if the device was scoring against a bar this
    # controller's wake would have cleared. During playback the controller drops
    # to bargeInThreshold, so a turn that fired at 0.055 was never something a
    # device scoring against 0.5 could have caught — counting those as misses
    # made the agreement figure pessimistic, which is how this was found.
    # A device that reports no threshold (older firmware) is also not comparable:
    # unknown, rather than guessed at.
    def _comparable(t) -> bool:
        dev_thr = t["dev_threshold"]
        if dev_thr is None:
            return False
        wake_thr = t["wake_threshold"]
        return wake_thr is not None and wake_thr >= dev_thr

    compared   = [t for t in scoring if _comparable(t)]
    incomparable = len(scoring) - len(compared)
    agreed     = [t for t in compared if t["dev_wake_score"] is not None]
    deltas     = sorted(t["dev_wake_delta_ms"] for t in agreed
                        if t["dev_wake_delta_ms"] is not None)
    dev_scores = [t["dev_wake_score"] for t in agreed]
    crossings  = sum(r["dev_crossings"] or 0 for r in counters)
    shadow_out = {
        "turns_scoring":  len(scoring),
        "turns_compared": len(compared),
        # Turns where the device was scoring but the comparison is not valid —
        # the controller used a lower (barge-in) bar, or the device's threshold
        # is unknown. Reported rather than hidden: a large number here means the
        # agreement figure is describing a small slice of reality.
        "not_comparable": incomparable,
        "agreed":         len(agreed),
        "missed":         len(compared) - len(agreed),
        "agreement_pct":  round(100.0 * len(agreed) / len(compared), 1) if compared else None,
        # Signed: negative means the device crossed FIRST, which is the
        # expected direction — it scores the frame it just captured while the
        # controller scores the same frame after a network hop.
        "delta_ms_p50":   pct(deltas, 0.50),
        "delta_ms_p95":   pct(deltas, 0.95),
        "dev_score_avg":  round(sum(dev_scores) / len(dev_scores), 3) if dev_scores else None,
        "dev_score_min":  round(min(dev_scores), 3) if dev_scores else None,
        "crossings":      crossings,
        # Crossings that never matched a turn. This is the false-accept side of
        # the comparison, which per-turn rows structurally cannot show — but it
        # is an ESTIMATE, not a count: the hourly counters and the turn rows are
        # pruned on different schedules (WAKE_COUNTER_RETENTION_DAYS vs
        # TURN_RETENTION rows), so over a long window this drifts. Treat a
        # small number as noise and a large one as worth investigating.
        "unmatched_crossings": max(0, crossings - len(agreed)),
        "frames":         sum(r["dev_frames"] or 0 for r in counters),
        # Nonzero drops mean the device could not keep up, so every figure
        # above is describing a subset of the audio.
        "drops":          sum(r["dev_drops"] or 0 for r in counters),
    }

    return _ok({
        "days":          days_out,
        "wake_models":   models_out,
        "wake_counters": [dict(r) for r in counters],
        "metrics":       metrics,
        "shadow":        shadow_out,
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
    # The satellite needs the same, and had no equivalent: it survives an
    # ordinary disconnect on purpose, so a delete used to leave it in
    # `_servers` holding the old port for a re-added device to inherit
    # silently. Lazy import — em_esphome imports em_api at module level.
    import em_esphome
    await em_esphome.device_deleted(device_id)
    # ...and the device is told to redial, or it never notices it was deleted.
    # Link auth is decided once, at register time, so a connected device keeps
    # running on the socket it already has: it vanishes from the dashboard and
    # carries on serving turns, and only comes back as pending after something
    # else drops the link — a reboot, a controller restart, a WiFi blip. The
    # bounce is what makes delete mean "start over" within seconds instead of
    # whenever. Deliberately AFTER the row is gone: the device redials in 5s
    # and must find an empty registry, or it re-registers into the row we were
    # deleting. em_linkauth ignores the token it still carries (rule 3), so it
    # arrives as pending — except under REQUIRE_DEVICE_TLS, where it is
    # refused and re-provisioning over USB is the intended path.
    await _disconnect_device(device_id)
    await _push_event({"type": "device_deleted", "device_id": device_id})
    return _ok({})


async def _disconnect_device(device_id: str) -> None:
    """
    Close a device's control plane, and any shell session riding on it.

    Only the control plane is closed: the device's own loop cancels its data
    client when control drops (`control.go` Run) and re-establishes both on
    the next dial, so closing `/data` here would only race that. The shell
    plane is separate and demand-opened, so an open session would otherwise
    hang against a device that is about to redial. `_release_shell_ws` is
    called even with no programmatic session registered, because the
    `shell_close` it sends is the only thing that ends an INTERACTIVE
    dashboard session — those deliberately do not set `_shell_ws`.
    """
    live = _devices.get(device_id)
    if live is None:
        return
    await _release_shell_ws(device_id, live)
    try:
        await live.control_ws.close()
    except Exception as e:
        log.warning(f"[api] Could not close control plane for {device_id}: {e}")


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


async def _apply_live_config(device_id: str, live, effective: dict) -> None:
    """
    Push an effective config to a connected device and refresh the
    controller-side mirrors of it.

    Extracted because the per-device and fleet endpoints both did this
    inline, and a mirror added to one but not the other is a bug that reads
    as working — the same shape as the v7 stats-relay miss (PR #23). Take
    the EFFECTIVE config, never a request body: with per-section scoping a
    body is partial by design, and a device must always be sent the whole
    resolved picture.

    One key is held back: a NEW `owwModel` is not sent to a device that scores
    locally until the classifier is actually on it — see _hold_back_oww_model.
    """
    effective, pending_model = _hold_back_oww_model(live, effective)
    await live.send_control({"type": "config", **effective})
    if "owwThreshold" in effective:
        live.oww_threshold = float(effective["owwThreshold"])
    if "owwModel" in effective:
        live.oww_model = effective["owwModel"]
        # Refresh HA's wake-word dropdown (lazy import — em_esphome imports
        # em_api at module level).
        import em_esphome
        em_esphome.update_oww_model(device_id, effective["owwModel"])
    if pending_model:
        # The device is still on its previous wake word, still scoring
        # locally, still answering. Install, then switch.
        asyncio.create_task(_install_then_switch(device_id, pending_model))
    if "owwSpeexNs" in effective:
        live.oww_speex_ns = bool(effective["owwSpeexNs"])
    if "nsAsr" in effective:
        live.ns_asr = bool(effective["nsAsr"])
    if "saveUtterances" in effective:
        live.save_utterances = bool(effective["saveUtterances"])
    if "bargeInEnabled" in effective:
        live.barge_in_enabled = bool(effective["bargeInEnabled"])
    if "bargeInThreshold" in effective:
        live.barge_threshold = float(effective["bargeInThreshold"])
    if "buttonSingleTapEvent" in effective:
        live.button_single_tap_event = bool(effective["buttonSingleTapEvent"])
    if "buttonMultiTapMs" in effective:
        live.button_multi_tap_ms = int(effective["buttonMultiTapMs"])
    if "wakeArbitrationMs" in effective:
        live.wake_arb_ms = int(effective["wakeArbitrationMs"])
    if "owwOnDevice" in effective:
        # Resolved against the CAPABILITY, not taken at face value: "on"
        # against firmware that cannot trigger would stop this controller
        # acting on its own detections while waiting for wakes the device has
        # no code to send, leaving it deaf. em_shadow.effective_mode degrades
        # that to shadow.
        live.oww_on_device = em_shadow.effective_mode(
            effective["owwOnDevice"], live.oww_trigger_capable,
            getattr(live, "oww_model_ready", True),
        )
    if "eqBands" in effective:
        live.eq_bands = effective["eqBands"]
    if "eqLoudness" in effective:
        live.eq_loudness = bool(effective["eqLoudness"])
    # The output chain is consumed HERE, not on the device — it ignores these
    # five keys entirely — so this mirror is the only thing that carries them.
    # Missing it meant a push wrote the database, sent JSON the device threw
    # away, and changed nothing audible until the device happened to
    # reconnect. Exactly the shape this function's docstring warns about, and
    # it cost a whole listening test on 2026-08-19: every setting appeared to
    # do nothing, because every setting WAS doing nothing.
    if "limiterEnabled" in effective:
        live.limiter_enabled = bool(effective["limiterEnabled"])
    if "limiterThreshold" in effective:
        live.limiter_threshold = float(effective["limiterThreshold"])
    if "limiterRelease" in effective:
        live.limiter_release = float(effective["limiterRelease"])
    if "bassGuardEnabled" in effective:
        live.bass_guard_enabled = bool(effective["bassGuardEnabled"])
    if "bassGuardDb" in effective:
        live.bass_guard_db = float(effective["bassGuardDb"])
    live.led_scene = em_scenes.resolve(effective)
    # #263: keep the device's cached listening animation in step when the
    # scene changes live, same push as at registration. Without it the ring
    # lit locally in the OLD scene's colours until the next reconnect.
    if live.led_anim_capable and live.led_scene.get("listening_anim"):
        try:
            await live.send_control(
                {"type": "config",
                 "listeningAnim": live.led_scene["listening_anim"]})
        except Exception:
            pass  # device offline — next connect re-sends it


@auth.require_auth
async def _get_device_config(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/config — effective config, scoping, and fleet view."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    config = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    sections = await loop.run_in_executor(None, db.get_device_config_sections, device_id)
    return _ok({
        "config":            config,
        "config_sections":   sections,
        # Compat view for older readers: no overridden sections == fleet.
        "use_global_config": not sections,
    })


@auth.require_admin
async def _post_device_config(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/config

    Body may include config_sections (list of section ids this device
    overrides), any config fields, and — for older clients —
    use_global_config (bool).

    Scoping is per section (see em_config_sections). Values supplied for a
    section the device does not override are ignored: the device follows the
    fleet there, and storing shadow values would silently resurrect them if
    the section were ever switched back.

    use_global_config is accepted as a compat alias: true == override
    nothing, false == override everything. That is exactly what the boolean
    meant before v8.

    If neither key is present the device's current scoping is left alone and
    only the in-scope values are updated.
    """
    device_id = request.match_info["id"]
    body = await _json_body(request)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    sections_body     = body.pop("config_sections", None)
    use_global        = body.pop("use_global_config", None)
    explicit_replace  = bool(body.pop("replace", False))

    # Compat: map the old boolean onto the section model.
    if sections_body is None and use_global is not None:
        sections_body = [] if use_global else list(sections_mod.SECTION_IDS)

    if sections_body is None:
        new_sections = await loop.run_in_executor(
            None, db.get_device_config_sections, device_id
        )
    else:
        if not isinstance(sections_body, list):
            return _error("bad_request", "config_sections must be a list", 400)
        unknown = [s for s in sections_body if s not in sections_mod.SECTIONS]
        if unknown:
            return _error(
                "bad_request",
                f"Unknown config section(s): {', '.join(map(str, unknown))}. "
                f"Valid: {', '.join(sections_mod.SECTION_IDS)}.",
                400,
            )
        new_sections = sections_mod.normalise(sections_body)

    in_scope = sections_mod.keys_for(new_sections) | sections_mod.STATE_KEYS

    # Same replace-not-merge trap as the global endpoint (see _dropped_keys),
    # but scoped: only keys that REMAIN in scope can be accidentally dropped.
    # Keys leaving scope are being deliberately handed back to the fleet, and
    # flagging those would make every legitimate un-override a 409.
    stored = await loop.run_in_executor(None, db.get_device_config, device_id)
    stored_in_scope = {k: v for k, v in stored.items() if k in in_scope}
    dropped = _dropped_keys(body, stored_in_scope)
    if dropped and not explicit_replace:
        return _error(
            "would_drop_keys",
            f"This body would delete {len(dropped)} existing setting(s): "
            f"{', '.join(dropped)}. Config POSTs replace rather than "
            f"merge — send the full config (read-modify-write), or pass "
            f"replace=true if the deletion is intended.",
            409,
        )

    # Apply scoping first: set_device_config_sections prunes the values of
    # any section no longer overridden, so what follows writes into an
    # already-clean picture.
    if sections_body is not None:
        await loop.run_in_executor(
            None, db.set_device_config_sections, device_id, new_sections
        )
    values = {k: v for k, v in body.items() if k in in_scope}
    if values:
        current = await loop.run_in_executor(None, db.get_device_config, device_id)
        await loop.run_in_executor(
            None, db.set_device_config, device_id, {**current, **values}
        )

    config = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )

    # Push the EFFECTIVE config — with per-section scoping the body is
    # partial by design, so the device must be sent the resolved picture.
    pushed = False
    live = _devices.get(device_id)
    if live is not None:
        await _apply_live_config(device_id, live, config)
        log.info(f"[api] Config pushed to live device: {device_id}")
        pushed = True

    # BT proxy lifecycle follows bleProxyEnabled in the *effective* config —
    # reconcile unconditionally (idempotent): re-scoping a section changes the
    # effective value without the key appearing in the body.
    await em_ble_proxy.reconcile(device_id)

    await _push_event({"type": "device_update", "device_id": device_id,
                       "state": {"config": config,
                                 "config_sections": new_sections,
                                 "use_global_config": not new_sections}})
    return _ok({"device_id": device_id, "config": config,
                "config_sections": new_sections,
                "use_global_config": not new_sections, "pushed": pushed})


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

    if device_id in _updates_in_progress or device_id in _updates_queued:
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

    if device_id in _updates_in_progress or device_id in _updates_queued:
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
        if len(binary) > UPLOAD_MAX_BYTES:
            return _error(
                "too_large",
                f"Binary is {len(binary) / 1024 / 1024:.1f} MB, over the "
                f"{UPLOAD_MAX_BYTES // 1024 // 1024} MB limit",
                413,
            )

        token = str(_uuid.uuid4())
        _pending_uploads[token] = binary
        log.info(f"[api] Binary uploaded: {len(binary):,} bytes token={token[:8]}…")

        async def _expire():
            await asyncio.sleep(600)
            _pending_uploads.pop(token, None)
        asyncio.create_task(_expire())

        return _ok({"upload_token": token, "size": len(binary)})
    except web.HTTPException:
        # aiohttp's own — HTTPRequestEntityTooLarge above all, raised by the
        # transport before this handler sees a byte. Swallowing it into a 500
        # is how a 1 MB transport limit presented as "an internal error
        # occurred" instead of naming a size, and the middleware already
        # re-raises these deliberately.
        raise
    except Exception as e:
        log.error(f"[api] Upload error: {e}")
        return _error("upload_failed", str(e), 500)


# ─── Custom wake-word models ─────────────────────────────────────────────────


@auth.require_auth
async def _get_oww_models(request: web.Request) -> web.Response:
    """
    GET /api/oww_models

    Custom models discovered in the data volume's oww_models/ dir.
    `path` is the value to store in owwModel config.
    """
    return _ok({
        "models": em_oww_models.scan(),
        "dir":    str(em_oww_models.models_dir()),
    })


@auth.require_admin
async def _post_oww_model_upload(request: web.Request) -> web.Response:
    """
    POST /api/oww_models/upload (multipart: field name "model", .onnx file)

    Installs an openWakeWord model into the persisted models dir. The
    file lands atomically (tmp + rename) so a wake-listener reload can
    never see a half-written model.
    """
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "model":
            return _error("invalid_upload", "Expected multipart field 'model'", 400)
        fname = em_oww_models.safe_model_filename(field.filename or "")
        if fname is None:
            return _error("invalid_filename",
                          "Model must be a .onnx file with a simple name "
                          "(letters, digits, _ - . only)", 400)
        data = await field.read()
        if not data:
            return _error("empty_upload", "Uploaded model is empty", 400)
        if len(data) > em_oww_models.MAX_MODEL_BYTES:
            return _error("too_large", "Model exceeds 20 MB limit", 413)

        directory = em_oww_models.models_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, directory / fname)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info(f"[api] Wake model installed: {fname} ({len(data):,} bytes)")
        entry = next((m for m in em_oww_models.scan() if m["file"] == fname), None)
        return _ok({"model": entry}, status=201)
    except Exception as e:
        log.error(f"[api] Model upload error: {e}")
        return _error("upload_failed", str(e), 500)


@auth.require_admin
async def _delete_oww_model(request: web.Request) -> web.Response:
    """
    DELETE /api/oww_models/{file}

    Refuses (409) while any device config or the global default still
    points at the model — deleting under a live listener would fail its
    next reload.
    """
    fname = em_oww_models.safe_model_filename(request.match_info["file"])
    if fname is None:
        return _error("invalid_filename", "Bad model filename", 400)
    path = em_oww_models.models_dir() / fname
    if not path.is_file():
        return _error("not_found", "No such model", 404)

    configs: dict[str, dict] = {"global": db.get_global_device_config()}
    for row in db.get_all_devices():
        configs[row["device_id"]] = db.get_device_config(row["device_id"])
    users = em_oww_models.in_use_by(str(path), configs)
    if users:
        return _error("model_in_use",
                      f"Model is selected by: {', '.join(users)}", 409)

    path.unlink()
    log.info(f"[api] Wake model deleted: {fname}")
    return _ok({"deleted": fname})


# ─── OTA background tasks ─────────────────────────────────────────────────────


def _extract_binary_version(binary: bytes) -> str | None:
    """
    Scan a compiled Go binary for its embedded EchoMuse version string.

    Two schemes, because compile.sh changed and this did not:

      v2.12.0-63-g99628d3   `git describe --tags --match 'v*'` — CURRENT
      20260614-1152-dev     date+suffix — used when the tree is DIRTY, and
                            the only scheme this function knew until now

    Matching only the second is why a clean-tree local build extracted
    nothing, fell back to a `local-<timestamp>` label, and then never matched
    the version the device reported on reboot — so a completely successful
    deploy was announced as "auto-rolled back". A message that says the
    opposite of what happened is worse than a plain failure, because the
    natural response is to distrust the feature.

    BARE `vX.Y.Z` IS DELIBERATELY NOT MATCHED, and that is the whole
    subtlety. A Go binary embeds its dependencies' module versions, so the
    shape is far from unique — measured on the v2.12.0-63-g99628d3 build:
    102 occurrences, 9 distinct, including v0.41.0 (x/sys), v1.5.3
    (gorilla/websocket), v1.0.3 (GoTinyAlsa) and v1.1.41 (miekg/dns). Our own
    version happened to appear first in file order on that build, which is
    luck and not a property to rely on: a dependency string landing earlier
    would silently extract the wrong version and label the deploy with it.

    The `-N-gSHA` suffix makes it unique — exactly one match on the same
    binary — so only the suffixed form is accepted. A build made exactly ON a
    tag has no suffix and extracts nothing, which is correct: that binary
    belongs on the release path, and returning None gets an honest
    `local-<timestamp>` label rather than a confident wrong one.

    Returns None when nothing matches; the caller labels it
    `local-YYYYMMDD-HHMM` and the reconnect check compares against the
    version the device had BEFORE the push rather than against this.
    """
    import re as _re
    for pattern in (
        # git describe, suffixed form only — see above
        rb'v\d+\.\d+\.\d+-\d+-g[0-9a-f]{7,40}',
        # dirty-tree builds, and every binary predating the compile.sh change
        rb'20\d{6}-\d{4}-[a-z][a-z0-9]*',
    ):
        match = _re.search(pattern, binary)
        if match:
            return match.group(0).decode("ascii")
    return None


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
    _update_errors.pop(device_id, None)  # fresh attempt clears the last failure

    # Wait for any other device's update to finish before touching this one.
    # Queued state is visible while waiting (see `_updates_queued`), and the
    # binary is fetched INSIDE the lock so a queued device is holding nothing
    # but its place in line.
    _updates_queued.add(device_id)
    try:
        await _ota_lock.acquire()
    finally:
        _updates_queued.discard(device_id)

    _updates_in_progress.add(device_id)
    try:
        # Bounded, because serialising turned a device-local stall into a
        # fleet-wide one. Every `recv` in the transfer is wait_for-bounded but
        # `await ws.send(line)` in the base64 loop is not: a device that stops
        # reading applies backpressure and can hang there. Before the lock that
        # stalled one device; now it would hold the queue and NOTHING could be
        # updated until the controller restarted.
        #
        # Generous on purpose — a real update is a ~10MB transfer plus a reboot
        # and a 90s reconnect watch, so this is a deadlock cap, not a
        # performance budget. A device that trips it has failed anyway.
        await asyncio.wait_for(
            _run_update_locked(device_id, release, binary_override),
            timeout=OTA_MAX_HOLD_S,
        )
    except asyncio.TimeoutError:
        await _update_failed(
            device_id,
            f"Update abandoned after {OTA_MAX_HOLD_S:.0f}s so the queue "
            f"could continue — the device may still be mid-update",
        )
    finally:
        _updates_in_progress.discard(device_id)
        # Release LAST, so the next queued device does not start its transfer
        # while this one is still being cleaned up.
        _ota_lock.release()


async def _run_update_locked(device_id: str, release: dict,
                             binary_override: bytes | None = None) -> None:
    """
    The update itself, run with `_ota_lock` already held. Split from
    `_run_update` so the whole of it can sit under one timeout — the queue is
    only as safe as its slowest member.
    """
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
            binary = await _fetch_binary(release["url"], release.get("version", ""))
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
        # Payload drift is not limited to the start script — the debloat
        # halves had no update path at all until 2026-07-30.
        await _sync_debloat(live, device_id)

        inactive_slot = "server_b" if active_slot == "server_a" else "server_a"

        # Free space, checked BEFORE writing anything. The transfer needs room
        # for the new binary alongside its .part, and running /data out of
        # space mid-write is a bad way to find out. Read with parse_free_mb,
        # never an awk field index — busybox wraps a long filesystem name onto
        # its own line, so $4 is the percentage on these devices.
        need_mb  = (len(binary) * 2) // 1048576 + 8   # binary + .part + slack
        free_out = await _shell_run(
            live, 'echo "FREE $(busybox df -m /data | busybox tail -1)"')
        free_mb = None
        for line in (free_out or "").splitlines():
            if line.startswith("FREE"):
                free_mb = em_oww_assets.parse_free_mb(line[5:])
                break
        if free_mb is not None and free_mb < need_mb:
            await _update_failed(
                device_id,
                f"Not enough space on /data: {free_mb}MB free, needs ~{need_mb}MB")
            return
        if free_mb is None:
            # Unknown reads as "carry on" — a df we cannot parse is not
            # evidence of a full disk, and refusing on it would block updates
            # on any device whose df we have not seen.
            log.warning(f"[api] Could not read free space on {device_id} "
                        f"from {free_out!r} — proceeding with update")

        await _push_log_event(device_id, "info", "controller",
                              f"Deploying to slot {inactive_slot} (active: {active_slot})")

        # Stream binary to inactive slot. Verified by md5 before it is renamed
        # into place, so a corrupt transfer leaves the slot as it was and never
        # reaches the symlink flip below (#76).
        ok = await _stream_binary_to_slot(live, binary, inactive_slot)
        if not ok:
            # Name the stage. "failed or did not verify" covered everything
            # from a shell that never opened to a corrupt payload, and #121
            # was the former reported in the language of the latter.
            await _update_failed(device_id,
                                 f"Binary transfer to {inactive_slot} failed: "
                                 f"{ok} — {inactive_slot} left untouched")
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
                _supervisor_log_wanted.add(device_id)
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
                _supervisor_log_wanted.add(device_id)
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


async def _stream_binary_to_slot(live, binary: bytes, slot: str) -> "TransferResult":
    """
    Transfer a firmware binary to /data/local/bin/{slot}.

    require_verify=True: firmware is the one payload where an unverifiable
    transfer must fail rather than proceed. A corrupt binary and a genuinely
    broken one produce the same observable — three fast exits and a rollback —
    so shipping one to the slot we are about to boot costs a reboot and a
    rollback to learn nothing (#76).
    """
    return await _stream_file_to_device(live, binary, f"/data/local/bin/{slot}",
                                        require_verify=True)


class TransferResult:
    """
    The outcome of a device file transfer, carrying the STAGE it stopped at.

    Truthy on success, so every existing `if not await _stream_file_to_device(
    ...)` call site keeps working unchanged.

    The stage exists because one message covered five different outcomes, and
    the two that matter most are at opposite ends: "the bytes arrived corrupt"
    and "we never opened a shell, so no byte was ever sent". #121 reported the
    second and read as the first — three Dots failing with `failed or did not
    verify`, fifteen seconds after starting, which is far too fast to have
    attempted a 10MB payload. Nothing short of the controller's own stdout
    could tell them apart, and a user cannot be asked for that mid-update.
    """

    __slots__ = ("ok", "stage", "detail")

    def __init__(self, ok: bool, stage: str = "ok", detail: str = ""):
        self.ok     = ok
        self.stage  = stage
        self.detail = detail

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        return self.detail or self.stage


# Stage detail text. Phrased for someone reading a device log who is deciding
# what to try next, so each one says whether any data left the controller —
# that is the difference between "retry, the link was bad" and "the payload or
# the device is wrong".
_TRANSFER_STAGES = {
    "shell":   "could not open a shell session on the device — no data was sent",
    "decoder": "no base64 decoder found on the device — no data was sent",
    "md5tool": "device has no md5 tool, refusing to send unverified",
    "send":    "timed out part-way through sending",
    "verify":  "sent, but timed out waiting for md5 verification",
    "corrupt": "arrived corrupt — md5 did not match what was sent",
    "error":   "transfer error",
}


def _transfer_failed(stage: str, extra: str = "") -> TransferResult:
    detail = _TRANSFER_STAGES.get(stage, stage)
    if extra:
        detail = f"{detail} ({extra})"
    return TransferResult(False, stage, detail)


async def _stream_file_to_device(live, data: bytes, dest: str,
                                 mode: str = "755",
                                 require_verify: bool = False) -> TransferResult:
    """
    Transfer a file to `dest` on the device via shell heredoc (default mode 755).

    Detects available base64 decoder (busybox base64, python3, python) before
    transferring, since 'base64' is not always in PATH on Android/FireOS.
    Uses a heredoc so no intermediate .b64 file is needed.
    The heredoc delimiter contains '_' which is not in the base64 alphabet.

    **md5 decides success, not the shell's exit status.** Bytes land in
    `{dest}.part` and are renamed only once their md5 matches what we sent, so
    a bad transfer leaves whatever was at `dest` untouched. `TRANSFER_OK` alone
    only ever proved that the decode pipeline and chmod exited 0 — not that the
    bytes arrived intact (#76). The verification rides the SAME shell session as
    the transfer, so it costs a round trip on an open socket, not a new session.

    `require_verify` decides what happens when the device has no md5 tool at
    all. Callers default to False, which warns and accepts — the same behaviour
    they had before this existed, and the base64 detection below already treats
    a busybox-less device as a contemplated state. Firmware passes True.
    """
    import base64 as _b64

    device_id     = live.device_id
    DELIM         = "__END_B64_42__"
    DETECT_MARKER = "__DETECT_DONE__"

    try:
        try:
            ws = await _get_device_shell_ws(live)
        except Exception as e:
            log.error(f"[api] Could not open a shell to {device_id} for "
                      f"{dest}: {e}")
            return _transfer_failed("shell", str(e))

        # `dest` is NOT deleted here, and must not be. This used to open with
        # `rm -f {dest}` to clear a previous attempt, which for firmware means
        # deleting /data/local/bin/server_<inactive> — THE ROLLBACK SLOT —
        # before a single byte of the replacement had been sent. Every failed
        # OTA therefore left the device with a good active slot and an empty
        # partner, so a later crash-loop would flip the symlink onto nothing.
        # It also contradicted the message the user was shown, which promised
        # the slot was left untouched (#121). Nothing needs the delete: the
        # heredoc writes with `>`, which truncates, and a verified transfer
        # arrives by `mv` over whatever was there.

        # ── Detect available base64 decoder ──────────────────────────────────
        # Try busybox first (Magisk provides it), then python3/python.
        # We run a round-trip sanity test so we know the decode flag works.
        # The md5 tool is detected in the SAME round trip, not a second one.
        # busybox first for the same reason as the decoder (Magisk provides
        # it); bare md5sum as a fallback since some SKUs have it in PATH.
        await ws.send(
            "if echo dGVzdA== | busybox base64 -d >/dev/null 2>&1; then echo DECODER:busybox; "
            "elif python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python3; "
            "elif python  -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python; "
            "else echo DECODER:none; fi; "
            "if echo x | busybox md5sum >/dev/null 2>&1; then echo MD5:busybox; "
            "elif echo x | md5sum >/dev/null 2>&1; then echo MD5:plain; "
            f"else echo MD5:none; fi; echo {DETECT_MARKER}\n"
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
            # Two very different things reach here. DETECT_MARKER present means
            # the device answered and genuinely has no decoder — a property of
            # that device, which retrying will not change. Absent means the
            # round trip produced nothing in 15s, i.e. the shell plane is not
            # carrying output, which is a link problem and IS worth retrying.
            # Reporting both as "no base64 decoder" sent #121 looking at the
            # wrong half.
            if DETECT_MARKER not in detect_buf:
                log.error(f"[api] Shell produced no output in 15s while probing "
                          f"{device_id} for a decoder — link problem, not a "
                          f"missing tool. Output so far: {detect_buf!r}")
                return _transfer_failed(
                    "shell", "device shell produced no output within 15s")
            log.error(f"[api] No base64 decoder found on device. "
                      f"Detection output: {detect_buf!r}")
            return _transfer_failed("decoder")

        log.info(f"[api] Decoder: {decode_cmd.split()[0]} {decode_cmd.split()[1]}")

        if "MD5:busybox" in detect_buf:
            md5_cmd = "busybox md5sum"
        elif "MD5:plain" in detect_buf:
            md5_cmd = "md5sum"
        else:
            md5_cmd = None
            if require_verify:
                log.error(f"[api] No md5 tool on device — refusing to transfer "
                          f"{dest} unverified. Detection output: {detect_buf!r}")
                return _transfer_failed("md5tool")
            log.warning(f"[api] No md5 tool on device — {dest} will be "
                        f"transferred WITHOUT verification")

        # ── Heredoc transfer ─────────────────────────────────────────────────
        lines = _b64.encodebytes(data).decode("ascii").splitlines(keepends=True)
        log.info(f"[api] Transferring {len(data):,} bytes to {dest} "
                 f"({len(lines)} base64 lines via heredoc)")

        # Bytes land in .part; only a matching md5 promotes them to dest. With
        # no md5 tool there is nothing to promote against, so write straight to
        # dest and keep the old behaviour (require_verify already refused above
        # for the payloads where that is not acceptable).
        landing = f"{dest}.part" if md5_cmd else dest

        # Single shell command: decode heredoc → landing, set permissions,
        # confirm. chmod here rather than after the rename so the mode travels
        # with the file and dest is never briefly present with the wrong one.
        await ws.send(
            f"{decode_cmd} << '{DELIM}' > {landing} && "
            f"chmod {mode} {landing} && "
            f"echo TRANSFER_OK\n"
        )

        # Stream base64 data — each line already ends with \n from encodebytes
        for line in lines:
            await ws.send(line)

        # Close heredoc; shell now executes the decode pipeline
        await ws.send(f"{DELIM}\n")
        log.info(f"[api] Heredoc sent — waiting for TRANSFER_OK")

        # Wait for confirmation (decode of ~13 MB on ARM takes a few seconds)
        deadline    = time.monotonic() + 120
        transferred = False
        while time.monotonic() < deadline:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                if "TRANSFER_OK" in text:
                    transferred = True
                    break
                if text.strip():
                    log.debug(f"[api] Shell output during transfer: {text!r}")
            except asyncio.TimeoutError:
                continue

        if not transferred:
            log.error(f"[api] Transfer to {dest} timed out waiting for TRANSFER_OK")
            return _transfer_failed("send")

        if md5_cmd is None:
            log.info(f"[api] Transfer to {dest} confirmed (unverified)")
            return TransferResult(True, "unverified")

        # ── Verify, then promote ─────────────────────────────────────────────
        # Same shell session, so this is a round trip on an open socket rather
        # than a new session. A mismatch removes the .part and leaves dest as
        # it was — for firmware that means the rollback slot keeps its previous
        # binary instead of being replaced by a broken one.
        # No `cut`: md5sum prints "<hash>  <path>", and the busybox-less branch
        # is exactly where `busybox cut` would not be there either. A case glob
        # needs no external tool at all.
        want = hashlib.md5(data).hexdigest()
        await ws.send(
            f'GOT=$({md5_cmd} {landing} 2>/dev/null); '
            f'case "$GOT" in {want}*) mv {landing} {dest} && echo VERIFY_OK ;; '
            f'*) rm -f {landing}; echo "VERIFY_BAD:$GOT" ;; esac\n'
        )

        verify_buf = ""
        verify_dl  = time.monotonic() + 120
        while time.monotonic() < verify_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                verify_buf += text
                if "VERIFY_OK" in verify_buf:
                    log.info(f"[api] Transfer to {dest} confirmed "
                             f"({len(data):,} bytes, md5 {want})")
                    return TransferResult(True)
                if "VERIFY_BAD" in verify_buf:
                    got = verify_buf.split("VERIFY_BAD:")[-1].strip().split()
                    log.error(f"[api] Transfer to {dest} CORRUPT — md5 {want} "
                              f"expected, device reported {got[0] if got else '(none)'}. "
                              f"{dest} left untouched.")
                    return _transfer_failed("corrupt",
                                            f"device reported {got[0] if got else '(none)'}")
            except asyncio.TimeoutError:
                continue

        log.error(f"[api] Transfer to {dest} timed out waiting for md5 verification "
                  f"— {dest} left untouched")
        return _transfer_failed("verify")

    except Exception as e:
        log.error(f"[api] File transfer to {dest} failed: {e}")
        return _transfer_failed("error", str(e))
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
    res = await _stream_file_to_device(live, script, tmp)
    if not res:
        await _push_log_event(device_id, "warn", "controller",
                              f"start_server.sh sync failed: {res} — continuing OTA")
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


# Magisk service.d location of the boot-time debloat script. Installed by the
# provisioning wizard; synced from here afterwards.
DEBLOAT_SCRIPT_PATH = "/sbin/.core/img/.core/service.d/echomuse-debloat.sh"


def _debloat_packages() -> list[str]:
    """The pm-hide list from the canonical payload, comments stripped."""
    try:
        raw = (PAYLOADS_DIR / "debloat_packages.txt").read_text()
    except OSError as e:
        log.error(f"[api] debloat_packages.txt unreadable: {e}")
        return []
    return [ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


async def _sync_debloat(live, device_id: str) -> None:
    """
    Heal debloat drift on a device that is already in the field.

    The debloat has two halves and neither had an update path. The boot script
    was installed once by the provisioning wizard, and the pm-hide list was
    applied once at the same time — so a device provisioned before a list grew
    never receives the addition. Found 2026-07-30 when round 2 added
    com.amazon.whad and every existing device needed a manual push.

    Both halves are reconciled here, idempotently, so this is safe to run as
    often as you like:

      * the script by md5 against the canonical payload, replaced by rename so
        the running shell keeps reading the old inode (same reasoning as
        _sync_start_script — it takes effect on the next device reboot);
      * the hide list by asking the device which of those packages are still
        VISIBLE and hiding only those. One `pm list packages` costs about a
        second; `pm hide` is slow enough per call that hiding all 32
        unconditionally would add half a minute for nothing.

    Best-effort throughout: a failure logs and returns, and never blocks the
    firmware update this normally rides along with.

    Note what hiding does NOT do: com.amazon.whad is PERSISTENT, so hiding it
    leaves the running instance alive until the next reboot (am force-stop is a
    no-op on it — measured). That matches the rest of the debloat's semantics
    rather than being a shortcoming of this function, and it is why the log
    line says "next reboot".
    """
    # ── half 1: the boot script ──────────────────────────────────────────────
    try:
        script = (PAYLOADS_DIR / "echomuse-debloat.sh").read_bytes()
    except OSError as e:
        log.error(f"[api] echomuse-debloat.sh payload unreadable — skipping sync: {e}")
        script = None

    if script is not None:
        want = hashlib.md5(script).hexdigest()
        out = await _shell_run(live, f"busybox md5sum {DEBLOAT_SCRIPT_PATH} 2>/dev/null")
        if want not in out:
            # An empty result also lands here — a device provisioned before the
            # script existed has no file at all, and installing it is right.
            await asyncio.sleep(1.0)
            await _push_log_event(device_id, "info", "controller",
                                  "debloat script out of date — syncing canonical version")
            tmp = DEBLOAT_SCRIPT_PATH + ".new"
            pushed = await _stream_file_to_device(live, script, tmp)
            if pushed:
                await asyncio.sleep(1.0)
                res = await _shell_run(live,
                    f'NEW=$(busybox md5sum {tmp} | busybox cut -d" " -f1); '
                    f'if [ "$NEW" = "{want}" ]; then '
                    f'mv {tmp} {DEBLOAT_SCRIPT_PATH} && chmod 755 {DEBLOAT_SCRIPT_PATH} '
                    f'&& echo DEBLOAT_SYNCED; '
                    f'else rm -f {tmp}; echo DEBLOAT_MD5_MISMATCH:$NEW; fi')
                await _push_log_event(
                    device_id,
                    "info" if "DEBLOAT_SYNCED" in res else "warn", "controller",
                    "debloat script synced — daemon stops take effect on next device reboot"
                    if "DEBLOAT_SYNCED" in res else
                    f"debloat script sync failed ({res.strip() or 'no output'})")
            else:
                await _push_log_event(device_id, "warn", "controller",
                                      f"debloat script sync failed: {pushed}")
            await asyncio.sleep(1.0)

    # ── half 2: the pm-hide list ─────────────────────────────────────────────
    pkgs = _debloat_packages()
    if not pkgs:
        return
    # Built as a file rather than a long inline list: 30-odd package names is
    # over a kilobyte of command line, and a shell command that is *usually*
    # short enough is the kind of thing that breaks on the day someone adds the
    # thirty-third package.
    listing = ("\n".join(pkgs) + "\n").encode()
    remote_list = "/data/local/tmp/em_debloat_pkgs.txt"
    pushed = await _stream_file_to_device(live, listing, remote_list, mode="644")
    if not pushed:
        await _push_log_event(device_id, "warn", "controller",
                              f"debloat hide-list sync failed: {pushed}")
        return
    await asyncio.sleep(1.0)

    # Two details here were learned by getting them wrong (2026-07-30).
    #
    # The list is iterated with `for` over a variable, NOT `while read < file`:
    # `pm` is a wrapper that starts app_process, and a command inside a read
    # loop can consume the loop's own stdin, silently ending it early. `pm hide`
    # also gets </dev/null for the same reason.
    #
    # And the end state is VERIFIED by re-listing rather than trusting the
    # return code of each hide, so a partial failure cannot read as success.
    #
    # The match is `grep -qx`, ANCHORED to the whole line, and that is the
    # important part. A shell `case "$VIS" in *"package:$p"*)` looks equivalent
    # and is not: `package:com.amazon.tcomm` is also a substring of
    # `package:com.amazon.tcomm.client`, so three packages appeared un-hidden
    # because a *different*, longer-named package was visible. That produced a
    # confident warning about a FireOS limitation that did not exist — `pm hide`
    # had worked, and dumpsys said hidden=true throughout.
    res = await _shell_run(live,
        f'PKGS=$(cat {remote_list}); VIS=$(pm list packages); N=0; '
        f'for p in $PKGS; do '
        f'if echo "$VIS" | busybox grep -qx "package:$p"; then '
        f'pm hide "$p" >/dev/null 2>&1 </dev/null && N=$((N+1)); fi; '
        f'done; '
        f'VIS2=$(pm list packages); LEFT=""; '
        f'for p in $PKGS; do '
        f'if echo "$VIS2" | busybox grep -qx "package:$p"; then LEFT="$LEFT $p"; fi; '
        f'done; '
        f'rm -f {remote_list}; '
        f'echo HIDDEN_APPLIED:$N; echo STILL_VISIBLE:$LEFT', timeout=180.0)

    applied = 0
    for tok in res.split():
        if tok.startswith("HIDDEN_APPLIED:"):
            try:
                applied = int(tok.split(":", 1)[1])
            except ValueError:
                pass
    still = ""
    for line in res.splitlines():
        if line.strip().startswith("STILL_VISIBLE:"):
            still = line.split(":", 1)[1].strip()
    if applied:
        await _push_log_event(device_id, "info", "controller",
                              f"debloat: hid {applied} newly-listed package(s) — "
                              f"persistent ones stop at the next device reboot")
    if still:
        await _push_log_event(device_id, "warn", "controller",
                              f"debloat: {len(still.split())} package(s) could not be "
                              f"hidden and are still active: {still}")
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
        if device_id in _updates_in_progress or device_id in _updates_queued:
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

    binary = await _fetch_binary(release["url"], release.get("version", ""))
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

# Per-device log lines in a support bundle, after thinning. Deep enough that
# a startup line survives a day of chatter, small enough that six devices do
# not bury the controller's own log.
DEVICE_LOG_LINES = 120


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


@auth.require_admin
async def _post_debloat(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/debloat

    Re-apply the debloat payloads to a live device: sync the boot script and
    hide any newly-listed packages.

    This exists because the OTA-time sync cannot reach every device. A device
    already running the latest firmware will not be updated again, so it would
    never receive a payload change — which is exactly the situation the first
    device hit (Lounge was current when round 2 landed). Idempotent, so
    pressing it twice costs a `pm list packages` and nothing else.
    """
    device_id = request.match_info["id"]
    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", f"Device not connected: {device_id}", 409)

    # No explicit shell release here: _shell_run and _stream_file_to_device each
    # acquire and release the session in their own finally, which is why
    # _sync_start_script does not either. Releasing it from out here could close
    # a session a concurrent caller had opened.
    task = asyncio.create_task(_sync_debloat(live, device_id))
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
                                  f"Secure link: credential transfer failed: {ok}")
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

    # Lazy resolution — em_controller imports em_api at module level, and
    # importing by name would load a second, uninitialised copy (#306).
    _ctrl = _running_controller_module()

    return _ok({
        "controller_version": CONTROLLER_VERSION,
        # True when running as a Home Assistant add-on behind Supervisor's
        # ingress proxy. Presentation only — the dashboard is the same
        # dashboard either way, with the same features, and nothing should
        # be gated on this. It exists so the SPA can stop drawing chrome
        # Home Assistant already draws (its own panel header and title) and
        # can avoid offering a theme toggle that fights HA's theme.
        "ha_ingress": INGRESS_ONLY,
        # Peak asyncio event-loop stall since start (ms). Non-trivial values
        # mean the controller itself delayed speaker frames and LED updates.
        "loop_lag_peak_ms": round(_ctrl._loop_lag_peak_ms, 1),
        "connected":      len(_devices),
        "total_devices":  len(all_rows),
        "pending":        sum(1 for r in all_rows if not r["approved"]),
        "approval_mode":  db.get_config("device_approval", "strict"),
        # Whether the BACKGROUND poll runs (#159). With it off, "No release
        # info" and a GitHub outage are indistinguishable from the Updates
        # tab, and the tab is where someone goes to find out — so the reason
        # for the blank has to be visible there or it reads as a fault and
        # sends them debugging their network.
        #
        # Deliberately not "are updates available": "Check now" still works
        # when this is False, because a button press is a request rather than
        # background traffic. The flag says the poll is off, nothing more.
        "update_checks_enabled": _update_check_interval() > 0,
        "latest_release": release["version"] if release else None,
        # Controller update, surfaced alongside the firmware one so the header
        # can badge it without a second round trip. Read-only by design: the
        # controller runs as a container the user owns, and updating it is a
        # `docker compose pull` they perform — there is deliberately no action
        # here, only the information needed to decide to take it.
        "controller_update": _controller_cache.get("version")
            if _controller_cache.get("available") else None,
        "updates_available": sum(
            1 for r in all_rows
            if r["firmware_ver"] and release
            and r["firmware_ver"] != release["version"]
        ),
    })


def _running_controller_module():
    """
    The module object the RUNNING controller executes as (#306).

    em_start.py execvp's em_controller.py, so in production the running
    code is __main__ — and `import em_controller` would load a SECOND,
    never-initialised copy whose module state is all defaults. That is
    why /api/system/status reported loop_lag_peak_ms: 0.0 next to a log
    line saying the loop had stalled 881ms: the reader was reading a
    fresh copy, not the live module. Resolve the running object instead
    of importing by name. The lazy-import pattern itself stays — the
    circular dependency is real — only the resolution changes.

    Placement note (#309 review): this deliberately sits BELOW its two
    callers' section divider rather than directly above a decorated
    function — between `@auth.require_auth` and `_get_system_status` it
    stole the decorator, leaving the status endpoint unauthenticated.
    """
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "_loop_lag_peak_ms"):
        return main
    return sys.modules.get("em_controller")


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


def _dropped_keys(incoming: dict, stored: dict) -> list[str]:
    """
    Keys present in the stored config that the incoming body would delete.

    Config POSTs REPLACE the stored dict — they do not merge. That is fine
    for the dashboard, which always submits the complete config, and a trap
    for anything that submits a partial one: on 2026-07-20 a POST carrying a
    single key silently reset all 26 fleet settings to defaults, taking the
    wake model from hey_mycroft back to hey_jarvis and dropping owwThreshold
    0.5 -> 0.3, which surfaced as devices false-waking on ordinary
    conversation. Callers that genuinely intend a destructive write pass
    replace=true; everything else is refused before anything is persisted.
    """
    return sorted(set(stored) - set(incoming))


@auth.require_admin
async def _post_global_config(request: web.Request) -> web.Response:
    """
    POST /api/global/config

    Persists new fleet-wide device defaults, then pushes each connected
    device its freshly resolved EFFECTIVE config.

    Since v8 that means every connected device, not just fully-inheriting
    ones: a device overriding only Ring still follows the fleet for
    Microphones, Wake word and the rest, so it has to receive this change.
    Each device gets its own resolved config rather than the raw body —
    sending the body would blow away exactly the overrides being respected.

    The body REPLACES the stored config. A body that would drop existing
    keys is refused with 409 unless it sets replace=true — see
    _dropped_keys.
    """
    config = await _json_body(request)
    loop = asyncio.get_event_loop()

    explicit_replace = bool(config.pop("replace", False))
    # Raw (defaults NOT underlaid): see get_global_device_config_raw — a
    # newly-added default must not look like a key this body is deleting.
    stored = await loop.run_in_executor(None, db.get_global_device_config_raw)
    dropped = _dropped_keys(config, stored)
    if dropped and not explicit_replace:
        return _error(
            "would_drop_keys",
            f"This body would delete {len(dropped)} existing setting(s): "
            f"{', '.join(dropped)}. Config POSTs replace rather than merge — "
            f"send the full config (read-modify-write), or pass replace=true "
            f"if the deletion is intended.",
            409,
        )

    await loop.run_in_executor(None, db.set_global_device_config, config)

    # Push every connected device its own resolved effective config.
    pushed = []
    for device_id, live in list(_devices.items()):
        effective = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        await _apply_live_config(device_id, live, effective)
        pushed.append(device_id)

    if pushed:
        log.info(f"[api] Global config pushed to {len(pushed)} device(s): {pushed}")

    # Reconcile BT proxies for every approved device — offline ones included
    # (proxy mDNS/port lifecycle is independent of the device connection,
    # unlike the config push above). No longer filtered on inheritance: a
    # device overriding some other section still tracks the fleet's
    # bleProxyEnabled, and reconcile is idempotent either way.
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    for row in all_rows:
        if row["approved"]:
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

def _update_check_interval() -> int:
    """
    The parsed update_check_interval; <= 0 means DISABLED (#159).

    db.get_config returns a STRING, so a stored "0" is truthy and survived
    the old `or 3600` fallback into asyncio.sleep(0) — turning the obvious
    way to stop the controller contacting GitHub (#158 documents this poll
    as its only outbound connection) into a busy loop against api.github.com
    until it rate-limits. The worst available outcome for exactly the reader
    who set it carefully. A non-numeric value used to raise out of the poll
    loop and kill the task outright; it now falls back to the default with
    a line in the log.
    """
    raw = (db.get_config("update_check_interval", "3600") or "3600").strip()
    try:
        return int(raw)
    except ValueError:
        log.warning(f"[api] update_check_interval {raw!r} is not a number — using 3600")
        return 3600

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
        _release_cache = {
            "version":      version,
            "url":          url,
            "notes":        db.get_config("latest_notes", "") or "",
            "release_url":  db.get_config("latest_release_url", "") or "",
            "published_at": db.get_config("latest_published_at", "") or "",
        }
        _release_cache_ts = time.monotonic()

        # If the DB cache has aged out, AWAIT the refresh rather than firing it
        # into the background and returning the stale value.
        #
        # Returning stale here is why "there's an update" showed up
        # inconsistently and why an OTA could push the previous release: the
        # caller — dashboard or update endpoint — got the old version and the
        # fresh one only landed in the cache afterwards, for whoever asked
        # next. It cost one wrong OTA (v2.9.9 pushed while v2.9.10 was
        # current, 2026-07-30).
        #
        # The cost is a single GitHub round trip, bounded by the 10s timeout in
        # _fetch_latest_release, and only on the first request after the
        # interval lapses — release_poll_loop normally refreshes ahead of any
        # caller. A failed refresh falls through to the stale cache, which is
        # better than no answer.
        # 0 (any non-positive value) means updates are DISABLED (#159):
        # serve the cache whatever its age and make no outbound call. The
        # poll loop reads this the same way, so the knob does what the
        # privacy section implies it does.
        interval = _update_check_interval()
        if (interval > 0
                and (not last_check
                     or (time.time() - float(last_check)) > interval)):
            fresh = await _fetch_latest_release()
            if fresh:
                return fresh

        return _release_cache

    # No cache at all. The disabled check has to be repeated HERE and not
    # only on the branch above (#159): that one guards a controller which
    # has successfully polled at least once, and this is the branch a fresh
    # install takes — which is exactly the install belonging to someone who
    # set the interval to 0 before the first poll ever ran. Gating only the
    # refresh left them an outbound call on every dashboard visit that reads
    # releases, forever, because a disabled poll loop never populates the
    # cache that would have stopped it.
    #
    # None is the honest answer, and callers already handle it — this
    # function returns None on any fetch failure. The dashboard shows no
    # release information, which is what "I turned update checks off" should
    # look like. "Check now" is unaffected: it calls _fetch_latest_release
    # directly, and a button press is a deliberate request rather than
    # background traffic.
    if _update_check_interval() <= 0:
        return None

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
        # Initialised explicitly: it is only assigned inside the loop, and
        # while the `binary is None` return below happens to cover that today,
        # relying on one guard to protect another variable is how a later edit
        # introduces a NameError on a path nobody runs in testing.
        release: dict = {}
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
            release = data
            break

        if binary is None:
            log.warning("[api] No device firmware release with a 'server' asset found")
            return None

        download_url = binary["browser_download_url"]

        # Release notes, so the dashboard can show WHAT an update changes
        # rather than only that one exists. Deciding whether to push firmware
        # to a device you rely on, from a version number alone, is not a
        # decision — it is a guess. The body comes from the annotated tag (see
        # .github/workflows/release.yml), which is why tags are annotated.
        notes = (release.get("body") or "").strip()

        previous_tag = db.get_config("latest_version")

        # Persist to DB
        db.set_config("latest_version",    tag)
        db.set_config("latest_binary_url", download_url)
        db.set_config("latest_notes",      notes)
        db.set_config("latest_release_url", release.get("html_url") or "")
        db.set_config("latest_published_at", release.get("published_at") or "")
        db.set_config("last_update_check", str(time.time()))

        # Update in-memory cache
        _release_cache    = {
            "version":      tag,
            "url":          download_url,
            "notes":        notes,
            "release_url":  release.get("html_url") or "",
            "published_at": release.get("published_at") or "",
        }
        _release_cache_ts = time.monotonic()

        log.info(f"[api] Latest release: {tag}")
        if tag != previous_tag:
            # Tell any open dashboard, so a tab that is already showing the
            # Updates panel does not sit on the old version until someone
            # reloads or presses Check now.
            log.info(f"[api] Release changed {previous_tag or '(none)'} -> {tag}")
            await _push_event({
                "type":         "release_update",
                "version":      tag,
                "notes":        notes,
                "release_url":  release.get("html_url") or "",
                "published_at": release.get("published_at") or "",
            })
        return _release_cache

    except Exception as e:
        log.error(f"[api] GitHub release fetch failed: {e}")
        return None


async def _fetch_controller_release(force: bool = False) -> Optional[dict]:
    """
    Find the newest controller-v* tag and its annotation.

    Two requests, not one per tag: matching-refs returns every controller-v*
    ref, the newest is picked by parsed version (NOT by list order — the API
    sorts refs lexically, which puts v2.9.0 after v2.10.0), and only that one
    tag's object is dereferenced for its message.

    A lightweight tag has no annotation and no message; that is a degraded
    release, not a broken one, so it still reports the version with empty
    notes.
    """
    global _controller_cache, _controller_cache_ts

    if (not force and _controller_cache
            and (time.monotonic() - _controller_cache_ts) < RELEASE_CACHE_TTL):
        return _controller_cache

    repo = db.get_config("github_repo", "wilbowes/EchoMuse")
    headers = {"Accept": "application/vnd.github+json"}
    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                GITHUB_TAGS_URL.format(repo=repo), headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[api] Controller tag list returned {resp.status}")
                    return _controller_cache or None
                refs = await resp.json()

            newest = None
            for ref in refs or []:
                tag = (ref.get("ref") or "").removeprefix("refs/tags/")
                parsed = _parse_version(tag)
                if parsed is None:
                    continue
                if newest is None or parsed > newest[0]:
                    newest = (parsed, tag, ref.get("object") or {})

            if newest is None:
                log.info("[api] No controller-v* tags published yet")
                return None

            _, tag, obj = newest
            notes = ""
            published_at = ""
            if obj.get("type") == "tag" and obj.get("sha"):
                async with session.get(
                    GITHUB_TAG_OBJECT_URL.format(repo=repo, sha=obj["sha"]),
                    headers=headers, timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        tag_obj = await resp.json()
                        notes = (tag_obj.get("message") or "").strip()
                        published_at = (tag_obj.get("tagger") or {}).get("date", "")

        version = tag.removeprefix("controller-")
        previous = db.get_config("latest_controller_version")

        db.set_config("latest_controller_version", version)
        db.set_config("latest_controller_notes", notes)
        db.set_config("latest_controller_published_at", published_at)

        _controller_cache = {
            "version":      version,
            "current":      CONTROLLER_VERSION,
            "notes":        notes,
            "published_at": published_at,
            "release_url":  f"https://github.com/{repo}/releases/tag/{tag}",
            **_compare_versions(CONTROLLER_VERSION, version),
        }
        _controller_cache_ts = time.monotonic()

        log.info(f"[api] Latest controller release: {version} "
                 f"(running {CONTROLLER_VERSION}, {_controller_cache['status']})")
        if version != previous:
            # Same live-push as device releases: a dashboard left open should
            # not sit on stale information until someone reloads.
            await _push_event({
                "type":         "controller_update",
                "version":      version,
                "notes":        notes,
                "published_at": published_at,
                **_compare_versions(CONTROLLER_VERSION, version),
            })
        return _controller_cache

    except Exception as e:
        log.error(f"[api] Controller release fetch failed: {e}")
        return _controller_cache or None


async def _get_controller_release(request: web.Request) -> web.Response:
    """GET /api/releases/controller"""
    data = await _fetch_controller_release()
    if data is None:
        # Fall back to the DB cache so a GitHub outage does not blank the
        # panel — offline is not the same as "no update exists".
        version = db.get_config("latest_controller_version", "") or ""
        if not version:
            return _ok({"version": None, "current": CONTROLLER_VERSION,
                        "status": "unknown", "available": False})
        data = {
            "version":      version,
            "current":      CONTROLLER_VERSION,
            "notes":        db.get_config("latest_controller_notes", "") or "",
            "published_at": db.get_config("latest_controller_published_at", "") or "",
            "release_url":  "",
            **_compare_versions(CONTROLLER_VERSION, version),
        }
    return _ok(data)



# ─── On-device wake word assets ───────────────────────────────────────────────
#
# The runtime and models are not in the firmware (see em_oww_assets), so they
# have to be installed. This is the field transport; the provisioning wizard
# pushes the same bytes over ADB from the browser, which is far better suited
# to 15MB than the shell plane, and both drive the same idempotent plan.


async def _oww_device_state(live) -> dict:
    """
    What is actually installed on a device, and how much room it has.

    One shell round trip. `md5sum` is asked for per file rather than with a
    glob so a missing directory yields an empty inventory rather than an
    error line that could be mistaken for one.
    """
    d = em_oww_assets.DEVICE_DIR
    out = await _shell_run(live, (
        f'for f in {d}/*.so {d}/*.onnx; do '
        f'[ -f "$f" ] && echo "$(busybox md5sum "$f" | busybox cut -d\" \" -f1) '
        f'$(busybox stat -c %Y "$f") $f"; done; '
        f'echo "FREE $(busybox df -m /data | busybox tail -1)"'
    ), timeout=120.0)

    free_mb = None
    lines = []
    for line in out.splitlines():
        if line.startswith("FREE "):
            # The whole df row, parsed in Python: the available column's INDEX
            # is not stable (busybox wraps a long filesystem name onto its own
            # line) and an awk field number silently yielded "65%" here, which
            # read as no measurement and quietly disabled the space check.
            free_mb = em_oww_assets.parse_free_mb(line[5:])
        else:
            lines.append(line)
    return {
        "installed": em_oww_assets.parse_device_listing("\n".join(lines)),
        "free_mb": free_mb,
    }


def _oww_wanted_models(device_id: str) -> list[str]:
    """
    The models a device should carry, most important first.

    The configured one is pinned and therefore first. Nothing else is added:
    extra slots exist so that models a user has already installed survive a
    switch, not so the controller can push models nobody asked for.
    """
    cfg = db.get_effective_device_config(device_id) or {}
    model = (cfg.get("owwModel") or "").strip()
    return [model] if model else []


def _hold_back_oww_model(live, effective: dict):
    """
    Keep a NEW wake word off a device until it has the model for it.

    Returns (config_to_send, pending_model). `pending_model` is the model the
    device should end up on once the classifier is installed, or None when the
    change can go straight through.

    A device cannot score a wake word whose classifier it does not have. Under
    `owwOnDevice=on` the controller has stood down and no longer triggers on
    its behalf, so telling the device to use a model it lacks produced a device
    with NO wake word: nothing fired, nothing warned, and the dashboard
    reported it healthy (#191). Selecting a wake word a device was never
    provisioned with is an ordinary dashboard action.

    So the device is never told about the new model until the file is there.
    It keeps listening for its CURRENT wake word, on-device, the whole time —
    no silent fall back to controller-side scoring, which is a posture the user
    did not ask for and would not see. If the install fails, the device simply
    stays where it was.

    Only devices that actually score locally are held back. With
    `owwOnDevice=off` the controller does the scoring and the file on the
    device is irrelevant, so the change applies immediately — which is the
    common case, and it stays instant.

    Both the old and the NEW mode are consulted: turning on-device scoring on
    in the same save that changes the wake word would otherwise slip through
    on the strength of the old mode being "off".
    """
    new_model = (effective.get("owwModel") or "").strip()
    if not new_model or new_model == live.oww_model:
        return effective, None
    if not live.oww_shadow_capable:
        return effective, None

    was_local = live.oww_on_device != em_shadow.MODE_OFF
    now_local = em_shadow.normalise_mode(
        effective.get("owwOnDevice", live.oww_on_device)
    ) != em_shadow.MODE_OFF
    if not (was_local or now_local):
        return effective, None

    held = dict(effective)
    held["owwModel"] = live.oww_model
    return held, new_model


async def _install_then_switch(device_id: str, model: str) -> None:
    """
    Install a wake word classifier, then move the device onto it.

    Background task: the push is a multi-megabyte shell-plane transfer over a
    link measured at 5-7% packet loss, and blocking the config save on it would
    time out the request without making anything safer. Nothing is degraded
    while it runs — the device is still on its previous wake word and still
    scoring locally.

    The sync is idempotent, so a device that already has the model completes in
    an md5 compare and the switch is effectively immediate.
    """
    live = _devices.get(device_id)
    if live is None:
        return

    await _push_log_event(
        device_id, "info", "controller",
        f"Installing wake word model {model} before switching to it"
    )
    try:
        result = await _sync_oww_assets(live, device_id)
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    live = _devices.get(device_id)
    if live is None:
        return

    if not result.get("ok"):
        # Deliberately leaves the device where it was: on a wake word it can
        # actually hear. The controller is scoring the NEW model (fleet config
        # decides that), so the two disagree until this is resolved — worth
        # saying loudly, and better than a device that hears nothing.
        await _push_log_event(
            device_id, "error", "controller",
            f"Could not install wake word model {model} "
            f"({result.get('error')}) — this device is still using "
            f"{live.oww_model}"
        )
        log.error(f"[api] [{device_id}] wake word switch to {model} abandoned: "
                  f"{result.get('error')} — device left on {live.oww_model}")
        return

    effective = await asyncio.get_event_loop().run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if (effective.get("owwModel") or "").strip() != model:
        # Changed again while the push was running; that save owns the
        # outcome and has its own install task.
        log.info(f"[api] [{device_id}] wake word changed again during install "
                 f"— dropping the switch to {model}")
        return

    await live.send_control({"type": "config", **effective})
    live.oww_model = model
    import em_esphome
    em_esphome.update_oww_model(device_id, model)
    await _push_log_event(
        device_id, "info", "controller",
        f"Wake word model {model} installed — device switched"
    )
    log.info(f"[api] [{device_id}] wake word switched to {model} after install")


async def reconcile_oww_assets(device_id: str, live) -> None:
    """
    On connect: make sure a locally-scoring device HAS the model it was told
    to use, and put the controller back in charge if it does not.

    Every other install path runs while the device is connected — the wizard
    over ADB, `_install_then_switch` on a config save, the Updates tab by hand.
    A device that was OFFLINE when its wake word changed has none of them: the
    connect handler pushes the effective config directly, so it is told to use
    a classifier it may never have received. Under `owwOnDevice=on` that is a
    device with no wake word at all — it cannot score, and the controller has
    stood down and no longer triggers on its behalf (#191). Changing the wake
    word, or re-scoping the wakeword section, while a device is unplugged is
    enough to produce it.

    This is `oww_model_ready`'s intended writer. Three rules:

    - **Failure to LOOK is not evidence of absence.** Any error reading the
      device's inventory leaves `oww_model_ready` alone, so a shell plane that
      is not up yet — likely, moments after connect — costs nothing. Only a
      successful listing that does not contain the model stands the device
      down. Absence of evidence, per em_shadow.effective_mode's own docstring.
    - **Degrade first, then repair.** The mode is dropped to off the moment the
      model is known missing, which puts the CONTROLLER back to triggering, so
      the device answers throughout the install rather than only after it. That
      ordering is the opposite of `_install_then_switch`, deliberately: there
      the device is already on a wake word it can hear and must not be
      disturbed, here it is already deaf.
    - **Quiet when there is nothing to do.** Devices on this fleet reconnect
      often, so the ordinary path is one shell round trip and no log line.

    Runs as a background task: it is a shell round trip and possibly a
    multi-megabyte push over a link measured at 5-7% packet loss, and nothing
    about the connect handshake should wait on it.
    """
    loop = asyncio.get_event_loop()
    effective = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    # With owwOnDevice=off the controller does the scoring and what is on the
    # device is irrelevant — the common case, and it costs nothing here.
    if em_shadow.normalise_mode(effective.get("owwOnDevice")) == em_shadow.MODE_OFF:
        return
    if not live.oww_shadow_capable:
        return

    desired, _ = em_oww_assets.desired_assets(_oww_wanted_models(device_id))
    try:
        state = await _oww_device_state(live)
    except Exception as e:
        log.info(f"[api] [{device_id}] oww reconcile: could not read the device "
                 f"({e}) — leaving it as configured")
        return

    missing = em_oww_assets.missing_selected_classifier(desired, state["installed"])
    if missing is None:
        live.oww_model_ready = True
        live.oww_on_device = em_shadow.effective_mode(
            effective.get("owwOnDevice"), live.oww_trigger_capable,
            model_ready=True,
        )
        return

    live.oww_model_ready = False
    live.oww_on_device = em_shadow.effective_mode(
        effective.get("owwOnDevice"), live.oww_trigger_capable,
        model_ready=False,
    )
    log.warning(f"[api] [{device_id}] oww reconcile: {missing} is not installed "
                f"— controller-side scoring until it is")
    await _push_log_event(
        device_id, "warn", "controller",
        f"Wake word model {missing} is missing — scoring on the controller "
        f"while it installs"
    )

    try:
        result = await _sync_oww_assets(live, device_id)
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    live = _devices.get(device_id)
    if live is None:
        return

    if not result.get("ok"):
        await _push_log_event(
            device_id, "error", "controller",
            f"Could not install {missing} ({result.get('error')}) — this "
            f"device is scoring on the controller, not locally"
        )
        log.error(f"[api] [{device_id}] oww reconcile: install failed "
                  f"({result.get('error')}) — left on controller-side scoring")
        return

    live.oww_model_ready = True
    live.oww_on_device = em_shadow.effective_mode(
        effective.get("owwOnDevice"), live.oww_trigger_capable,
        model_ready=True,
    )
    # The device builds its scorer from the config push, so it needs telling
    # the model is now there — same mechanism _install_then_switch relies on.
    await live.send_control({"type": "config", **effective})
    await _push_log_event(
        device_id, "info", "controller",
        f"Wake word model {missing} installed — scoring locally again"
    )
    log.info(f"[api] [{device_id}] oww reconcile: {missing} installed, "
             f"mode restored to {live.oww_on_device}")


async def _sync_oww_assets(live, device_id: str, progress=None) -> dict:
    """
    Make a device's asset directory match what it needs. Idempotent.

    Push to `.part` then rename only once md5 matches, so an interrupted
    transfer can never leave a file the device would try to dlopen. md5 is
    the ONLY definition of success — the shell transport has produced files
    of the right length and the wrong content, and a corrupt
    libonnxruntime.so fails at dlopen with an error that names nothing.
    """
    async def say(level: str, msg: str):
        log.info(f"[api] [{device_id}] oww assets: {msg}")
        await _push_log_event(device_id, level, "controller", f"Wake word assets: {msg}")
        if progress:
            await progress(level, msg)

    desired, problems = em_oww_assets.desired_assets(_oww_wanted_models(device_id))
    for p in problems:
        await say("warn", p)
    if not desired:
        return {"ok": False, "error": "; ".join(problems) or "nothing to install"}

    state = await _oww_device_state(live)
    plan = em_oww_assets.plan_sync(desired, state["installed"], state["free_mb"])

    if plan.blocked:
        await say("error", plan.blocked)
        return {"ok": False, "error": plan.blocked}

    if plan.is_noop:
        await say("info", "already up to date")
        return {"ok": True, "pushed": [], "pruned": [], "problems": problems}

    d = em_oww_assets.DEVICE_DIR
    await _shell_run(live, f"mkdir -p {d}")

    pushed = []
    for asset in plan.push:
        mb = asset.size / (1024 * 1024)
        await say("info", f"sending {asset.name} ({mb:.1f}MB)…")
        try:
            data = asset.source.read_bytes()
        except OSError as e:
            await say("error", f"{asset.name} unreadable on the controller: {e}")
            return {"ok": False, "error": f"{asset.name}: {e}"}

        dest = em_oww_assets.device_path(asset.name)
        part = f"{dest}.part"
        # NOT `pushed` — that is the accumulator above, and assigning the
        # transfer result to it shadowed the list on the first file, so the
        # append below raised AttributeError and asset installation failed
        # outright. TransferResult is truthy-compatible, which is what let
        # this reach a release: every `if not …` call site kept working and
        # only the one that treated it as a list broke.
        sent = await _stream_file_to_device(live, data, part, mode="644")
        if not sent:
            await say("error", f"{asset.name} transfer failed: {sent}")
            return {"ok": False, "error": f"{asset.name}: {sent}"}

        res = await _shell_run(live, (
            f'GOT=$(busybox md5sum {part} | busybox cut -d" " -f1); '
            f'if [ "$GOT" = "{asset.md5}" ]; then mv {part} {dest} && '
            f'chmod 644 {dest} && echo OK; else rm -f {part}; echo "BAD:$GOT"; fi'
        ), timeout=120.0)
        if "OK" not in res:
            await say("error", f"{asset.name} verify failed ({res.strip() or 'no output'})")
            return {"ok": False, "error": f"{asset.name}: md5 mismatch"}
        pushed.append(asset.name)

    for name in plan.prune:
        await _shell_run(live, f"rm -f {em_oww_assets.device_path(name)}")
    if plan.prune:
        await say("info", f"removed unused: {', '.join(plan.prune)}")

    # Touch the selected classifier so LRU eviction sees it as most recent
    # even on a sync that did not need to re-push it.
    sel = next((a for a in desired if a.kind == "classifier"), None)
    if sel:
        await _shell_run(live, f"touch {em_oww_assets.device_path(sel.name)}")

    await say("info", f"installed {len(pushed)} file(s) — "
                      f"restart the device to start scoring")
    return {"ok": True, "pushed": pushed, "pruned": plan.prune, "problems": problems}


async def _get_oww_assets(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/oww_assets — what is installed, and what is needed."""
    device_id = request.match_info["id"]
    live = _devices.get(device_id)

    desired, problems = em_oww_assets.desired_assets(_oww_wanted_models(device_id))
    payload = {
        "device_dir": em_oww_assets.DEVICE_DIR,
        "problems": problems,
        "required": [
            {"name": a.name, "kind": a.kind, "size": a.size, "md5": a.md5}
            for a in desired
        ],
        "connected": live is not None,
    }
    if live is None:
        # Not an error: the state is simply unknowable, and saying "not
        # installed" for an offline device would be a guess that reads as fact.
        payload.update({"status": "unknown", "installed": None, "free_mb": None})
        return _ok(payload)

    state = await _oww_device_state(live)
    plan = em_oww_assets.plan_sync(desired, state["installed"], state["free_mb"])
    payload.update({
        "installed": {k: v[0] for k, v in state["installed"].items()},
        "free_mb": state["free_mb"],
        "missing": [a.name for a in plan.push],
        "prunable": plan.prune,
        "blocked": plan.blocked,
        "status": ("blocked" if plan.blocked
                   else "installed" if plan.is_noop
                   else "outdated" if plan.keep
                   else "absent"),
    })
    return _ok(payload)


@auth.require_admin
async def _post_oww_assets(request: web.Request) -> web.Response:
    """POST /api/devices/{id}/oww_assets — install or update them."""
    device_id = request.match_info["id"]
    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)
    result = await _sync_oww_assets(live, device_id)
    if not result.get("ok"):
        return _error("sync_failed", result.get("error", "sync failed"), 500)
    return _ok(result)



@auth.require_auth
async def _get_provision_oww_manifest(request: web.Request) -> web.Response:
    """
    GET /api/provision/oww_assets — what the wizard should push, and where.

    The wizard pushes over ADB from the browser rather than through the shell
    plane: a freshly-flashed device is not in _devices yet, and USB is far
    better suited to 15MB than a base64 heredoc. Same bytes, same md5s, same
    destination as the field path — only the transport differs.
    """
    fleet = db.get_global_device_config() or {}
    models = [m for m in [fleet.get("owwModel") or ""] if m]
    desired, problems = em_oww_assets.desired_assets(models)
    return _ok({
        "dir": em_oww_assets.DEVICE_DIR,
        "problems": problems,
        "assets": [{"name": a.name, "size": a.size, "md5": a.md5} for a in desired],
    })


@auth.require_auth
async def _get_provision_oww_asset(request: web.Request) -> web.Response:
    """
    GET /api/provision/oww_asset/{name} — the bytes of one asset.

    Serves only names the manifest just listed, resolved from the manifest
    rather than from the request: the filename is user-supplied, and joining
    it onto a directory is how a path traversal gets written by accident.
    """
    name = request.match_info["name"]
    fleet = db.get_global_device_config() or {}
    desired, _ = em_oww_assets.desired_assets(
        [m for m in [fleet.get("owwModel") or ""] if m])
    asset = next((a for a in desired if a.name == name), None)
    if asset is None:
        return _error("not_found", f"{name} is not a current asset", 404)
    try:
        data = asset.source.read_bytes()
    except OSError as e:
        return _error("unreadable", str(e), 500)
    return web.Response(
        body=data,
        content_type="application/octet-stream",
        headers={"X-Asset-MD5": asset.md5},
    )



def _read_first_line(path: str) -> str:
    with open(path) as fh:
        return fh.readline().strip()


def _proc_meminfo() -> dict[str, float]:
    """/proc/meminfo in MB. Empty on anything without procfs."""
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out[parts[0].rstrip(":")] = int(parts[1]) / 1024.0
    except OSError:
        pass
    return out


def _controller_stats() -> dict:
    """
    The controller's own CPU, memory and storage.

    Read from /proc and the filesystem rather than psutil, which is not a
    dependency and is not worth becoming one for a handful of files. Every
    lookup degrades to an absent key: a bundle missing a stat is a nuisance,
    a bundle that 500s when the host is unusual is the failure that matters,
    since the bundle is what someone reaches for when things are wrong.

    Paths are deliberately never reported — on a bare-metal install the data
    directory carries the account name, which is the leak this same change
    fixes in the log tail.
    """
    stats: dict[str, Any] = {}
    try:
        _ctrl = _running_controller_module()
        if _ctrl is not None:
            stats["loop_lag_peak_ms"] = round(_ctrl._loop_lag_peak_ms, 1)
    except Exception:
        pass

    stats["python"] = platform.python_version()
    stats["platform"] = f"{platform.system()} {platform.machine()}"
    stats["cpu_count"] = os.cpu_count()
    stats["container"] = os.path.exists("/.dockerenv")

    try:
        load = os.getloadavg()
        stats["load_1"], stats["load_5"], stats["load_15"] = [round(x, 2) for x in load]
    except OSError:
        pass

    mem = _proc_meminfo()
    if "MemTotal" in mem:
        stats["mem_total_mb"] = round(mem["MemTotal"], 1)
    if "MemAvailable" in mem:
        stats["mem_available_mb"] = round(mem["MemAvailable"], 1)

    # cgroup v2 then v1: in a container MemTotal is the HOST's memory, which
    # reads as plenty of headroom while the container is being OOM-killed.
    for limit_path in ("/sys/fs/cgroup/memory.max",
                       "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = _read_first_line(limit_path)
            if raw and raw != "max":
                val = int(raw) / 1048576.0
                # An unset v1 limit is a sentinel near 2^63, not a real cap.
                if val < 1024 * 1024:
                    stats["mem_limit_mb"] = round(val, 1)
            break
        except (OSError, ValueError):
            continue

    try:
        with open("/proc/self/status") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    stats["rss_mb"] = round(int(ln.split()[1]) / 1024.0, 1)
                    break
    except OSError:
        pass

    # Process CPU as a share of one core, averaged over the process lifetime.
    # A lifetime average, not an instant sample: a support bundle is taken
    # once, and a single 100ms sample of an asyncio process is noise.
    try:
        uptime = time.time() - _PROCESS_START
        stats["uptime_s"] = int(uptime)
        cpu_time = sum(os.times()[:2])
        # Below a few seconds the ratio is startup cost divided by almost
        # nothing — it reads as 1147% and looks like a controller on fire.
        # Omitted rather than reported wrong; a bundle taken in the first
        # seconds of a run has no CPU history worth having anyway.
        # Percent of ONE core, as `top` reports it — over 100 means more than
        # a core's worth. The windowed figures are what point anywhere: a
        # lifetime average cannot tell a controller busy right now from one
        # that was busy for an hour this morning.
        stats.update(_cpu_history.windows(time.monotonic(), cpu_time))
        if uptime >= 5.0:
            stats["cpu_pct_life"] = round(100.0 * cpu_time / uptime, 1)
    except Exception:
        pass

    # Same resolution as em_recordings, via its helper — the DB path lives in
    # one place and this must not become a second definition of it.
    db_path = Path(os.environ.get("DB_PATH", "echomuse.db")).resolve()
    try:
        usage = shutil.disk_usage(db_path.parent)
        stats["data_used_mb"] = round(usage.used / 1048576.0, 1)
        stats["data_free_mb"] = round(usage.free / 1048576.0, 1)
    except OSError:
        pass
    try:
        # The database is the thing that grows without anyone watching it.
        stats["db_mb"] = round(db_path.stat().st_size / 1048576.0, 1)
    except OSError:
        pass
    try:
        rec_dir = em_recordings.recordings_dir()
        total = sum(f.stat().st_size for f in rec_dir.iterdir() if f.is_file())
        stats["recordings_mb"] = round(total / 1048576.0, 1)
    except OSError:
        pass

    return stats


@auth.require_admin
async def _post_provision_diagnostics(request: web.Request) -> web.Response:
    """
    POST /api/provision/diagnostics

    The wizard collects raw probe output when a step fails and posts it here;
    this returns the sanitised file to attach to an issue (#87).

    Packaged on the controller rather than in the browser on purpose. The
    redaction rules and their tests live in em_support, and a second copy in
    JavaScript would drift from them without anyone noticing until a file
    carried an SSID. This function only carries; em_support decides.

    Admin-only and a download rather than a display, the same call the support
    bundle makes: it is meant to be looked at before it is shared.
    """
    body = await _json_body(request)
    diag = em_support.build_provision_diagnostics(
        step=body.get("step") or "unknown",
        error=body.get("error") or "",
        probes=body.get("probes") or {},
        transcript=body.get("transcript") or None,
        # The wizard knows which network the operator picked; the file cannot
        # work it out once the names are gone, and "the one you wanted is
        # WPA3" is the whole answer on a #82-shaped failure.
        selected_ssid=body.get("selected_ssid") or None,
        controller_version=CONTROLLER_VERSION,
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=em_support.to_json(diag).encode(),
        content_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="echomuse-provision-{stamp}.json"'},
    )


@auth.require_admin
async def _get_support_bundle(request: web.Request) -> web.Response:
    """
    GET /api/support/bundle — one file to attach to an issue.

    Admin-only, and deliberately a download rather than a display: it is
    meant to be reviewed before it is shared. The privacy contract lives in
    em_support (allowlist, no speech, no labels, no network identifiers) and
    is enforced there, not here — this function only gathers.
    """
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_all_devices)

    since = time.time() - 24 * 3600
    turns, metrics, counters = [], [], []
    device_configs, live_state, logs = {}, {}, []

    for row in rows:
        did = row["device_id"]
        device_configs[did] = await loop.run_in_executor(
            None, db.get_effective_device_config, did)
        live = _devices.get(did)
        live_state[did] = {
            "connected":    live is not None,
            # Capabilities decide which HA entities are advertised at all,
            # which is the first thing to check when one is "missing".
            "capabilities": list(getattr(live, "capabilities", []) or []) if live else [],
            # When ambient_light is missing from the list above, this says
            # WHY: no_chip (hardware revision without the part), no_attribute
            # (driver has not bound), or ok. None means firmware too old to
            # report it — not a fault. Carries the i2c device names it saw,
            # which is what makes a no_chip answer checkable rather than
            # merely asserted, and identifies an unfamiliar board revision
            # the first time one turns up (#90).
            "ambient_light_status": getattr(live, "ambient_light_status", None) if live else None,
            "muted":        getattr(live, "muted", None) if live else None,
            "rtt_ms":       getattr(live, "rtt_last_ms", None) if live else None,
            "volume":       getattr(live, "volume", None) if live else None,
            "media_state":  em_player.state(did),
            "stats":        em_support.redact_stats(live.stats if live else None),
        }
        turns += await loop.run_in_executor(None, db.get_turns, did, 50, since)
        # get_device_metrics resolves its own rows and does NOT carry the
        # device, so without this every device's hours pooled into one
        # anonymous list — six devices' CPU and memory with no way to tell
        # whose was whose. (`_pick` wants .keys(), which a dict already has.)
        metrics += [dict(m, device_id=did)
                    for m in await loop.run_in_executor(None, db.get_device_metrics, did, since)]
        counters += await loop.run_in_executor(None, db.get_wake_counters, did, since)
        # Fetch deep and thin, rather than fetching 100 and shipping noise:
        # 89% of this table is [mem] heap dumps, so a flat 100 was ~11 lines
        # of evidence. Newest first, which is what thin_noise expects.
        raw = await loop.run_in_executor(None, db.get_device_logs, did, 500, None)
        pairs = [(lg["ts"], f"{lg['ts']} [{lg['level']}] {lg['source']}: {lg['message']}")
                 for lg in raw]
        # Sorted oldest-first at the end: a log someone reads should run
        # forwards, and per-device blocks in reverse order do not.
        logs += em_support.thin_noise(pairs, key=lambda p: p[1])[:DEVICE_LOG_LINES]

    bundle = em_support.build(
        controller_version=CONTROLLER_VERSION,
        devices=rows,
        fleet_config=await loop.run_in_executor(None, db.get_global_device_config),
        schema_version=len(db.MIGRATIONS),
        turns=turns,
        metrics=metrics,
        counters=counters,
        device_configs=device_configs,
        live_state=live_state,
        controller_log=_log_ring.tail(),
        device_log=[ln for _, ln in sorted(logs)],
        # From the user table, not guessed at: an account name in log prose
        # has nothing to pattern-match on. Mapped to the ROLE it is replaced
        # with — "an admin opened a shell" is the diagnostic content, and
        # this is a single-operator system, so a positional alias would be a
        # one-to-one stand-in for a real person.
        accounts={u["username"]: u["role"] for u in
                  await loop.run_in_executor(None, db.get_all_users)},
        controller_stats=await loop.run_in_executor(None, _controller_stats),
    )
    body = em_support.to_json(bundle)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=body.encode(),
        content_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="echomuse-support-{stamp}.json"'},
    )


async def _fetch_binary(download_url: str,
                        version: str = "") -> Optional[bytes]:
    """
    The release binary, from disk if we already have it.

    A published tag never changes what it points at, so the download is worth
    doing once per release rather than once per device — a fleet update used to
    pull the same ~10MB for every Dot, and the provisioning wizard again for
    every device it set up.

    `version` is optional so a caller with only a URL still works; it simply
    does not get the cache. Nothing here can fail an update: a cache miss, an
    unwritable directory or a corrupt entry all end in an ordinary download.
    """
    if version:
        cached = await asyncio.get_event_loop().run_in_executor(
            None, em_firmware.read, version
        )
        if cached is not None:
            return cached

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
                data = await resp.read()
    except Exception as e:
        log.error(f"[api] Binary download exception: {e}")
        return None

    if version and data:
        # Off the event loop: hashing and writing 10MB blocks it for long
        # enough to delay speaker frames, and this runs during an OTA.
        await asyncio.get_event_loop().run_in_executor(
            None, em_firmware.write, version, data
        )
    return data


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
        interval = _update_check_interval()
        if interval <= 0:
            # Disabled (#159): no fetch, no outbound connection. Re-read
            # after a fixed park so re-enabling from the dashboard takes
            # effect without a restart.
            await asyncio.sleep(60)
            continue

        try:
            await _fetch_latest_release()
        except Exception as e:
            log.error(f"[api] Release poll loop error: {e}")

        # Same cadence, separate failure domain: a GitHub hiccup on one must
        # not cost the other its poll.
        try:
            await _fetch_controller_release(force=True)
        except Exception as e:
            log.error(f"[api] Controller release poll error: {e}")

        # Floor of 1s so even an absurd tiny positive interval cannot spin
        # the loop faster than the event loop allows.
        await asyncio.sleep(max(interval, 1))


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

# Devices whose last update failed and whose supervisor log has not yet been
# collected. The fetch cannot happen at failure time — the device is gone,
# which is the whole problem — so it waits for the next connect.
_supervisor_log_wanted: set[str] = set()

# Supervisor decisions kept on the device, surviving the reboot that /tmp does
# not. Must match SUP_LOG in device_payloads/start_server.sh.
SUPERVISOR_LOG = "/data/local/etc/echomuse/supervisor.log"


async def _collect_supervisor_log(device_id: str) -> None:
    """
    Pull the device's supervisor log after a failed update, on reconnect.

    A failed update destroys its own evidence: everything start_server.sh
    logs goes to /tmp, which is RAM-backed, so the power cycle used to
    recover wipes exactly the lines that would explain it (2026-08-01, a
    device that never came back and could not be diagnosed afterwards).

    The persistent log fixes the storage half. This is the other half: the
    controller notices it is owed an explanation and fetches it the moment
    the device is reachable again, pushing it into that device's log events —
    so the evidence arrives where someone would look, instead of sitting in a
    file nobody knows to read.
    """
    live = _devices.get(device_id)
    if live is None:
        return
    out = await _shell_run(live, f"busybox tail -c 4096 {SUPERVISOR_LOG}", timeout=30.0)
    text = (out or "").strip()
    if not text:
        await _push_log_event(device_id, "warn", "controller",
            "Update failed, and the device has no supervisor log — firmware "
            "predating it, or start_server.sh has not been synced yet "
            "(it takes effect on the next device reboot).")
        return
    await _push_log_event(device_id, "warn", "controller",
        "Supervisor log from the failed update:\n" + text)


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

    # Owed an explanation from a failed update? Collect it now the device is
    # reachable again. Removed from the set on the way in, so a flapping
    # device cannot queue repeated fetches, and scheduled rather than awaited
    # so a slow shell never delays the connect path.
    if device_id in _supervisor_log_wanted:
        _supervisor_log_wanted.discard(device_id)

        async def _collect_soon(_id=device_id):
            # The device has just registered; give its shell plane a moment
            # before demanding a session on it.
            await asyncio.sleep(3.0)
            try:
                await _collect_supervisor_log(_id)
            except Exception as e:
                log.warning(f"[api] supervisor log fetch failed for {_id}: {e}")

        asyncio.create_task(_collect_soon())


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

def _stored_volume(row):
    """Last-known volume as an HA 0..1 float, from the persisted config."""
    try:
        level = json.loads(row["config"] or "{}").get("startupVolume")
    except (json.JSONDecodeError, TypeError):
        return None
    if level is None:
        return None
    try:
        # float() first, deliberately: em_volume swallows bad input and
        # returns 0.0, which is the right answer on the audio path and the
        # wrong one here — this function's None means "not known", and a
        # corrupt stored value must not report as "silent".
        return em_volume.device_level_to_ha(float(level))
    except (TypeError, ValueError):
        return None


def _row_sections(row) -> list:
    """
    Overridden config sections from a device row, tolerant of a row that
    predates the v8 column or carries unparseable JSON — either way the safe
    reading is "overrides nothing", which shows the device as fleet-scoped
    rather than inventing overrides it does not have.
    """
    try:
        raw = row["config_sections"]
    except (IndexError, KeyError):
        return []
    try:
        return sections_mod.normalise(json.loads(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def _merge_device(row) -> dict:
    """
    Merge a DB device row with live in-memory state.

    DB row provides persistent fields (label, config, firmware_ver etc).
    Live _devices dict provides transient state (connected, speaking,
    muted, listening, thinking).
    """
    device_id = row["device_id"]
    live = _devices.get(device_id)
    # Lazy, for the reason every other em_esphome call site here is lazy:
    # em_esphome imports em_api at module level. After the first call this
    # is a sys.modules lookup, which is what makes it affordable on a path
    # that runs per device per dashboard poll.
    import em_esphome

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
        "config_sections":    _row_sections(row),
        # Compat view for older readers: no overridden sections == fleet.
        "use_global_config":  not _row_sections(row),
        "esphome_port":       row["esphome_api_port"],
        "ble_proxy_port":     row["ble_proxy_port"],
        # Live — defaults when device is not connected
        "connected":        live is not None,
        "speaking":         live.speaking  if live else False,
        "muted":            getattr(live, "muted",     False) if live else False,
        "listening":        getattr(live, "listening", False) if live else False,
        "thinking":         getattr(live, "thinking",  False) if live else False,
        "stats":            live.stats if live else None,
        # Control-plane round trip, controller-measured. The RF counters are
        # structurally zero on this hardware (the MTK driver populates
        # neither retries nor noise), so this is the only latency signal.
        "rttMs":            getattr(live, "rtt_last_ms", None) if live else None,
        # Volume is persisted device state, not config (see
        # em_config_sections.STATE_KEYS): the live level while connected,
        # otherwise the last one the device reported, so an offline device
        # still shows where it will come back.
        "volume":           (live.volume if live is not None
                             else _stored_volume(row)),
        # Controller-side BT proxy state — non-None only while the device's
        # bleProxyEnabled config has a proxy server instantiated.
        "bleProxy":         em_ble_proxy.get_status(device_id),
        # Controller-side voice satellite state: whether Home Assistant is
        # actually on the other end of this device's ESPHome port. Without
        # it a device HA has never connected to renders as idle, which is
        # the same thing a working device renders as (#349) — the wake word
        # fires, the ring lights, and the turn dies in milliseconds.
        "voiceSatellite":   em_esphome.get_status(device_id),
        # Device-link security: token issued (persistent) + whether the
        # current control connection came in over the TLS listener (live).
        "linkTokenIssued":  bool(row["token"]) if "token" in row.keys() else False,
        "linkTls":          getattr(live, "secure", False) if live else False,
        # Q4 fix (2026-07-05 review): near-miss counter — same lifecycle as
        # the rest of this "Live" section (resets on reconnect, since it
        # lives on the per-connection Device object, not the DB row).
        "owwNearMisses":    getattr(live, "oww_near_misses", 0) if live else 0,
        # What this firmware can be asked to do, by capability rather than by
        # version comparison. Drives whether the dashboard OFFERS on-device
        # scoring: a toggle that silently does nothing on old firmware is worse
        # than no toggle, because it looks like the feature is broken.
        "owwShadowCapable": getattr(live, "oww_shadow_capable", False) if live else False,
        # Separate from shadow: firmware in the field scores and reports
        # without being able to act on it, and offering those "on" produces a
        # device that never answers.
        "owwTriggerCapable": getattr(live, "oww_trigger_capable", False) if live else False,
        "audioMixCapable": getattr(live, "audio_mix_capable", False) if live else False,
        # Gates the AEC delay slider, which only means anything on the
        # software tap. Paired with aecRef because the capability says the
        # firmware KNOWS how to use a hardware reference and aecRef says
        # whether this board turned out to have one — a device can be
        # capable and still be running on the software tap.
        "aecHwRefCapable": getattr(live, "aec_hw_ref_capable", False) if live else False,
        "aecRef":          getattr(live, "aec_ref", None) if live else None,
        # Gates the tap-as-event toggle — see em_button.decide.
        "buttonHoldCapable": getattr(live, "button_hold_capable", False) if live else False,
        # Whether the device found its ambient light sensor. Reported so the
        # dashboard can tell "no sensor" apart from "sensor present, no reading
        # yet" — which is the question #90 had to be answered by hand, because
        # nothing on screen showed the lux value at all and the only way to
        # check was a support bundle.
        "ambientLightCapable":
            "ambient_light" in (getattr(live, "capabilities", []) or []) if live else False,
        # WiFi change state (survives the reconnect a change causes)
        "wifi":             wifi_state(device_id),
        # Update state
        "update_in_progress": device_id in _updates_in_progress,
        # Waiting on the global OTA lock — started, but nothing has been sent
        # to this device yet. Distinct from update_in_progress so the panel
        # does not report a transfer that has not begun.
        "update_queued":      device_id in _updates_queued,
        # Last OTA/rollback failure (None when the last attempt succeeded or
        # none was made) — lets the dashboard show a terminal ✗ state instead
        # of "updating…" forever when an update aborts.
        "update_error":       _update_errors.get(device_id),
    }
