"""
Intercom sessions and the controller's switchboard (Phase 1).

The controller is the switchboard for intercom calls: it knows both
devices, creates the session, issues the one-time token, and fans the
end out to both ends. It never touches call audio — that flows device
to device in a later phase.

Two layers, one module. The session state (IntercomSession,
IntercomRegistry) is pure, like em_barge and em_pacing: no sockets, so
the transition rules are testable without a device. The switchboard
operations below it (start_intercom, end_intercom) are the part the
routes and the button handler call — and they live here rather than in
em_controller for a load-bearing reason: no module but em_controller
itself may import em_controller by name (a second copy gets loaded),
so anything the dashboard needs to call must live in a module that
does not. em_controller owns only the device's own state reports
(_on_intercom_state) and the in-call guards.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

import em_db as db
import em_state

log = logging.getLogger("echomuse.intercom")

# The device intercom listener port (device/internal/intercom, DefaultPort).
# Sendspin holds 8928; the intercom takes the next port up. The device
# opens its firewall rule for this port only while a call is being
# attempted or is live.
INTERCOM_PORT = 8929


# A session is active from the moment the controller offers it to both
# devices until it is ended, for any reason. There is no separate
# "offered" state: the offer is the call in Phase 1, and a device that
# never acks is caught by the controller's view of its intercom_state,
# not by a session timeout here.
STATE_ACTIVE = "active"
STATE_ENDED = "ended"


class IntercomError(ValueError):
    """A call that cannot be started or ended as asked."""


@dataclass
class IntercomSession:
    """One call between two devices."""

    session_id: str
    caller_id: str
    callee_id: str
    # The one-time token both devices are offered. The callee checks a
    # dialler against it in Phase 2; Phase 1 carries it without acting
    # on it. It is a session secret: it must never be logged.
    token: str
    state: str = STATE_ACTIVE
    created_monotonic: float = field(default_factory=time.monotonic)
    ended_reason: str | None = None

    @property
    def peer_of(self) -> dict[str, str]:
        """Each device's peer in this call."""
        return {self.caller_id: self.callee_id, self.callee_id: self.caller_id}

    def peer(self, device_id: str) -> str | None:
        """The other end of this call for device_id, or None."""
        return self.peer_of.get(device_id)


class IntercomRegistry:
    """All intercom sessions this controller knows about.

    A device is in at most one call at a time: creating a second call
    for a device that already has one is an error, and the caller must
    end the current call first. Ending is idempotent — the controller
    fans the end out to both ends, and either end (or the dashboard)
    may ask again without it being an error.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, IntercomSession] = {}

    def create(self, caller_id: str, callee_id: str) -> IntercomSession:
        """Start a session between two devices.

        Raises IntercomError for a missing id, a call to oneself, or
        either device already being in a call.
        """
        if not caller_id or not callee_id:
            raise IntercomError("both devices are required for a call")
        if caller_id == callee_id:
            raise IntercomError("a device cannot call itself")
        for device_id in (caller_id, callee_id):
            if self.active_for(device_id) is not None:
                raise IntercomError(f"{device_id} is already in a call")
        session = IntercomSession(
            session_id=secrets.token_hex(16),
            caller_id=caller_id,
            callee_id=callee_id,
            token=secrets.token_urlsafe(24),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> IntercomSession | None:
        """The session with this id, or None."""
        return self._sessions.get(session_id)

    def end(self, session_id: str, reason: str = "hangup") -> IntercomSession | None:
        """End a session. Idempotent: ending an ended session returns
        it unchanged, and ending an unknown session returns None."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.state == STATE_ENDED:
            return session
        session.state = STATE_ENDED
        session.ended_reason = reason
        return session

    def active_for(self, device_id: str) -> IntercomSession | None:
        """The active session a device is in, or None."""
        for session in self._sessions.values():
            if session.state == STATE_ACTIVE and device_id in session.peer_of:
                return session
        return None

    def active_sessions(self) -> list[IntercomSession]:
        """Every session still active, in creation order."""
        return [s for s in self._sessions.values() if s.state == STATE_ACTIVE]


# ── The switchboard ─────────────────────────────────────────────────────────
# One registry for the process; a device is in at most one call at a time.
INTERCOM = IntercomRegistry()


async def _device_label(device_id: str) -> str:
    """The dashboard label for a device, falling back to its id."""
    try:
        loop = asyncio.get_event_loop()
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row is not None and row["label"]:
            return row["label"]
    except Exception:
        pass
    return device_id


