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
    runner = await em_api.create_runner()
    await runner.setup()
    site = web.TCPSite(runner, host, port + 1)   # or same port via middleware
    await site.start()
    ...
    await runner.cleanup()

Live state shared with em_controller (the device registry, shell sessions)
is in em_state, which both import by name.
"""

import asyncio
import contextlib
import html as _html
import json
import secrets
import logging
import os
import platform
import posixpath as _posixpath
import re
from shlex import quote as _sh_quote
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
import websockets

import em_db as db
import em_esphome
import em_events
import em_shell
import em_releases
import em_payloads
import em_ota
import em_oww_sync
import em_emos_ota
import em_reconcile
import em_http
import em_state
import em_health
import em_auth as auth
import em_ble_proxy
import em_config_sections as sections_mod
import em_config_types
import em_dbwriter
import em_tasks
import em_console_pw
import em_tcp
import em_labels
import em_emos_build
import em_emos_update
import em_firmware
import em_ingressauth
import em_routes_auth
import em_routes_oww
import em_routes_provision
import em_pairing
import em_pki
import em_player
import em_recordings
import em_wake_samples
import em_volume
import em_wifi
import em_endpoints
import em_scenes
import em_shadow
import em_support
from version import VERSION as CONTROLLER_VERSION
from version import compare as _compare_versions
from version import firmware_update as _firmware_update

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
# Device-link TLS material directory — set by em_controller.main() once
# em_pki.ensure_pki() succeeds. None = TLS listener not running (no
# cryptography package / setup failure); credential endpoints then 503.
_tls_dir: str | None = None


def set_tls_dir(tls_dir: str) -> None:
    global _tls_dir
    _tls_dir = tls_dir


# This controller's own address and device ports: the defaults for an entry
# in controllerEndpoints, and what the dashboard offers to fill in.
# tls_port is 0 when the wss listener is not running.
_link = {"ip": "", "port": 8767, "tls_port": 0}


def set_link_ports(ip: str, port: int, tls_port: int) -> None:
    _link.update(ip=ip or "", port=port, tls_port=tls_port)

# Pending local binary uploads — keyed by UUID token, expire after 10 minutes.
_pending_uploads: dict[str, bytes] = {}

# Uploaded payload bundles (the dev path), token -> (files, version).
_pending_emos_uploads: dict[str, tuple] = {}

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

    em_routes_auth.register(app)

    # Devices. A literal path wins over a parameterised one in any order;
    # order decides only between parameterised routes that overlap
    # (tests/test_route_resolution.py).
    app.router.add_get("/api/devices",                    _get_devices)
    app.router.add_get("/api/devices/pending",            _get_pending)
    app.router.add_get("/api/devices/{id}",               _get_device)
    app.router.add_patch("/api/devices/{id}",             _patch_device)
    app.router.add_delete("/api/devices/{id}",            _delete_device)
    app.router.add_post("/api/devices/{id}/approve",      _post_approve)
    app.router.add_get("/api/devices/{id}/config",        _get_device_config)
    app.router.add_get("/api/devices/{id}/sendspin/token", _get_sendspin_token)
    app.router.add_get("/api/devices/{id}/ble_proxy/key", _get_ble_proxy_key)
    app.router.add_post("/api/devices/{id}/config",       _post_device_config)
    app.router.add_get("/api/devices/{id}/logs",          _get_device_logs)
    app.router.add_get("/api/devices/{id}/turns",         _get_device_turns)
    app.router.add_get("/api/devices/{id}/activity",      _get_device_activity)
    app.router.add_get("/api/devices/{id}/turns/{turn}/audio", _get_turn_audio)
    app.router.add_get("/api/devices/{id}/wake-samples", _get_wake_samples)
    app.router.add_patch("/api/devices/{id}/wake-samples/{sample}", _patch_wake_sample)
    app.router.add_delete("/api/devices/{id}/wake-samples/{sample}", _delete_wake_sample)
    app.router.add_get("/api/devices/{id}/wake-samples/{sample}/audio", _get_wake_sample_audio)
    app.router.add_post("/api/devices/{id}/wifi",         _post_device_wifi)
    app.router.add_post("/api/devices/{id}/wifi/scan",    _post_device_wifi_scan)
    app.router.add_post("/api/devices/{id}/update",       _post_device_update)
    app.router.add_post("/api/devices/{id}/emos_update",  _post_device_emos_update)
    app.router.add_post("/api/emos/upload",               _post_emos_upload)
    app.router.add_post("/api/devices/{id}/rollback",     _post_device_rollback)
    app.router.add_post("/api/releases/upload",           _post_upload_binary)

    em_routes_oww.register(app)
    app.router.add_get("/api/devices/{id}/shell",         _ws_shell)

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
    em_routes_provision.register(app)
    app.router.add_post("/api/provision/tls_credentials", _post_provision_tls_credentials)
    app.router.add_get("/api/provision/controller_endpoints", _get_provision_controller_endpoints)
    app.router.add_post("/api/devices/{id}/pair",         _post_pair)
    app.router.add_post("/api/devices/{id}/debloat",      _post_debloat)

    # Live events WebSocket
    app.router.add_get("/api/events", _ws_events)

    return app


async def create_runner() -> web.AppRunner:
    """Convenience wrapper — create_app + AppRunner."""
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
    if (em_ingressauth.INGRESS_ONLY
            and request.remote != em_ingressauth.INGRESS_GATEWAY_IP):
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
        return em_http.error("internal_error", "An internal error occurred", 500)


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


def _bundle_version() -> str:
    """The dashboard bundle's mtime, as the string used in its URL.

    One function so `_serve_dashboard` (which stamps it) and
    `/api/system/status` (which publishes it for comparison) cannot disagree
    about what identifies a build. Two call sites deriving the same value
    independently is how a staleness check ends up permanently stale, or
    permanently fresh, with nothing to show for it either way.

    An unreadable bundle returns "", which compares equal to the "" a client
    reports when it cannot find its own script tag — so the check degrades to
    "say nothing" rather than to a reload prompt nobody can satisfy.
    """
    try:
        return str(int((STATIC_DIR / "dashboard.js").stat().st_mtime))
    except OSError:
        return ""


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
    stamp = _bundle_version()
    if stamp:
        page = page.replace(
            "static/dashboard.js",
            f"static/dashboard.js?v={stamp}",
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
    return em_http.ok({"needs_setup": auth.get_bootstrap_token() is not None})


async def _post_setup(request: web.Request) -> web.Response:
    """
    POST /api/setup — first-run admin account creation.

    Body: {token, username, password}
    Returns 201 + {token, role} on success so the client is immediately
    logged in after setup.
    """
    body = await em_http.json_body(request)
    token    = em_http.require_str(body, "token")
    username = em_http.require_str(body, "username")
    password = em_http.require_str(body, "password")

    await auth.create_first_admin(token, username, password)

    session_token, role = await auth.login(username, password)
    return em_http.ok({"token": session_token, "role": role}, status=201)


# ─── Devices ──────────────────────────────────────────────────────────────────

def _listen_json(live) -> dict | None:
    view = getattr(live, "listen_view", None)
    if view is None:
        return None
    return {"state": view.state, "streams": view.streams, "reason": view.reason}


@auth.require_auth
async def _get_devices(request: web.Request) -> web.Response:
    """GET /api/devices — all devices, live state merged with DB."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_all_devices)
    health = await loop.run_in_executor(None, db.latest_health)
    return em_http.ok([_merge_device(row, health.get(row["device_id"])) for row in rows])


