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
import logging

from aiohttp import web

import em_db as db
import em_events
import em_shell
import em_ota
import em_http
import em_state
import em_auth as auth
import em_tasks
import em_ingressauth
import em_routes_activity
import em_routes_auth
import em_routes_config
import em_routes_devices
import em_routes_events
import em_routes_link
import em_routes_oww
import em_routes_provision
import em_routes_shell
import em_routes_static
import em_routes_system
import em_routes_updates
import em_pairing

log = logging.getLogger("echomuse.api")


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
        client_max_size=em_routes_updates.UPLOAD_MAX_BYTES + 8 * 1024 * 1024,
    )

    # Static / setup
    em_routes_static.register(app)

    em_routes_auth.register(app)

    # Devices, then the Activity tab's routes under /api/devices/{id}
    em_routes_devices.register(app)
    em_routes_activity.register(app)

    em_routes_oww.register(app)
    em_routes_shell.register(app)

    # Updates: firmware, emOS, releases, the controller release notice
    em_routes_updates.register(app)

    # Global device config
    em_routes_config.register(app)

    # System
    em_routes_system.register(app)

    # Provisioning
    em_routes_provision.register(app)
    em_routes_link.register(app)

    # Live events WebSocket
    em_routes_events.register(app)

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
    except Exception:
        log.exception(f"Unhandled error in {request.method} {request.path}")
        return em_http.error("internal_error", "An internal error occurred", 500)


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



