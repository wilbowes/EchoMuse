"""
Pairing an Echo: who has asked, and who an admin has approved.

A device holding link credentials dials only wss. When those stop working (a
regenerated CA, a move to a new controller) or it never had any, the owner
holds its action button: the device opens a two-minute pairing window, asks
over whatever connection it can make, and an admin approves it in the
dashboard. The approval is what issues credentials: a fresh token and the CA,
pushed over that connection (em_api._issue_credentials). Approving a NEW
device opens the same window, so approval is the one human decision and no
separate "secure link" step exists (Wil, 2026-09-26).

Nothing here decides link auth. A pairing device whose approval is live has
its token rotated before the auth decision runs, which makes it an
unconfirmed device, and em_linkauth already admits those. So no rule in
em_linkauth is bypassed, and a presented token that is wrong is still refused.

In memory on purpose: a request is repeated every few seconds for as long as
the device's window is open, and an approval that outlives a controller
restart would issue credentials nobody is waiting for.
"""

from __future__ import annotations

import time

# A request is dropped this long after it was last repeated. The device's
# window is 120s and it repeats every ~5s, so this only has to outlast a gap.
REQUEST_TTL_S = 30.0

# How long an approval waits for the device to take it: the rest of the
# device's two-minute window, with slack for someone reading the dashboard.
APPROVAL_TTL_S = 180.0

_requests: dict[str, dict] = {}
_approvals: dict[str, float] = {}


def request(device_id: str, via: str, now: float | None = None) -> bool:
    """
    Record that the device asked to pair. `via` is "link" (it is connected and
    asked over the control plane) or "plain" (it could not connect and dialled
    plain to ask). True when this is a new request rather than a repeat.
    """
    now = time.time() if now is None else now
    fresh = pending_request(device_id, now) is None
    prev = _requests.get(device_id)
    _requests[device_id] = {"via": via, "at": now,
                            "first": prev["first"] if prev and not fresh else now}
    return fresh


def pending_request(device_id: str, now: float | None = None) -> dict | None:
    """The live request for this device, or None."""
    now = time.time() if now is None else now
    r = _requests.get(device_id)
    if r is None:
        return None
    if now - r["at"] > REQUEST_TTL_S:
        _requests.pop(device_id, None)
        return None
    return r


def approve(device_id: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    _approvals[device_id] = now + APPROVAL_TTL_S


def approved(device_id: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    until = _approvals.get(device_id)
    if until is None:
        return False
    if now > until:
        _approvals.pop(device_id, None)
        return False
    return True


def done(device_id: str) -> None:
    """Credentials issued, or the device already had them: close both."""
    _approvals.pop(device_id, None)
    _requests.pop(device_id, None)


def forget(device_id: str) -> None:
    done(device_id)