@auth.require_auth
async def _get_pending(request: web.Request) -> web.Response:
    """GET /api/devices/pending — unapproved devices."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_pending_devices)
    return em_http.ok([_merge_device(row) for row in rows])


@auth.require_auth
async def _get_device(request: web.Request) -> web.Response:
    """GET /api/devices/{id}"""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    return em_http.ok(_merge_device(row))


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
        return em_http.error("bad_request", "limit/since must be numeric", 400)
    loop  = asyncio.get_event_loop()
    turns = await loop.run_in_executor(
        None, lambda: db.get_turns(device_id, limit, since)
    )
    return em_http.ok(_redact_turns_for(turns, request["user"]))


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
        return em_http.error("bad_request", "turn must be an integer", 400)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    name = em_recordings.filename(device_id, turn_id)
    path = em_recordings.resolve(device_id, name) if name else None
    if path is None:
        return em_http.error("no_recording",
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


@auth.require_admin
async def _get_wake_samples(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        limit = min(max(int(request.query.get("limit", 50)), 1), 100)
    except ValueError:
        return em_http.error("bad_request", "limit must be an integer", 400)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_wake_samples, device_id, limit)
    return em_http.ok([dict(row) for row in rows])


@auth.require_admin
async def _patch_wake_sample(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    body = await em_http.json_body(request)
    label = body.get("label")
    if label not in ("wake", "not_wake", "uncertain"):
        return em_http.error("bad_request", "label must be wake, not_wake, or uncertain", 400)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, db.set_wake_sample_label,
                                    device_id, sample_id, label)
    return em_http.ok({"id": sample_id, "label": label}) if ok else em_http.error(
        "sample_not_found", "No wake sample with that id", 404)


@auth.require_admin
async def _delete_wake_sample(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    loop = asyncio.get_event_loop()
    name = await loop.run_in_executor(None, db.delete_wake_sample,
                                      device_id, sample_id)
    if name is None:
        return em_http.error("sample_not_found", "No wake sample with that id", 404)
    return em_http.ok({"id": sample_id, "deleted": True})


@auth.require_admin
async def _get_wake_sample_audio(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_wake_samples, device_id, 100)
    row = next((item for item in rows if item["id"] == sample_id), None)
    path = em_wake_samples.resolve(device_id, row["audio_file"]) if row else None
    if path is None:
        return em_http.error("no_recording", "No saved audio for this sample", 404)
    return web.FileResponse(path, headers={
        "Content-Type": "audio/wav",
        "Content-Disposition": f'attachment; filename="{_slug(device_id)}-wake{sample_id}.wav"',
        "Cache-Control": "private, max-age=60",
    })


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
        return em_http.error("bad_request", "days must be an integer", 400)
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

    return em_http.ok({
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
    body  = await em_http.json_body(request)
    label = _require_label(body)
    await _require_unique_label(label, device_id)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.set_device_label, device_id, label)
    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"label": label}})
    # Music Assistant lists the Sendspin player by this name; the device
    # restarts the player to re-advertise it.
    live = em_state.devices.get(device_id)
    if live is not None and getattr(live, "sendspin_capable", False):
        with contextlib.suppress(Exception):
            await live.send_control({"type": "config", "sendspinName": label})
    return em_http.ok({"device_id": device_id, "label": label})


# How long to wait for a device to answer sendspin_token_request. It is a
# file read and one message on the control plane; seconds of slack are for
# this fleet's link stalls, not for the work.
SENDSPIN_TOKEN_TIMEOUT_S = 8


@auth.require_admin
async def _get_ble_proxy_key(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/ble_proxy/key — the encryption key Home Assistant
    asks for when it adds or re-authenticates this Echo's Bluetooth proxy.

    Handed to the one request and nothing else: never logged, never put in an
    event (events reach every open tab and support bundles). It is all that
    stands between the LAN and whatever the proxy can connect to. Admin only
    for the same reason. There is a key only while connections are on —
    a passive proxy is unencrypted and has nothing to show.
    """
    device_id = request.match_info["id"]
    status = em_ble_proxy.get_status(device_id)
    if status is None or not status.get("connections"):
        return em_http.error("connections_off",
                      "Turn on Bluetooth connections for this Echo first", 409)
    key = await asyncio.get_event_loop().run_in_executor(
        None, db.ensure_ble_proxy_key, device_id)
    if not key:
        return em_http.error("not_found", "Unknown device", 404)
    return em_http.ok({"key": key})


@auth.require_admin
async def _get_sendspin_token(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/sendspin/token — the pairing token to paste
    into Music Assistant.

    Asked of the device each time and never stored or logged: it carries the
    device's pairing key, and anyone holding it can pair with the device.
    Admin only for the same reason.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "The Echo is offline", 409)
    if not getattr(live, "sendspin_capable", False):
        return em_http.error("unsupported", "This Echo's firmware has no Sendspin player", 409)
    waiter = asyncio.get_event_loop().create_future()
    live.sendspin_token_waiter = waiter
    try:
        await live.send_control({"type": "sendspin_token_request"})
        reply = await asyncio.wait_for(waiter, SENDSPIN_TOKEN_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):
        return em_http.error("timeout", "The Echo did not answer", 504)
    finally:
        if live.sendspin_token_waiter is waiter:
            live.sendspin_token_waiter = None
    if reply.get("error") or not reply.get("token"):
        return em_http.error("sendspin_off", "Turn Sendspin on for this Echo first", 409)
    return em_http.ok({"token": reply["token"], "clientId": reply.get("clientId")})


