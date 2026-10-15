"""
Device-link credentials: the CA and token the provisioning wizard installs
over adb, the controller address list it writes, and pairing — an approved
Echo is issued a fresh token and the CA over its live connection.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from aiohttp import web

import em_auth as auth
import em_db as db
import em_endpoints
import em_events
import em_http
import em_pairing
import em_payloads
import em_pki
import em_shell
import em_state
import em_tasks

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_post("/api/provision/tls_credentials", _post_provision_tls_credentials)
    app.router.add_get("/api/provision/controller_endpoints", _get_provision_controller_endpoints)
    app.router.add_post("/api/devices/{id}/pair",         _post_pair)


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
    if em_state.tls_dir is None:
        return em_http.error("tls_unavailable",
                      "Device-link TLS is not active on this controller", 503)
    body      = await em_http.json_body(request)
    device_id = em_http.require_str(body, "device_id")

    loop  = asyncio.get_event_loop()
    token = await loop.run_in_executor(None, db.ensure_device_token, device_id)
    return em_http.ok({
        "ca_pem": em_pki.ca_pem(em_state.tls_dir),
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
    if em_state.tls_dir is None:
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
        em_tasks.spawn(issue_credentials(device_id))
    log.info(f"[api] {request['user']['username']} approved pairing for {device_id}")
    return em_http.ok({"approved": True, "connected": live is not None})


async def issue_credentials(device_id: str) -> None:
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
    if live is None or em_state.tls_dir is None:
        return
    try:
        await em_events.push_log(device_id, "info", "controller",
                              "Pairing: installing link credentials")
        # Minted here and stored only once the device has it. The push rides
        # the shell plane, which the device dials with its CURRENT token, so
        # storing the new one first refused the push on any device that was
        # connected over wss (found on 15LE, 2026-09-26).
        token = secrets.token_urlsafe(32)
        ca    = em_pki.ca_pem(em_state.tls_dir)

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
