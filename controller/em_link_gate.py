"""
Who may hold a device-link plane: the token and TLS check on each WebSocket
(the decision itself is em_linkauth's), the token-confirmed test, and the rule
that /data and /shell follow the device's control connection.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import hmac
import logging
import os

from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_db as db
import em_device_view
import em_device
import em_linkauth

log = logging.getLogger("echomuse")


# Enforcing posture: reject device connections that are not TLS + a valid
# per-device token. Leave 0 until the whole fleet shows tls=true —
# a plain, tokenless connection is the legacy default and must keep
# working during the rollout.
REQUIRE_DEVICE_TLS = os.environ.get("REQUIRE_DEVICE_TLS", "0") == "1"


async def link_auth_ok(
    ws: WebSocketServerProtocol, device_id: str, secure: bool, plane: str
) -> bool:
    """
    Device-link auth gate, applied to all three WS planes once the
    device_id is known.

    Rules (rollout-safe by construction):
      - a presented token that MISMATCHES a stored one always rejects;
      - a stored token with NO token presented is allowed unless
        REQUIRE_DEVICE_TLS — the DB row is minted before the files land
        on the device, and rejecting in that window would cut off the
        shell plane that the credential push itself rides on;
      - a presented token for a device with NOTHING on record is ignored,
        not rejected (see below);
      - REQUIRE_DEVICE_TLS=1 requires TLS + a matching token, full stop.

    That third rule used to be a rejection, and it made deleting a device a
    one-way door. Delete removes the row, the token is a column on it, and the
    device re-reads its credential file on every dial — so it presented a token
    nothing recognised and was refused on all three planes, INCLUDING the shell
    plane the controller would otherwise push fresh credentials over. The
    device retried forever behind a pulsing orange ring, and the dashboard had
    nothing to show because as far as it was concerned the device did not
    exist.

    Rejecting there also never bought anything. The rule immediately above
    admits a connection presenting no token at all, so an unrecognised token
    was being treated as worse than none while anyone could simply omit the
    header. A device with a stale credential now comes back as pending and
    waits for approval, which is the decision a human should be making anyway.

    `REQUIRE_DEVICE_TLS=1` installs are unaffected: the last rule still
    demands a token that matches a stored one, so a deleted device is refused
    there regardless and re-provisioning over USB stays the intended path.
    """
    presented = None
    try:
        presented = ws.request.headers.get("X-EM-Token")
    except AttributeError:
        pass

    loop = asyncio.get_event_loop()
    expected, confirmed = await loop.run_in_executor(
        None, db.get_device_link_auth, device_id)

    verdict = em_linkauth.decide(
        presented=presented,
        expected=expected,
        confirmed=confirmed,
        secure=secure,
        require_tls=REQUIRE_DEVICE_TLS,
    )
    if not verdict.ok:
        log.warning(f"[{plane}] {device_id}: {verdict.reason} — rejecting")
        # Only for a device with a row and a token, so ids nobody issued
        # cannot grow the map. The dashboard shows it on the Link row.
        if expected:
            em_device_view.note_link_refused(device_id, verdict.reason)
        return False
    if presented and expected and not confirmed:
        # First sight of the device holding its token: from now on it must.
        if await loop.run_in_executor(
                None, db.confirm_device_token, device_id, presented):
            log.info(f"[{plane}] {device_id}: link token confirmed — "
                     f"a connection without it will be refused from now on")
    if plane == "control":
        em_device_view.clear_link_refused(device_id)
    if verdict.stale_token:
        # Allowed, but worth seeing in the log: almost always a device that was
        # deleted and has come back carrying the credential from its previous
        # life, which is the answer to "why is this in pending again".
        log.warning(
            f"[{plane}] {device_id}: {verdict.reason}. "
            f"Treating as an unregistered device; it will need approval."
        )
    return True


async def presented_its_token(ws, device_id: str) -> bool:
    """Whether this connection's X-EM-Token is the device's stored token."""
    try:
        presented = ws.request.headers.get("X-EM-Token")
    except AttributeError:
        return False
    expected = await asyncio.get_event_loop().run_in_executor(
        None, db.get_device_token, device_id)
    return bool(presented and expected and hmac.compare_digest(presented, expected))


def _peer_ip(ws) -> str | None:
    try:
        return ws.remote_address[0]
    except (AttributeError, TypeError, IndexError):
        return None


def not_from_control(device: "em_device.Device", ws, secure: bool) -> str | None:
    """Why a /data or /shell connection is not the device's own, or None."""
    return em_linkauth.follows_control(
        control_peer=_peer_ip(device.control_ws),
        control_secure=bool(getattr(device, "secure", False)),
        peer=_peer_ip(ws),
        secure=secure,
    )