@auth.require_admin
async def _delete_device(request: web.Request) -> web.Response:
    """DELETE /api/devices/{id} — remove from registry."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.delete_device, device_id)
    # Row gone → reconcile tears down any BT proxy listener/mDNS for it.
    await em_ble_proxy.reconcile(device_id)
    # The satellite needs the same, and had no equivalent: it survives an
    # ordinary disconnect on purpose, so a delete used to leave it in
    # `_servers` holding the old port for a re-added device to inherit
    # silently.
    await em_esphome.device_deleted(device_id)
    # Free the device's cached OWW models (#512), or a deleted device keeps its
    # models — and their ONNX sessions — for the life of the process.
    em_state.forget_oww_models(device_id)
    # A re-added device is the one whose payloads are least likely to be
    # right, so it must not inherit the deleted row's debounce and skip its
    # first reconcile — the bounce below has it redialling within seconds.
    em_reconcile.forget(device_id)
    # A refusal belongs to the deleted row; the device comes back as pending.
    clear_link_refused(device_id)
    em_pairing.forget(device_id)
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
    await em_events.push({"type": "device_deleted", "device_id": device_id})
    return em_http.ok({})


async def _disconnect_device(device_id: str) -> None:
    """
    Close a device's control plane, and any shell session riding on it.

    Only the control plane is closed: the device's own loop cancels its data
    client when control drops (`control.go` Run) and re-establishes both on
    the next dial, so closing `/data` here would only race that. The shell
    plane is separate and demand-opened, so an open session would otherwise
    hang against a device that is about to redial. `em_shell.release_ws` is
    called even with no programmatic session registered, because the
    `shell_close` it sends is the only thing that ends an INTERACTIVE
    dashboard session — those deliberately do not set `em_shell.sockets`.
    """
    live = em_state.devices.get(device_id)
    if live is None:
        return
    await em_shell.release_ws(device_id, live)
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
    body   = await em_http.json_body(request)
    label  = _require_label(body)
    await _require_unique_label(label, device_id)
    config = body.get("config")  # optional

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    if row["approved"]:
        return em_http.error("already_approved", "Device is already approved", 409)

    await loop.run_in_executor(None, db.approve_device, device_id, label, config)
    # Approval is the one human decision: it also issues link credentials when
    # the device next connects without them (em_pairing).
    em_pairing.approve(device_id)
    await em_events.push({"type": "device_approved", "device_id": device_id,
                       "label": label})
    return em_http.ok({"device_id": device_id, "label": label})


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
    locally until the classifier is actually on it — see em_oww_sync.hold_back_model.
    """
    effective = em_config_types.well_typed(device_id, effective)
    effective, pending_model = em_oww_sync.hold_back_model(live, effective)
    await live.send_control({"type": "config", **effective})
    if "owwThreshold" in effective:
        live.oww_threshold = float(effective["owwThreshold"])
    if "owwModel" in effective:
        live.oww_model = effective["owwModel"]
        # Refresh HA's wake-word dropdown.
        await em_esphome.update_oww_model(device_id, effective["owwModel"])
    if pending_model:
        # The device is still on its previous wake word, still scoring
        # locally, still answering. Install, then switch.
        em_tasks.spawn(em_oww_sync.install_then_switch(device_id, pending_model))
    if "owwSpeexNs" in effective:
        live.oww_speex_ns = bool(effective["owwSpeexNs"])
    if "nsAsr" in effective:
        live.ns_asr = bool(effective["nsAsr"])
    if "saveUtterances" in effective:
        live.save_utterances = bool(effective["saveUtterances"])
    if "wakeClipCapture" in effective:
        live.wake_clip_capture = bool(effective["wakeClipCapture"])
        if not live.wake_clip_capture:
            live.wake_capture.reset()
    if "wakeClipMinScore" in effective:
        try:
            live.wake_clip_min_score = max(0.05, min(0.95, float(effective["wakeClipMinScore"])))
        except (TypeError, ValueError):
            live.wake_clip_min_score = 0.20
    if "streamReply" in effective:
        live.stream_reply = bool(effective["streamReply"])
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
    # Consumed controller-side ONLY on the controller-detected wake path
    # (#120): a device that detects its own wake plays the cue itself and
    # never consults this. Mirrored anyway, because the path that does need
    # it is the one where the setting would otherwise silently do nothing.
    if "wakeSound" in effective:
        live.wake_sound = bool(effective["wakeSound"])
    if "micGainDb" in effective:
        live.mic_gain_db = float(effective["micGainDb"])
    # What a wake's level reading still contains; see em_arbiter.pick.
    if "adcMicpga" in effective:
        live.adc_gains = (int(effective["adcMicpga"]), live.adc_gains[1])
    if "adcDigitalGain" in effective:
        live.adc_gains = (live.adc_gains[0], int(effective["adcDigitalGain"]))
    if "owwOnDevice" in effective:
        # Resolved against the CAPABILITY, not taken at face value: "on"
        # against firmware that cannot trigger would stop this controller
        # acting on its own detections while waiting for wakes the device has
        # no code to send, leaving it deaf. em_shadow.effective_mode degrades
        # that to shadow.
        new_oww_on_device = em_shadow.effective_mode(
            effective["owwOnDevice"], live.oww_trigger_capable,
        )
        if new_oww_on_device != live.oww_on_device:
            # #696 review: a buffered pre-roll from the old mode must not
            # carry into the new one.
            live.wake_capture.reset()
        live.oww_on_device = new_oww_on_device
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
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    config = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    sections = await loop.run_in_executor(None, db.get_device_config_sections, device_id)
    return em_http.ok({
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
    body = await em_http.json_body(request)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

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
            return em_http.error("bad_request", "config_sections must be a list", 400)
        unknown = [s for s in sections_body if s not in sections_mod.SECTIONS]
        if unknown:
            return em_http.error(
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
        return em_http.error(
            "would_drop_keys",
            f"This body would delete {len(dropped)} existing setting(s): "
            f"{', '.join(dropped)}. Config POSTs replace rather than "
            f"merge — send the full config (read-modify-write), or pass "
            f"replace=true if the deletion is intended.",
            409,
        )

    # Validated BEFORE anything is written: a refusal must leave the stored
    # config untouched, not half-applied with the bad key rejected later.
    if (err := _validate_console_timeout(body)):
        return em_http.error("bad_console_timeout", err, 400)
    # Judged against what the device has in force now, so re-sending a value
    # it already has is never refused (em_config_types.problems).
    before = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if (bad := em_config_types.problems(
            {k: v for k, v in body.items() if k in in_scope}, before)):
        return em_http.error("bad_config_value", "; ".join(bad), 400)

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
    live = em_state.devices.get(device_id)
    if live is not None:
        await _apply_live_config(device_id, live, config)
        log.info(f"[api] Config pushed to live device: {device_id}")
        pushed = True

    # BT proxy lifecycle follows bleProxyEnabled in the *effective* config —
    # reconcile unconditionally (idempotent): re-scoping a section changes the
    # effective value without the key appearing in the body.
    await em_ble_proxy.reconcile(device_id)

    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"config": config,
                                 "config_sections": new_sections,
                                 "use_global_config": not new_sections}})
    return em_http.ok({"device_id": device_id, "config": config,
                "config_sections": new_sections,
                "use_global_config": not new_sections, "pushed": pushed})


