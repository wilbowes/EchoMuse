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
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
import websockets

import em_db as db
import em_auth as auth

log = logging.getLogger("echomuse.api")

# ─── Config ───────────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases/latest"

# How long to cache GitHub release info in memory (seconds).
# DB is the persistent cache; this avoids hitting the DB on every
# /api/releases/latest request.
_release_cache: dict = {}
_release_cache_ts: float = 0.0
RELEASE_CACHE_TTL = 60  # seconds

# Reference to the live devices dict from em_controller — set by init().
_devices: dict = {}

# Set of connected /api/events WebSocket clients.
_event_clients: set[web.WebSocketResponse] = set()

# Track in-progress OTA updates per device_id to enforce one-at-a-time.
_updates_in_progress: set[str] = set()

# ─── Initialisation ───────────────────────────────────────────────────────────

def init(devices_ref: dict) -> None:
    """
    Bind the live devices dict from em_controller.

    Must be called before create_app().
    """
    global _devices
    _devices = devices_ref


async def create_app() -> web.Application:
    """
    Build and return the aiohttp Application.

    Routes are registered here. The app is not started — the caller
    creates an AppRunner and TCPSite.
    """
    app = web.Application(middlewares=[_error_middleware])

    # Static / setup
    app.router.add_get("/",       _serve_spa)
    app.router.add_get("/setup",  _serve_spa)
    app.router.add_post("/api/setup", _post_setup)

    # Auth
    app.router.add_post("/api/auth/login",  _post_login)
    app.router.add_post("/api/auth/logout", _post_logout)
    app.router.add_get("/api/auth/me",      _get_me)

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
    app.router.add_post("/api/devices/{id}/update",       _post_device_update)
    app.router.add_post("/api/devices/{id}/rollback",     _post_device_rollback)
    app.router.add_get("/api/devices/{id}/shell",         _ws_shell)

    # Releases
    app.router.add_get("/api/releases/latest",   _get_latest_release)
    app.router.add_post("/api/releases/check",   _post_check_release)
    app.router.add_post("/api/releases/deploy",  _post_deploy_all)

    # System
    app.router.add_get("/api/system/status",    _get_system_status)
    app.router.add_get("/api/system/config",    _get_system_config)
    app.router.add_patch("/api/system/config",  _patch_system_config)

    # Live events WebSocket
    app.router.add_get("/api/events", _ws_events)

    return app


async def create_runner(devices_ref: dict) -> web.AppRunner:
    """Convenience wrapper — init + create_app + AppRunner."""
    init(devices_ref)
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
    """GET /api/devices/{id}/config"""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    config = await loop.run_in_executor(None, db.get_device_config, device_id)
    return _ok(config)


@auth.require_admin
async def _post_device_config(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/config

    Persists new config and pushes it to the device immediately if
    connected. The Go binary applies tinymix changes on receipt.
    """
    device_id = request.match_info["id"]
    config = await _json_body(request)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.set_device_config, device_id, config)

    # Push to live device if connected
    live = _devices.get(device_id)
    if live is not None:
        await live.send_control({"type": "config", **config})
        log.info(f"[api] Config pushed to live device: {device_id}")
        pushed = True
    else:
        pushed = False

    await _push_event({"type": "device_update", "device_id": device_id,
                       "state": {"config": config}})
    return _ok({"device_id": device_id, "config": config, "pushed": pushed})


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

    Deploy the latest GitHub release to a single device.
    Returns 202 Accepted immediately — update runs in the background.
    Poll GET /api/devices/{id} to observe firmware_ver change.
    """
    device_id = request.match_info["id"]

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)

    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)

    if device_id in _updates_in_progress:
        return _error("update_in_progress", "An update is already in progress", 409)

    release = await _get_cached_release()
    if release is None:
        return _error("no_release", "No release information available — check GitHub", 409)

    asyncio.create_task(_run_update(device_id, release))
    return _ok({"device_id": device_id, "version": release["version"]}, status=202)


