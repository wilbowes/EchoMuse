"""
Issuing device-link credentials: a fresh token and the controller's CA,
pushed to an Echo over its live shell plane once an admin has approved it,
and the on-device path they live at. em_controller issues them when an
approved device connects; the pairing route issues them for a device
already connected.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

import em_db as db
import em_events
import em_pairing
import em_pki
import em_shell
import em_state

log = logging.getLogger("echomuse.api")


# Canonical on-device credential paths — coupled with
# device/internal/client/tlscreds.go. The Go client re-reads them on every
# dial attempt, so pushed credentials take effect on the next reconnect
# without a firmware restart.
DEVICE_TLS_DIR = "/data/local/etc/echomuse"


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