async def start_intercom(caller_id: str, callee_id: str) -> IntercomSession:
    """Start a call between two devices.

    Both must be connected, intercom-capable, not muted, not in a voice
    turn and not already in a call. The offer goes to both devices with
    the role each plays and the other's intercom listener address: the
    callee rings and dials the caller on its button, the caller waits
    to be dialled. The token travels with the offer for the dialler to
    present.

    Raises IntercomError for anything that cannot start.
    """
    caller = em_state.devices.get(caller_id)
    callee = em_state.devices.get(callee_id)
    if caller is None or callee is None:
        missing = caller_id if caller is None else callee_id
        raise IntercomError(f"{missing} is offline")
    for device in (caller, callee):
        if not device.intercom_capable:
            raise IntercomError(
                f"{device.device_id}'s firmware has no intercom mode")
        if device.muted:
            raise IntercomError(
                f"{device.device_id} is muted — a muted device never calls")
        if device.voice_lock.locked():
            raise IntercomError(
                f"{device.device_id} is in a voice turn")
    session = INTERCOM.create(caller_id, callee_id)
    caller_label = await _device_label(caller_id)
    callee_label = await _device_label(callee_id)
    # Either send failing leaves the session half-offered; the end below
    # cleans it up, and the devices that did hear the offer hear the end.
    try:
        await caller.send_control({
            "type": "intercom_offer", "session": session.session_id,
            "peer_id": callee_id, "peer_label": callee_label,
            "token": session.token, "role": "caller",
            "peer_addr": f"{callee.ip}:{INTERCOM_PORT}",
        })
        await callee.send_control({
            "type": "intercom_offer", "session": session.session_id,
            "peer_id": caller_id, "peer_label": caller_label,
            "token": session.token, "role": "callee",
            "peer_addr": f"{caller.ip}:{INTERCOM_PORT}",
        })
    except Exception:
        await end_intercom(session.session_id, "offer_failed")
        raise
    log.info(f"intercom {session.session_id}: {caller_id} ↔ {callee_id}")
    return session


async def end_intercom(session_id: str, reason: str = "hangup") -> IntercomSession | None:
    """End a call, fanning intercom_end out to both devices.

    Idempotent: ending an ended or unknown session is a no-op that
    returns what the registry knows (or None). The end goes to both
    ends rather than relying on one device telling the other, so hangup
    still works when the peer link itself is what degraded. A device
    that is offline simply never hears it; its state clears on its next
    intercom_state report.
    """
    session = INTERCOM.get(session_id)
    if session is None:
        return None
    if session.state == STATE_ENDED:
        # A duplicate end — a dashboard double-click, or both ends
        # hanging up at once — asks nothing of the devices again. A
        # device that missed the first end clears itself when its link
        # drops (the device ends a call it can no longer be hung up
        # from), so there is nothing a re-send would rescue.
        return session
    session = INTERCOM.end(session_id, reason)
    for device_id in (session.caller_id, session.callee_id):
        device = em_state.devices.get(device_id)
        if device is None:
            continue
        try:
            await device.send_control({
                "type": "intercom_end", "session": session_id,
                "reason": reason,
            })
        except Exception:
            # The device will clear itself on disconnect or its next
            # state report; a failed end must not fail the hangup.
            log.warning(f"[{device_id}] intercom_end for {session_id} "
                        f"could not be sent")
        if device.intercom_session == session_id:
            device.intercom_state = "idle"
            device.intercom_session = None
            device.intercom_peer = None
    log.info(f"intercom {session_id} ended ({reason})")
    return session


async def end_intercom_for_device(device, reason: str = "hangup") -> IntercomSession | None:
    """End whatever call a device is in, if any. None when it is not."""
    session = INTERCOM.active_for(device.device_id)
    if session is None:
        return None
    return await end_intercom(session.session_id, reason)


def wake_action(state: str | None) -> str | None:
    """What a wake word does while a device is in an intercom state.

    "nest" — the call is flowing (in_call): the wake starts a normal
    HA turn, and the turn suspends the call for its duration (see
    suspend_intercom) and resumes it after.
    "decline" — the call is being attempted (ringing / calling) or
    already suspended for a turn: the wake is logged and dropped, the
    call continues. A wake during an attempt would start a turn the
    call would then collide with, and a wake during a suspend belongs
    to the turn already running (its voice_lock is held, so the
    listener's busy path handles it first).
    None — no call: the wake is an ordinary one.
    """
    if state == "in_call":
        return "nest"
    if state in ("ringing", "calling", "suspended"):
        return "decline"
    return None


async def suspend_intercom(device) -> bool:
    """Pause a flowing call on one device for a nested HA turn.

    True when the device was told to suspend (or already is). The
    device stops sending its mic to the peer, stops playing peer
    audio, and reports intercom_state "suspended"; the peer link stays
    up. The send is not awaited for an ack: the suspend lands in
    milliseconds, and the command itself does not start for a beat
    after the wake word, so the race it would close does not exist in
    practice — the peer hears the wake word it already heard live,
    not the command. A failed send logs and returns False, and the
    turn proceeds anyway: the user's command outranks the call.
    """
    session = INTERCOM.active_for(device.device_id)
    if session is None:
        return False
    if device.intercom_state == "suspended":
        return True
    if device.intercom_state != "in_call":
        return False
    try:
        await device.send_control({
            "type": "intercom_suspend", "session": session.session_id,
        })
    except Exception:
        log.warning(f"[{device.device_id}] intercom_suspend for "
                    f"{session.session_id} could not be sent")
        return False
    return True


async def resume_intercom(device) -> bool:
    """Flow a suspended call again after the nested turn ends.

    True when the device was told to resume. The message goes out
    whether the controller believes the device is in_call or
    suspended: an ack lost on the way back leaves the controller
    believing in_call while the device is suspended, and the device's
    resume is a no-op when it is not suspended — so the tolerant
    send is the correct one, and a lost ack cannot wedge a call
    silent. False only when there is no call to resume.
    """
    session = INTERCOM.active_for(device.device_id)
    if session is None:
        return False
    try:
        await device.send_control({
            "type": "intercom_resume", "session": session.session_id,
        })
    except Exception:
        log.warning(f"[{device.device_id}] intercom_resume for "
                    f"{session.session_id} could not be sent")
        return False
    return True