@auth.require_admin
async def _post_device_wifi(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/wifi — switch the device to a new WiFi network.

    Body: {"ssid": "...", "ssid_hex": "...", "psk": "..."} — ssid_hex is
    optional and names the exact SSID bytes from a scan; empty/absent psk =
    open network. Rules in em_wifi.

    Returns 202 immediately: the device owns the whole switch (associate →
    DHCP → reconnect gates, auto-rollback on any failure — see the device's
    internal/wifi package). The outcome arrives asynchronously as a
    wifi_result control message and is surfaced via the device_update
    event / the "wifi" field on the device object.
    """
    device_id = request.match_info["id"]
    body = await em_http.json_body(request)
    ssid = em_http.require_str(body, "ssid")
    ssid_hex = str(body.get("ssid_hex") or "")
    psk  = str(body.get("psk") or "")

    # Mirror the device's own validation so obvious mistakes fail fast
    # with a readable message instead of a full switch/rollback cycle.
    try:
        why = em_wifi.problem(em_wifi.ssid_bytes(ssid, ssid_hex), psk)
    except ValueError as e:
        why = str(e)
    if why:
        return em_http.error("invalid_credentials", why, 400)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    st = wifi_state(device_id)
    if st["pending"]:
        return em_http.error("wifi_change_in_progress",
                      f"A change to \"{st['pending']['ssid']}\" is already "
                      f"in progress", 409)

    st["pending"] = {"ssid": ssid, "started_at": time.time()}
    st["last_result"] = None
    # ssid_hex rides alongside the name. Firmware that predates it ignores
    # the field and uses the name, which is its behaviour today.
    change = {"type": "wifi_change", "ssid": ssid, "psk": psk}
    if ssid_hex:
        change["ssid_hex"] = ssid_hex
    await live.send_control(change)
    em_dbwriter.submit(db.log_device, device_id, "info", "controller",
                       f'WiFi change to "{ssid}" requested')
    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"wifi": st}})
    return em_http.ok({"device_id": device_id, "ssid": ssid, "status": "switching"},
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
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)
    if getattr(live, "wifi_scan_future", None) is not None:
        return em_http.error("scan_in_progress", "A scan is already running", 409)

    fut = asyncio.get_event_loop().create_future()
    live.wifi_scan_future = fut
    try:
        await live.send_control({"type": "wifi_scan"})
        msg = await asyncio.wait_for(fut, timeout=20)
    except asyncio.TimeoutError:
        return em_http.error("scan_timeout",
                      "Device did not return scan results within 20s "
                      "(old firmware without WiFi support?)", 504)
    finally:
        live.wifi_scan_future = None
    if msg.get("error"):
        return em_http.error("scan_failed", msg["error"], 502)
    return em_http.ok({"networks": msg.get("networks") or []})


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
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    try:
        limit = int(request.rel_url.query.get("limit", "100"))
    except ValueError:
        return em_http.error("invalid_param", "limit must be an integer", 400)

    before_param = request.rel_url.query.get("before")
    before_ts = None
    if before_param:
        try:
            before_ts = int(before_param)
        except ValueError:
            return em_http.error("invalid_param", "before must be a unix ms timestamp", 400)

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
    return em_http.ok(entries)


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
        # PEEKED, not popped. Every refusal below leaves the token usable, so
        # re-deciding — or forcing — costs nothing. Popping first meant an
        # 11MB re-upload to get past a check that had just told the operator
        # something useful.
        binary_override = _pending_uploads.get(upload_token)
        if binary_override is None:
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        _embedded = em_ota.extract_binary_version(binary_override)
        _ver      = _embedded or f"local-{time.strftime('%Y%m%d-%H%M')}"
        release   = {"version": _ver, "url": None}
    else:
        release = await em_releases.cached_firmware()
        if release is None:
            return em_http.error("no_release", "No release information available", 409)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    if (device_id in em_ota.in_progress or device_id in em_ota.queued
            or em_emos_ota.busy(device_id)):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    # Installing what the device is already running costs a transfer, a
    # reboot and a slot — and the fleet endpoint has always skipped it for
    # releases (`already_current`), while this endpoint checked nothing and
    # the upload path could not check anything, because it labelled every
    # uploaded binary `local-<timestamp>` instead of reading the version out
    # of it. So the case most likely to happen by accident was the one case
    # nothing guarded: an engineering build uploaded by hand, twice.
    #
    # Refused rather than silently skipped, because someone pressed a button
    # here and a no-op reported as success is how they press it again.
    # `force` exists because re-flashing the SAME version is a real repair —
    # a corrupt slot is fixed by writing it again — so this must never become
    # a wall between an operator and their own device.
    if (release["version"] and row["firmware_ver"] == release["version"]
            and not body.get("force")):
        return em_http.error(
            "already_running",
            f"This device is already running {release['version']}. "
            f"Pass force to install it again.",
            409,
        )

    if upload_token:
        _pending_uploads.pop(upload_token, None)
    em_tasks.spawn(em_ota.run_update(device_id, release, binary_override))
    return em_http.ok({"status": "started", "version": release["version"]}, status=202)


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
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    if not row["firmware_previous"]:
        return em_http.error("no_rollback_available",
                      "No previous version recorded — cannot roll back", 404)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    if (device_id in em_ota.in_progress or device_id in em_ota.queued
            or em_emos_ota.busy(device_id)):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    em_tasks.spawn(em_ota.run_rollback(device_id, row["firmware_previous"]))
    return em_http.ok({"status": "started", "rolling_back_to": row["firmware_previous"]}, status=202)


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
            return em_http.error("invalid_upload", "Expected multipart field 'binary'", 400)
        binary = await field.read()
        if not binary:
            return em_http.error("empty_upload", "Uploaded binary is empty", 400)
        if len(binary) > UPLOAD_MAX_BYTES:
            return em_http.error(
                "too_large",
                f"Binary is {len(binary) / 1024 / 1024:.1f} MB, over the "
                f"{UPLOAD_MAX_BYTES // 1024 // 1024} MB limit",
                413,
            )

        token = str(_uuid.uuid4())
        _pending_uploads[token] = binary
        version = em_ota.extract_binary_version(binary)
        log.info(f"[api] Binary uploaded: {len(binary):,} bytes "
                 f"version={version or 'unknown'} token={token[:8]}…")

        async def _expire():
            await asyncio.sleep(600)
            _pending_uploads.pop(token, None)
        em_tasks.spawn(_expire())

        # The version rides back so the dashboard can say what was uploaded,
        # and warn against a device already running it BEFORE the operator
        # commits. The update endpoint refuses it either way; this is what
        # makes the refusal something you see coming instead of something you
        # find out afterwards. None means the binary carries no recognisable
        # version — shown as unknown rather than guessed at.
        return em_http.ok({"upload_token": token, "size": len(binary),
                    "version": version})
    except web.HTTPException:
        # aiohttp's own — HTTPRequestEntityTooLarge above all, raised by the
        # transport before this handler sees a byte. Swallowing it into a 500
        # is how a 1 MB transport limit presented as "an internal error
        # occurred" instead of naming a size, and the middleware already
        # re-raises these deliberately.
        raise
    except Exception as e:
        log.error(f"[api] Upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)


# ─── emOS update (#573) ───────────────────────────────────────────────────────
#
# Same shape as the firmware update above — per device, queued behind
# `em_ota.queue`, verified before anything is written, watched afterwards — but
# what it writes is the boot partition. em_emos_update holds every decision
# and its docstring is the design; this section only carries.


@auth.require_admin
async def _post_emos_upload(request: web.Request) -> web.Response:
    """
    POST /api/emos/upload (multipart: field "payload", an emos-payload.zip)

    The emOS counterpart of /api/releases/upload: a locally built payload
    bundle (emos/tools/make-payload-bundle.py), so an init can be tried on a
    bench device before it is tagged. Returns an upload_token for
    /api/devices/{id}/emos_update, valid for 10 minutes.
    """
    import uuid as _uuid
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "payload":
            return em_http.error("invalid_upload",
                          "Expected multipart field 'payload'", 400)
        raw = await field.read()
    except web.HTTPException:
        raise
    except Exception as e:
        log.error(f"[api] emOS payload upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)
    if not raw:
        return em_http.error("empty_upload", "Uploaded payload is empty", 400)
    if len(raw) > UPLOAD_MAX_BYTES:
        return em_http.error("too_large", "Payload is over the upload limit", 413)
    try:
        payload = await asyncio.get_event_loop().run_in_executor(
            None, em_emos_build.read_payload_bundle, raw)
    except em_emos_build.BuildError as e:
        return em_http.error("bad_payload", f"That is not a usable emOS payload: {e}", 400)

    version = payload["version"] or f"local-{time.strftime('%Y%m%d-%H%M')}"
    token = str(_uuid.uuid4())
    _pending_emos_uploads[token] = (payload["files"], version)
    log.info(f"[api] emOS payload uploaded: {len(raw):,} bytes "
             f"version={version} files={sorted(payload['files'])}")

    async def _expire():
        await asyncio.sleep(600)
        _pending_emos_uploads.pop(token, None)
    em_tasks.spawn(_expire())
    return em_http.ok({"upload_token": token, "version": version,
                "files": sorted(payload["files"])})


@auth.require_admin
async def _post_device_emos_update(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/emos_update

    Rebuild the device's emOS image around the latest release's init and flash
    it. Optional JSON body: {"upload_token": "..."} to use an uploaded payload
    instead, {"force": true} to install a release the device is not behind.

    Returns 202 — the update runs in the background.
    """
    device_id = request.match_info["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)
    # Refused here as well as greyed out in the panel: this is a plain POST.
    if live.android_userspace:
        return em_http.error("not_emos", "This device is not running emOS", 409)
    if (em_emos_ota.busy(device_id) or device_id in em_ota.in_progress
            or device_id in em_ota.queued):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    override = None
    token = body.get("upload_token")
    if token:
        override = _pending_emos_uploads.get(token)
        if override is None:
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        version = override[1]
    else:
        release = await em_releases.fetch_emos()
        if release is None:
            return em_http.error("no_release", "No emOS release information available", 409)
        version = release["version"]
        if not em_emos_update.can_target(version):
            return em_http.error(
                "release_too_old",
                f"emOS {version} predates updating over the network: its init "
                f"cannot roll a failed update back by itself.", 409)
        if (not em_emos_update.update_available(row["emos_version"], version)
                and not body.get("force")):
            return em_http.error(
                "already_running",
                f"This device runs emOS {row['emos_version'] or 'unknown'}; "
                f"{version} is not an update for it. Pass force to install "
                f"it anyway.", 409)

    if token:
        _pending_emos_uploads.pop(token, None)
    em_tasks.spawn(em_emos_ota.run_update(device_id, override))
    return em_http.ok({"status": "started", "version": version}, status=202)


# ─── Shell WebSocket proxy (interactive dashboard terminal) ───────────────────

async def _ws_shell(request: web.Request) -> web.WebSocketResponse:
    """
    WS /api/devices/{id}/shell — interactive shell terminal for dashboard.

    Auth is handled via ws_resolve_session (checks cookie then ?token= query
    param) because browser WebSocket clients cannot set custom headers.
    Do NOT add @auth.require_admin here — _extract_token doesn't read query
    params and would reject every connection before this function runs.

    Sets em_state.shell_dashboard so handle_shell proxies in interactive mode.
    """
    device_id = request.match_info["id"]

    user = await auth.ws_resolve_session(request)
    if user is None:
        raise web.HTTPUnauthorized()
    if user["role"] != "admin":
        raise web.HTTPForbidden()

    live = em_state.devices.get(device_id)
    if live is None:
        raise web.HTTPConflict(reason="Device is not connected")

    # Refuse if a programmatic shell session (e.g. OTA transfer) is in progress.
    # Opening a terminal mid-transfer sends shell_open to the device, which cancels
    # the current shell context and kills the transfer.
    lock = em_shell.locks.get(device_id)
    if lock and lock.locked():
        raise web.HTTPConflict(reason="Device shell is busy — an OTA update is in progress")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    log.info(f"[api] Shell session requested: {device_id} by {user['username']}")
    await em_events.push_log(device_id, "info", "controller",
                          f"Shell session opened by {user['username']}")

    loop = asyncio.get_event_loop()
    done_future = loop.create_future()
    em_state.shell_pending[device_id]   = done_future
    em_state.shell_dashboard[device_id] = ws
    # Do NOT set em_shell.sockets or acquire em_shell.locks — interactive sessions
    # bypass the programmatic shell mechanism entirely.

    try:
        # pty:true — interactive terminal wants a real PTY (mksh prompt,
        # line editing, top/vi, resize). Old firmware ignores the field and
        # opens the legacy pipe; handle_shell reports the established mode
        # to the dashboard via shell_meta. Programmatic sessions
        # (em_shell.get_ws) deliberately do not set it.
        await live.send_control({"type": "shell_open", "pty": True})
        await done_future
    except Exception as e:
        log.warning(f"[api] Shell session error ({device_id}): {e}")
    finally:
        em_state.shell_pending.pop(device_id, None)
        em_state.shell_dashboard.pop(device_id, None)
        await live.send_control({"type": "shell_close"})
        log.info(f"[api] Shell session closed: {device_id}")
        await em_events.push_log(device_id, "info", "controller",
                              f"Shell session closed by {user['username']}")

    return ws


# ─── Releases ─────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_latest_release(request: web.Request) -> web.Response:
    """GET /api/releases/latest — latest GitHub release, from cache."""
    release = await em_releases.cached_firmware()
    if release is None:
        return em_http.error("no_release", "No release information available", 404)
    return em_http.ok(release)


@auth.require_admin
async def _post_check_release(request: web.Request) -> web.Response:
    """POST /api/releases/check — force re-poll GitHub."""
    release = await em_releases.fetch_firmware(force=True)
    # Same button, same question: the Updates tab shows emOS beside firmware.
    await em_releases.fetch_emos()
    if release is None:
        return em_http.error("no_release", "Could not fetch release from GitHub", 502)
    return em_http.ok(release)


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
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        # Read the version OUT of the binary, as the single-device path does.
        # Labelling it `local-<timestamp>` made every uploaded binary look
        # like a version no device had ever run, so the already_current skip
        # below could never fire for an upload and the whole fleet re-flashed
        # what it was already running. The timestamp remains the fallback for
        # a binary carrying no recognisable version.
        _embedded = em_ota.extract_binary_version(binary_override)
        release = {
            "version": _embedded or f"local-{time.strftime('%Y%m%d-%H%M')}",
            "url": None,
        }
    else:
        release = await em_releases.cached_firmware()
        if release is None:
            return em_http.error("no_release", "No release information available", 409)

    started = []
    skipped = []
    loop = asyncio.get_event_loop()

    for device_id, live in list(em_state.devices.items()):
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row is None or not row["approved"]:
            skipped.append({"device_id": device_id, "reason": "not_approved"})
            continue
        # No longer `not upload_token and …`: an uploaded binary now carries a
        # real version, so this skip finally covers the fleet-wide re-flash of
        # an engineering build too. `force` overrides it for the repair case,
        # matching the single-device endpoint.
        if (release["version"] and row["firmware_ver"] == release["version"]
                and not body.get("force")):
            skipped.append({"device_id": device_id, "reason": "already_current"})
            continue
        if device_id in em_ota.in_progress or device_id in em_ota.queued:
            skipped.append({"device_id": device_id, "reason": "update_in_progress"})
            continue

        em_tasks.spawn(em_ota.run_update(device_id, release, binary_override))
        started.append(device_id)

    return em_http.ok({
        "version": release["version"],
        "started": started,
        "skipped": skipped,
    }, status=202)


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
        return em_http.error("tls_unavailable",
                      "Device-link TLS is not active on this controller", 503)
    body      = await em_http.json_body(request)
    device_id = em_http.require_str(body, "device_id")

    loop  = asyncio.get_event_loop()
    token = await loop.run_in_executor(None, db.ensure_device_token, device_id)
    return em_http.ok({
        "ca_pem": em_pki.ca_pem(_tls_dir),
        "token":  token,
        "dir":    DEVICE_TLS_DIR,
    })


@auth.require_admin
async def _get_provision_controller_endpoints(request: web.Request) -> web.Response:
    """
    GET /api/provision/controller_endpoints

    The fleet's controller.json for the wizard to write over adb, or null
    when the list is empty — the wizard then removes whatever file is there.
    At provisioning this controller is the source of truth; the fleet sync is
    more careful (em_payloads.sync_endpoints).
    """
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, em_payloads.endpoints_file)
    return em_http.ok({
        "content":     data.decode("ascii") if data else None,
        "md5":         em_endpoints.md5(data) if data else None,
        "path":        em_endpoints.DEVICE_PATH,
    })


@auth.require_admin
async def _post_pair(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/pair — approve a pairing request. ADMIN ONLY.

    Only answers a request the device made (its owner held the action button),
    so a click cannot hand credentials to whatever happens to hold a device's
    connection. A connected device is issued them now; one that could not
    connect is issued them when it next dials within its window.
    """
    if _tls_dir is None:
        return em_http.error("tls_unavailable",
                      "Device-link TLS is not active on this controller", 503)
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    # Firmware without `pairing` cannot ask, so for a connected device whose
    # link is plain the admin's click is the request. It goes away as devices
    # update; firmware that CAN ask must, so the button on the device is
    # always part of it there.
    can_ask = live is None or getattr(live, "pairing_capable", False)
    admin_started = (not can_ask) and not getattr(live, "secure", False)
    if em_pairing.pending_request(device_id) is None and not admin_started:
        return em_http.error("no_pair_request",
                      "This Echo has not asked to pair. Hold its action button "
                      "for 5 seconds, then approve it here.", 409)
    em_pairing.approve(device_id)
    if live is not None:
        em_tasks.spawn(_issue_credentials(device_id))
    log.info(f"[api] {request['user']['username']} approved pairing for {device_id}")
    return em_http.ok({"approved": True, "connected": live is not None})


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
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", f"Device not connected: {device_id}", 409)

    # Refused server-side, not merely greyed out in the dashboard. This is a
    # plain POST with a session token, so a dashboard-only rule protects
    # nothing from anyone who opens the network tab — the same reasoning that
    # puts the recordings check on the server. And the cost of running it
    # anyway is not a no-op: the payload targets Magisk's /sbin/.core overlay,
    # which a device without Magisk has no daemon to create, so the write
    # fails, TRANSFER_OK never comes, and the transfer holds that device's
    # shell lock for its full timeout.
    if not live.android_userspace:
        return em_http.error(
            "not_android",
            "This device is not running Android, so there is nothing to "
            "debloat — the payload is a package list and a Magisk boot script.",
            409)

    # No explicit shell release here: em_shell.run and em_shell.stream_file each
    # acquire and release the session in their own finally, which is why
    # em_payloads.sync_start_script does not either. Releasing it from out here could close
    # a session a concurrent caller had opened.
    em_tasks.spawn(em_payloads.sync_debloat(live, device_id))
    return em_http.ok({"started": True})


async def _issue_credentials(device_id: str) -> None:
    """
    Background task: give a live, approved device a fresh token and the CA.

    Called only behind an admin approval (em_pairing): this writes the token
    to whatever holds the device's connection, so it must never run for a
    connection nobody vouched for. The token is ROTATED, not reused, so a token
    that may have leaked before is dead the moment this runs, and the new one
    starts unconfirmed.
    """
    loop = asyncio.get_event_loop()
    live = em_state.devices.get(device_id)
    if live is None or _tls_dir is None:
        return
    try:
        await em_events.push_log(device_id, "info", "controller",
                              "Pairing: installing link credentials")
        # Minted here and stored only once the device has it. The push rides
        # the shell plane, which the device dials with its CURRENT token, so
        # storing the new one first refused the push on any device that was
        # connected over wss (found on 15LE, 2026-09-26).
        token = secrets.token_urlsafe(32)
        ca    = em_pki.ca_pem(_tls_dir)

        await em_shell.run(live, f"mkdir -p {DEVICE_TLS_DIR}")
        await asyncio.sleep(1.0)  # let the shell session close cleanly

        ok = await em_shell.stream_file(
            live, ca.encode("ascii"), f"{DEVICE_TLS_DIR}/ca.pem", mode="644")
        if ok:
            await asyncio.sleep(1.0)
            ok = await em_shell.stream_file(
                live, token.encode("ascii"), f"{DEVICE_TLS_DIR}/token", mode="600")
        if not ok:
            await em_events.push_log(device_id, "error", "controller",
                                  f"Pairing: credential transfer failed: {ok}")
            return

        await loop.run_in_executor(None, db.set_device_token, device_id, token)
        em_pairing.done(device_id)
        await em_events.push_log(
            device_id, "info", "controller",
            "Pairing: credentials installed — reconnecting over TLS")
        # The Go client reloads credentials on every dial, so a reconnect
        # is enough to move to the TLS listener.
        try:
            await live.control_ws.close()
        except Exception:
            pass
    except Exception as e:
        log.exception(f"[api] Issuing credentials failed for {device_id}: {e}")
        await em_events.push_log(device_id, "error", "controller",
                              f"Pairing failed: {e}")


# ─── System ───────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_system_status(request: web.Request) -> web.Response:
    """GET /api/system/status"""
    loop = asyncio.get_event_loop()
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    fleet_base_os, approval_mode = await loop.run_in_executor(None, lambda: (
        db.fleet_base_os(), db.get_config("device_approval", "strict")))
    release = await em_releases.cached_firmware()

    return em_http.ok({
        "controller_version": CONTROLLER_VERSION,
        # This controller's device address and ports — what a new entry in
        # the controller address list starts from.
        "link": dict(_link),
        # The mtime stamped onto the dashboard bundle's URL by
        # _serve_dashboard, so a running page can tell whether the JavaScript
        # it is executing is still the JavaScript this controller serves.
        #
        # A long-lived SPA tab survives an add-on update and keeps its old
        # bundle indefinitely — the URL is cache-busted, but only on a page
        # LOAD. Nothing in the page could notice, and `controller_version`
        # above made it worse rather than better: it is read from the server,
        # so it reports the NEW version while the page runs the OLD code.
        # Measured 2026-09-10, when a wizard run on a stale tab silently
        # skipped a provisioning step that had shipped hours earlier and the
        # header cheerfully named a version whose code was not running.
        #
        # Compared rather than displayed, so it does not matter that an mtime
        # is meaningless to a person; `version.py` cannot be used here because
        # a local build is "dev" for every build and would never differ.
        "bundle_version": _bundle_version(),
        # True when running as a Home Assistant add-on behind Supervisor's
        # ingress proxy. Presentation only — the dashboard is the same
        # dashboard either way, with the same features, and nothing should
        # be gated on this. It exists so the SPA can stop drawing chrome
        # Home Assistant already draws (its own panel header and title) and
        # can avoid offering a theme toggle that fights HA's theme.
        "ha_ingress": em_ingressauth.INGRESS_ONLY,
        # Which userspaces the fleet has reported (schema v21). Sorted for a
        # stable response; EMPTY means nothing has ever said, which is not the
        # same as "all Android" and must not be read that way — a control
        # disabled on the strength of not knowing is worse than one that is
        # merely useless on this fleet.
        "fleet_base_os": sorted(fleet_base_os),
        # Peak asyncio event-loop stall since start (ms). Non-trivial values
        # mean the controller itself delayed speaker frames and LED updates.
        "loop_lag_peak_ms": round(em_state.loop_lag_peak_ms, 1),
        "connected":      len(em_state.devices),
        "total_devices":  len(all_rows),
        "pending":        sum(1 for r in all_rows if not r["approved"]),
        "approval_mode":  approval_mode,
        # Whether the BACKGROUND poll runs (#159). With it off, "No release
        # info" and a GitHub outage are indistinguishable from the Updates
        # tab, and the tab is where someone goes to find out — so the reason
        # for the blank has to be visible there or it reads as a fault and
        # sends them debugging their network.
        #
        # Deliberately not "are updates available": "Check now" still works
        # when this is False, because a button press is a request rather than
        # background traffic. The flag says the poll is off, nothing more.
        "update_checks_enabled": em_releases.check_interval() > 0,
        "latest_release": release["version"] if release else None,
        # Controller update, surfaced alongside the firmware one so the header
        # can badge it without a second round trip. Read-only by design: the
        # controller runs as a container the user owns, and updating it is a
        # `docker compose pull` they perform — there is deliberately no action
        # here, only the information needed to decide to take it.
        "controller_update": em_releases.controller_update(),
        "updates_available": sum(
            1 for r in all_rows
            if release and _firmware_update(r["firmware_ver"], release["version"])
        ),
    })


@auth.require_admin
async def _get_system_config(request: web.Request) -> web.Response:
    """GET /api/system/config — full system_config table."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_all_config)
    # Don't expose schema_version — internal detail
    config.pop("schema_version", None)
    return em_http.ok(config)


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
    body = await em_http.json_body(request)
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
        return em_http.error(
            "unknown_config_key",
            f"Unknown or immutable config key(s): {', '.join(unknown)}",
            400,
        )
    return em_http.ok(updated)


# ─── Global device config ─────────────────────────────────────────────────────

@auth.require_auth
async def _get_global_config(request: web.Request) -> web.Response:
    """GET /api/global/config — fleet-wide default device config."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_global_device_config)
    return em_http.ok(_redact_console_pw(config))


# The console password record never leaves the controller. Hashing before
# storage is pointless if the result is then handed to every client that asks
# for the config, so reads see a sentinel and writes send it back untouched —
# see em_console_pw.for_display / resolve_write.
_CONSOLE_PW_KEY = "consolePassword"


def _redact_console_pw(config: dict) -> dict:
    if not config or _CONSOLE_PW_KEY not in config:
        return config
    out = dict(config)
    out[_CONSOLE_PW_KEY] = em_console_pw.for_display(out[_CONSOLE_PW_KEY])
    return out


def _resolve_console_pw(incoming: dict, stored: dict) -> None:
    """Turn whatever a client sent into the record to store, in place."""
    if _CONSOLE_PW_KEY not in incoming:
        return
    incoming[_CONSOLE_PW_KEY] = em_console_pw.resolve_write(
        incoming[_CONSOLE_PW_KEY], (stored or {}).get(_CONSOLE_PW_KEY)
    )


# The console idle timeout, in minutes: 0 for none, otherwise 1-90.
_CONSOLE_TMOUT_KEY = "consoleTimeoutMin"
_CONSOLE_TMOUT_MAX = 90


def _validate_console_timeout(config: dict) -> str | None:
    """
    Return an error message when the timeout is out of range, else None.

    REFUSED rather than clamped, deliberately. A value of 600 is somebody who
    meant seconds, and silently giving them ten minutes is a console that logs
    them out all day from a setting that looked accepted. The device refuses
    it too — the controller validating first means a bad value reaching the
    firmware is a bug rather than a user, but neither end assumes the other is
    the careful one.

    Absence is fine: it means the body did not mention the key.
    """
    if _CONSOLE_TMOUT_KEY not in config:
        return None
    v = config[_CONSOLE_TMOUT_KEY]
    if isinstance(v, bool) or not isinstance(v, int):
        return (f"{_CONSOLE_TMOUT_KEY} must be a whole number of minutes, "
                f"got {v!r}")
    if v < 0 or v > _CONSOLE_TMOUT_MAX:
        return (f"{_CONSOLE_TMOUT_KEY} must be 0 (no timeout) or 1-"
                f"{_CONSOLE_TMOUT_MAX} minutes, got {v}")
    return None


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
    config = await em_http.json_body(request)
    loop = asyncio.get_event_loop()

    explicit_replace = bool(config.pop("replace", False))
    # Raw (defaults NOT underlaid): see get_global_device_config_raw — a
    # newly-added default must not look like a key this body is deleting.
    stored = await loop.run_in_executor(None, db.get_global_device_config_raw)
    _resolve_console_pw(config, stored)
    if (err := _validate_console_timeout(config)):
        return em_http.error("bad_console_timeout", err, 400)
    if (bad := em_config_types.problems(config, stored)):
        return em_http.error("bad_config_value", "; ".join(bad), 400)
    endpoints_before = stored.get("controllerEndpoints") or []
    if "controllerEndpoints" in config:
        eps, err = em_endpoints.normalise(
            config["controllerEndpoints"], _link["port"], _link["tls_port"])
        if err:
            return em_http.error("bad_controller_endpoints", f"Controller address list {err}.", 400)
        config["controllerEndpoints"] = eps
    dropped = _dropped_keys(config, stored)
    if dropped and not explicit_replace:
        return em_http.error(
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
    for device_id, live in list(em_state.devices.items()):
        effective = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        await _apply_live_config(device_id, live, effective)
        pushed.append(device_id)

    if pushed:
        log.info(f"[api] Global config pushed to {len(pushed)} device(s): {pushed}")

    if (config.get("controllerEndpoints") or []) != endpoints_before:
        # Connected devices get the file now; the rest on their next connect,
        # which must not be skipped by a reconcile stamp from before the change.
        em_reconcile.stamps.clear()
        for device_id, live in list(em_state.devices.items()):
            em_tasks.spawn(em_payloads.push_endpoints(live, device_id))

    # Reconcile BT proxies for every approved device — offline ones included
    # (proxy mDNS/port lifecycle is independent of the device connection,
    # unlike the config push above). No longer filtered on inheritance: a
    # device overriding some other section still tracks the fleet's
    # bleProxyEnabled, and reconcile is idempotent either way.
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    for row in all_rows:
        if row["approved"]:
            await em_ble_proxy.reconcile(row["device_id"])

    return em_http.ok({"config": config, "pushed_to": pushed})


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
    em_events.clients.add(ws)
    log.debug(f"[api] Events client connected ({user['username']}) "
              f"— {len(em_events.clients)} total")

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
        em_events.clients.discard(ws)
        log.debug(f"[api] Events client disconnected — "
                  f"{len(em_events.clients)} remaining")

    return ws


# ─── Controller release notice ────────────────────────────────────────────────

@auth.require_auth
async def _get_controller_release(request: web.Request) -> web.Response:
    """GET /api/releases/controller"""
    data = await em_releases.fetch_controller()
    if data is None:
        # Fall back to the DB cache so a GitHub outage does not blank the
        # panel — offline is not the same as "no update exists".
        stored = await asyncio.get_running_loop().run_in_executor(None, lambda: {
            key: db.get_config(key, "") or "" for key in (
                "latest_controller_version", "latest_controller_notes",
                "latest_controller_published_at")})
        version = stored["latest_controller_version"]
        if not version:
            return em_http.ok({"version": None, "current": CONTROLLER_VERSION,
                        "status": "unknown", "available": False})
        data = {
            "version":      version,
            "current":      CONTROLLER_VERSION,
            "notes":        stored["latest_controller_notes"],
            "published_at": stored["latest_controller_published_at"],
            "release_url":  "",
            **_compare_versions(CONTROLLER_VERSION, version),
        }
    return em_http.ok(data)


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
    stats: dict[str, Any] = {"loop_lag_peak_ms": round(em_state.loop_lag_peak_ms, 1)}

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

    health = await loop.run_in_executor(None, db.latest_health)
    for row in rows:
        did = row["device_id"]
        device_configs[did] = await loop.run_in_executor(
            None, db.get_effective_device_config, did)
        live = em_state.devices.get(did)
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
            # Latest boot's reason and eMMC wear (schema v28): hardware
            # facts, nothing about the owner.
            "boot":         em_support.redact_boot(health.get(did)),
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


# ─── Periodic background tasks ────────────────────────────────────────────────

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
    live = em_state.devices.get(device_id)
    if live is None:
        return
    out = await em_shell.run(live, f"busybox tail -c 4096 {SUPERVISOR_LOG}", timeout=30.0)
    text = (out or "").strip()
    if not text:
        await em_events.push_log(device_id, "warn", "controller",
            "Update failed, and the device has no supervisor log — firmware "
            "predating it, or start_server.sh has not been synced yet "
            "(it takes effect on the next device reboot).")
        return
    await em_events.push_log(device_id, "warn", "controller",
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
    await em_events.push(event)

    # Owed an explanation from a failed update? Collect it now the device is
    # reachable again. Removed from the set on the way in, so a flapping
    # device cannot queue repeated fetches, and scheduled rather than awaited
    # so a slow shell never delays the connect path.
    if device_id in em_ota.supervisor_log_wanted:
        em_ota.supervisor_log_wanted.discard(device_id)

        async def _collect_soon(_id=device_id):
            # The device has just registered; give its shell plane a moment
            # before demanding a session on it.
            await asyncio.sleep(3.0)
            try:
                await _collect_supervisor_log(_id)
            except Exception as e:
                log.warning(f"[api] supervisor log fetch failed for {_id}: {e}")

        em_tasks.spawn(_collect_soon())


async def notify_device_disconnected(device_id: str) -> None:
    """Called by em_controller when a device disconnects."""
    await em_events.push({"type": "device_disconnected", "device_id": device_id})


async def notify_pair_request(device_id: str, via: str) -> None:
    """A device asked to pair; the dashboard offers Approve pairing."""
    if em_pairing.request(device_id, via):
        log.info(f"[api] {device_id} asked to pair ({via})")
        await em_events.push_log(device_id, "info", "controller",
                              "Asked to pair — approve it in the dashboard")
        await em_events.push({"type": "device_pair_request", "device_id": device_id})


async def notify_device_pending(device_id: str, ip: str) -> None:
    """Called by em_controller when an unapproved device attempts connection."""
    await em_events.push({
        "type":      "device_pending",
        "device_id": device_id,
        "ip":        ip,
    })


# ─── Label checks ─────────────────────────────────────────────────────────────

def _require_label(body: dict) -> str:
    """The body's label, or a 400 naming the rule it broke (em_labels)."""
    label, err = em_labels.check_label(body.get("label"))
    if err:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": err, "code": "invalid_label"}),
        )
    return label


async def _require_unique_label(label: str, device_id: str) -> None:
    """A 409 if another Echo already has this name (em_labels.duplicate_of).

    A hard refusal rather than a warning: two Echos with one name cannot be
    told apart in the dashboard, and Home Assistant quietly suffixes the
    second one's entity_ids with _2.
    """
    rows = await asyncio.get_event_loop().run_in_executor(None, db.get_all_devices)
    other = em_labels.duplicate_of(label, ((r["device_id"], r["label"]) for r in rows), device_id)
    if other is not None:
        raise web.HTTPConflict(
            content_type="application/json",
            body=json.dumps({"error": f'Another Echo is already called "{other}".',
                             "code": "duplicate_label"}),
        )


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


# device_id -> {"reason", "at"}: the last link refusal for a device on record,
# until it next registers. In memory on purpose: a refused device retries every
# few seconds, so a restarted controller has it back within one retry.
_link_refusals: dict[str, dict] = {}


def note_link_refused(device_id: str, reason: str) -> None:
    _link_refusals[device_id] = {"reason": reason, "at": time.time()}


def clear_link_refused(device_id: str) -> None:
    _link_refusals.pop(device_id, None)


def _health_json(boot: dict | None) -> dict | None:
    if not boot:
        return None
    return {
        "emmc":       em_health.emmc_summary(boot),
        "bootReason": em_health.boot_summary(boot.get("boot_reason")),
        "bootAt":     boot.get("boot_at"),
        "wearDay":    boot.get("day"),
        "emmcPart":   " ".join(x for x in (boot.get("emmc_name"), boot.get("emmc_date")) if x) or None,
    }


def _merge_device(row, boot: dict | None = None) -> dict:
    """
    Merge a DB device row with live in-memory state.

    DB row provides persistent fields (label, config, firmware_ver etc).
    Live em_state.devices dict provides transient state (connected, speaking,
    muted, listening, thinking).
    """
    device_id = row["device_id"]
    live = em_state.devices.get(device_id)
    return {
        # Persistent
        "device_id":          device_id,
        "label":              row["label"],
        "approved":           bool(row["approved"]),
        "ip":                 row["ip"],
        "firmware_ver":       row["firmware_ver"],
        "firmware_previous":  row["firmware_previous"],
        # Whether the offered release is newer than what this device runs,
        # by version rather than string (EA firmware on a GA controller is
        # ahead of the GA release, not behind it).
        "firmware_update":    _firmware_update(row["firmware_ver"],
                                               em_releases.firmware_version()),
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
        # Stored, not live (#286): an offline device reports HA's last choice.
        "wake_word":        db.get_wake_word_enabled(device_id),
        "listening":        getattr(live, "listening", False) if live else False,
        "thinking":         getattr(live, "thinking",  False) if live else False,
        "stats":            live.stats if live else None,
        # Control-plane round trip, controller-measured. The RF counters are
        # structurally zero on this hardware (the MTK driver populates
        # neither retries nor noise), so this is the only latency signal.
        "rttMs":            getattr(live, "rtt_last_ms", None) if live else None,
        # Link quality graded on packet loss from TCP's own retransmit
        # counters (em_tcp.MinuteStrip): a verdict over the last 10 minutes
        # and loss per minute for the last 30. Loss, not signal strength, is
        # what a user hears. None until measured, and when offline.
        "linkQuality":      (live.tcp_minutes.summary(time.time())
                             if live and getattr(live, "tcp_minutes", None) else None),
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
        # Why the controller is turning this device away, while it is.
        "linkRefused":      _link_refusals.get(device_id),
        # The device's owner held its button and it is waiting for Approve.
        "pairRequest":      em_pairing.pending_request(device_id),
        # Q4 fix (2026-07-05 review): near-miss counter — same lifecycle as
        # the rest of this "Live" section (resets on reconnect, since it
        # lives on the per-connection Device object, not the DB row).
        # None for a privately listening Echo: this controller scores nothing
        # from it, and 0 would read as "measured, and none".
        "owwNearMisses":    (None if live and getattr(live, "private_listening", False)
                             else getattr(live, "oww_near_misses", 0) if live else 0),
        # What this firmware can be asked to do, by capability rather than by
        # version comparison. Drives whether the dashboard OFFERS on-device
        # scoring: a toggle that silently does nothing on old firmware is worse
        # than no toggle, because it looks like the feature is broken.
        "owwShadowCapable": getattr(live, "oww_shadow_capable", False) if live else False,
        # Separate from shadow: firmware in the field scores and reports
        # without being able to act on it, and offering those "on" produces a
        # device that never answers.
        "owwTriggerCapable": getattr(live, "oww_trigger_capable", False) if live else False,
        # Firmware that asks to pair itself (the owner holds the button); for
        # older firmware on a plain link the dashboard offers Pair instead.
        "pairingCapable":   getattr(live, "pairing_capable", False) if live else False,
        # What this Echo is actually doing with its microphone
        # (docs/listening.md, em_listen.resolve) — the one source for every
        # privacy statement the dashboard makes. `streams` is true, false, or
        # null for not known yet, which must never be shown as private. Null
        # for an offline device: it is not sending anything, and what it will
        # do on reconnect is its own report to make.
        "owwLocalCapable": getattr(live, "oww_local_capable", False) if live else False,
        "listen":          _listen_json(live) if live else None,
        "wakeCueCapable": getattr(live, "wake_cue_capable", False) if live else False,
        "volumeCueCapable": getattr(live, "volume_cue_capable", False) if live else False,
        "wakeMicCapable": getattr(live, "wake_mic_capable", False) if live else False,
        "remoteVolumeArcCapable": getattr(live, "remote_volume_arc_capable", False) if live else False,
        "responseLevelCapable": getattr(live, "response_level_capable", False) if live else False,
        # Sendspin player (#89): whether the firmware has one, and its status
        # (no secrets; the pairing token is its own request).
        "sendspinCapable": getattr(live, "sendspin_capable", False) if live else False,
        "bleConnectCapable": getattr(live, "ble_connect_capable", False) if live else False,
        "sendspin":        getattr(live, "sendspin", None) if live else None,
        "audioMixCapable": getattr(live, "audio_mix_capable", False) if live else False,
        # Gates the AEC delay slider, which only means anything on the
        # software tap. Paired with aecRef because the capability says the
        # firmware KNOWS how to use a hardware reference and aecRef says
        # whether this board turned out to have one — a device can be
        # capable and still be running on the software tap.
        "aecHwRefCapable": getattr(live, "aec_hw_ref_capable", False) if live else False,
        "aecRef":          getattr(live, "aec_ref", None) if live else None,
        # Which userspace the device booted: "emos", "fireos", or null from
        # firmware that cannot say. Null is not FireOS — the wizard, the
        # support bundle and the payload reconcile all need to tell "Android"
        # apart from "not asked". Offline it falls back to the value stored at
        # its last registration (schema v21), so the dashboard's slug does not
        # vanish when a device does; a live report always wins.
        "baseOs":          (getattr(live, "base_os", None) if live else None)
                           or row["base_os"],
        # `uname -m` / `uname -r` from the register message, stored value when
        # offline (schema v23). Null from firmware that does not send them.
        "kernelArch":      (getattr(live, "kernel_arch", None) if live else None)
                           or row["kernel_arch"],
        "kernelRelease":   (getattr(live, "kernel_release", None) if live else None)
                           or row["kernel_release"],
        # Flash wear and how the current boot started (schema v28, em_health):
        # the stored last boot, so it survives the device going offline. Null
        # from firmware that does not report them.
        "health":          _health_json(boot),
        # The DERIVED answer, not a second copy of the rule. em_platform owns
        # "which payloads mean anything here"; a dashboard that re-derived it
        # from baseOs would be a mirror free to disagree with the server that
        # actually refuses. Defaults True with no device, so a disconnected
        # device shows the control as it always did.
        "androidUserspace": getattr(live, "android_userspace", True) if live else True,
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
        # The emOS image it runs (schema v30), and whether the newest release
        # is an update for it — decided here so the panel and the endpoint
        # that refuses cannot disagree.
        "emosVersion":      row["emos_version"],
        "emosLatest":       em_releases.emos_latest_version() or None,
        "emosUpdateAvailable": em_emos_update.update_available(
            row["emos_version"], em_releases.emos_latest_version()),
        "emosOffer":        em_emos_update.offer(
            row["emos_version"], em_releases.emos_latest_version()),
        "emos_update_in_progress": device_id in em_emos_ota.in_progress,
        "emos_update_queued":      device_id in em_emos_ota.queued,
        "emos_update_stage":       em_emos_ota.stage.get(device_id),
        "emos_update_error":       em_emos_ota.errors.get(device_id),
        # Update state
        "update_in_progress": device_id in em_ota.in_progress,
        # Waiting on the global OTA lock — started, but nothing has been sent
        # to this device yet. Distinct from update_in_progress so the panel
        # does not report a transfer that has not begun.
        "update_queued":      device_id in em_ota.queued,
        # Last OTA/rollback failure (None when the last attempt succeeded or
        # none was made) — lets the dashboard show a terminal ✗ state instead
        # of "updating…" forever when an update aborts.
        "update_error":       em_ota.errors.get(device_id),
    }