@auth.require_admin
async def _post_device_rollback(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/rollback

    Roll back to server.old on the device.
    Requires firmware_previous to be set (i.e. an update was done).
    Returns 202 Accepted — rollback runs in background.
    """
    device_id = request.match_info["id"]

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return _error("device_not_found", f"No device: {device_id}", 404)
    if not row["firmware_previous"]:
        return _error("no_rollback_available",
                      "No previous version available — server.old does not exist", 404)

    live = _devices.get(device_id)
    if live is None:
        return _error("device_offline", "Device is not connected", 409)

    if device_id in _updates_in_progress:
        return _error("update_in_progress", "An update is already in progress", 409)

    asyncio.create_task(_run_rollback(device_id, row["firmware_previous"]))
    return _ok({"device_id": device_id,
                "rolling_back_to": row["firmware_previous"]}, status=202)


# ─── OTA background tasks ─────────────────────────────────────────────────────

async def _run_update(device_id: str, release: dict) -> None:
    """
    Background task: stream new binary to device and trigger update.sh.
    Monitors reconnection for up to 90s to confirm or detect rollback.
    """
    _updates_in_progress.add(device_id)
    loop = asyncio.get_event_loop()

    try:
        await _push_log_event(device_id, "info", "controller",
                              f"OTA update starting → {release['version']}")

        # Fetch binary from GitHub
        binary = await _fetch_binary(release["url"])
        if binary is None:
            await _push_log_event(device_id, "error", "controller",
                                  "Failed to fetch binary from GitHub")
            return

        # Record the current version as previous before updating
        row = await loop.run_in_executor(None, db.get_device, device_id)
        current_ver = row["firmware_ver"] if row else None
        await loop.run_in_executor(
            None, db.set_firmware_previous, device_id, current_ver
        )

        # Stream binary to device via shell
        live = _devices.get(device_id)
        if live is None:
            await _push_log_event(device_id, "error", "controller",
                                  "Device disconnected before update could start")
            return

        ok = await _stream_binary_via_shell(live, binary)
        if not ok:
            await _push_log_event(device_id, "error", "controller",
                                  "Binary transfer failed")
            return

        # Execute update.sh on device
        await _push_log_event(device_id, "info", "controller",
                              "Binary transferred — running update.sh")
        await _exec_shell(live, "/data/local/bin/update.sh")

        # Monitor reconnection for up to 90s
        confirmed = await _monitor_reconnect(device_id, release["version"], timeout=90)

        if confirmed:
            await _push_log_event(device_id, "info", "controller",
                                  f"Update confirmed: {release['version']}")
            await _push_event({
                "type":      "device_updated",
                "device_id": device_id,
                "version":   release["version"],
            })
        else:
            # Check what version reconnected (may have rolled back on-device)
            row = await loop.run_in_executor(None, db.get_device, device_id)
            running = row["firmware_ver"] if row else "unknown"
            await _push_log_event(
                device_id, "warn", "controller",
                f"Update failed or rolled back — running: {running}",
            )
            await _push_event({
                "type":      "device_update_failed",
                "device_id": device_id,
                "running":   running,
            })

    except Exception as e:
        log.exception(f"[api] OTA update error for {device_id}: {e}")
        await _push_log_event(device_id, "error", "controller",
                              f"OTA update exception: {e}")
    finally:
        _updates_in_progress.discard(device_id)


async def _run_rollback(device_id: str, target_version: str) -> None:
    """Background task: roll back to server.old on device."""
    _updates_in_progress.add(device_id)
    try:
        await _push_log_event(device_id, "info", "controller",
                              f"Rolling back to {target_version}")

        live = _devices.get(device_id)
        if live is None:
            await _push_log_event(device_id, "error", "controller",
                                  "Device disconnected before rollback")
            return

        rollback_cmds = (
            "stop echomuse && "
            "cp /data/local/bin/server.old /data/local/bin/server && "
            "start echomuse"
        )
        await _exec_shell(live, rollback_cmds)

        confirmed = await _monitor_reconnect(device_id, target_version, timeout=90)
        loop = asyncio.get_event_loop()

        if confirmed:
            # Clear firmware_previous — server.old is now current
            await loop.run_in_executor(
                None, db.set_firmware_previous, device_id, None
            )
            await _push_log_event(device_id, "info", "controller",
                                  f"Rollback confirmed: {target_version}")
            await _push_event({
                "type":      "device_rolled_back",
                "device_id": device_id,
                "version":   target_version,
            })
        else:
            await _push_log_event(device_id, "warn", "controller",
                                  "Rollback did not reconnect within 90s")

    except Exception as e:
        log.exception(f"[api] Rollback error for {device_id}: {e}")
    finally:
        _updates_in_progress.discard(device_id)


async def _monitor_reconnect(
    device_id: str,
    expected_version: str,
    timeout: int = 90,
) -> bool:
    """
    Poll until the device reconnects and reports expected_version,
    or until timeout seconds elapse.

    Returns True if the expected version is confirmed, False otherwise.
    """
    loop = asyncio.get_event_loop()
    deadline = time.monotonic() + timeout
    await asyncio.sleep(5)  # give the device time to stop

    while time.monotonic() < deadline:
        if device_id in _devices:
            row = await loop.run_in_executor(None, db.get_device, device_id)
            if row and row["firmware_ver"] == expected_version:
                return True
        await asyncio.sleep(2)

    return False


# ─── Shell WebSocket proxy ────────────────────────────────────────────────────

async def _ws_shell(request: web.Request) -> web.WebSocketResponse:
    """
    WS /api/devices/{id}/shell

    Admin-only. Proxies the dashboard terminal (xterm.js) to the
    device's /shell WebSocket endpoint on the Go binary.

    The Go binary spawns sh as root and pipes its stdio as raw binary
    frames. We proxy transparently — no framing added.

    Auth via session cookie or ?token= query param.
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

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    device_ip = live.ip
    shell_uri = f"ws://{device_ip}:8767/shell"

    log.info(f"[api] Shell session opened: {device_id} by {user['username']}")
    await _push_log_event(device_id, "info", "controller",
                          f"Shell session opened by {user['username']}")

    try:
        async with websockets.connect(shell_uri) as device_ws:

            async def browser_to_device():
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await device_ws.send(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        await device_ws.send(msg.data.encode())
                    elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                      aiohttp.WSMsgType.ERROR):
                        break

            async def device_to_browser():
                async for raw in device_ws:
                    if isinstance(raw, bytes):
                        await ws.send_bytes(raw)
                    else:
                        await ws.send_str(raw)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(browser_to_device()),
                    asyncio.create_task(device_to_browser()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        log.warning(f"[api] Shell session error ({device_id}): {e}")
    finally:
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

    Deploy latest release to all connected, approved, non-current devices.
    Returns list of device_ids that updates were started for.
    """
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
        if row["firmware_ver"] == release["version"]:
            skipped.append({"device_id": device_id, "reason": "already_current"})
            continue
        if device_id in _updates_in_progress:
            skipped.append({"device_id": device_id, "reason": "update_in_progress"})
            continue

        asyncio.create_task(_run_update(device_id, release))
        started.append(device_id)

    return _ok({
        "version": release["version"],
        "started": started,
        "skipped": skipped,
    }, status=202)


# ─── System ───────────────────────────────────────────────────────────────────

@auth.require_auth
async def _get_system_status(request: web.Request) -> web.Response:
    """GET /api/system/status"""
    loop = asyncio.get_event_loop()
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    release = await _get_cached_release()

    return _ok({
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
                data = await resp.json()

        tag     = data.get("tag_name", "")
        assets  = data.get("assets", [])
        binary  = next(
            (a for a in assets if a.get("name") == "server"), None
        )
        if not binary:
            log.warning("[api] No 'server' asset found in latest release")
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


# ─── Shell helpers ────────────────────────────────────────────────────────────

async def _stream_binary_via_shell(live, binary: bytes) -> bool:
    """
    Transfer a binary to /data/local/bin/server.new on the device
    via the shell WebSocket connection.

    Uses base64 encoding to safely transfer binary data through a
    text-mode shell. The device decodes it on receipt.

    Returns True on success.
    """
    import base64

    device_ip  = live.ip
    shell_uri  = f"ws://{device_ip}:8767/shell"
    chunk_size = 4096  # bytes before base64 encoding

    try:
        async with websockets.connect(shell_uri) as ws:
            # Prepare destination
            await ws.send("rm -f /data/local/bin/server.new\n")
            await asyncio.sleep(0.2)

            # Stream in base64 chunks, decoded on device
            encoded = base64.b64encode(binary).decode("ascii")
            for i in range(0, len(encoded), chunk_size):
                chunk = encoded[i:i + chunk_size]
                await ws.send(
                    f"printf '%s' '{chunk}' >> /data/local/bin/server.new.b64\n"
                )
                await asyncio.sleep(0.05)

            # Decode on device
            await ws.send(
                "base64 -d /data/local/bin/server.new.b64 "
                "> /data/local/bin/server.new && "
                "rm /data/local/bin/server.new.b64 && "
                "chmod 755 /data/local/bin/server.new && "
                "echo TRANSFER_OK\n"
            )

            # Wait for confirmation
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    if "TRANSFER_OK" in str(msg):
                        log.info(f"[api] Binary transfer confirmed")
                        return True
                except asyncio.TimeoutError:
                    continue

            log.error("[api] Binary transfer timed out waiting for TRANSFER_OK")
            return False

    except Exception as e:
        log.error(f"[api] Binary transfer failed: {e}")
        return False


async def _exec_shell(live, cmd: str) -> None:
    """Send a command to the device shell and return immediately."""
    device_ip = live.ip
    shell_uri = f"ws://{device_ip}:8767/shell"
    try:
        async with websockets.connect(shell_uri) as ws:
            await ws.send(cmd + "\n")
            await asyncio.sleep(0.5)
    except Exception as e:
        log.warning(f"[api] Shell exec failed ({cmd!r}): {e}")


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

async def notify_device_connected(device_id: str) -> None:
    """Called by em_controller when a device successfully registers."""
    await _push_event({"type": "device_connected", "device_id": device_id})


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
        "device_id":        device_id,
        "label":            row["label"],
        "approved":         bool(row["approved"]),
        "ip":               row["ip"],
        "firmware_ver":     row["firmware_ver"],
        "firmware_previous": row["firmware_previous"],
        "first_seen":       row["first_seen"],
        "last_seen":        row["last_seen"],
        "config":           json.loads(row["config"] or "{}"),
        # Live — defaults when device is not connected
        "connected":        live is not None,
        "speaking":         live.speaking  if live else False,
        "muted":            getattr(live, "muted",     False) if live else False,
        "listening":        getattr(live, "listening", False) if live else False,
        "thinking":         getattr(live, "thinking",  False) if live else False,
        # Update state
        "update_in_progress": device_id in _updates_in_progress,
    }
