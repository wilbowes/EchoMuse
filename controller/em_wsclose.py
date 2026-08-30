"""Why a device WebSocket closed, in words, from the close frames.

Every device-plane drop has logged "Data connection closed: <id>" and nothing
else, because the reason was being discarded in two places at once:
`except websockets.exceptions.ConnectionClosed: pass` threw away the
exception carrying the close frames, and `websockets.server` is pinned to
CRITICAL so the library's own account of it never printed either. Three
sessions of chasing intermittent mid-response drops had timing to reason
from and no stated cause (2026-08-30).

Pure so it can be tested: em_controller imports websockets and aiohttp and
cannot be imported by the CI test job, which is the same reason em_volume,
em_linkauth and em_ble_health live on their own.

WHICH SIDE CLOSED IS THE WHOLE POINT. A close frame we SENT means the
controller gave up on the device; one we RECEIVED means the device or its
network went first; neither means the TCP connection died with no close
frame at all, which is a network event rather than a decision. Those three
want different investigations, and the log line has to say which it was.

The case this was written for is `sent 1011 keepalive ping timeout`: the
server pings every 20s and closes if no pong arrives within 10s
(`websockets.serve(ping_interval=20, ping_timeout=10)`), so a link saturated
by a burst of response audio can lose the pong and take the connection with
it. That reads as the device failing when it is the pacing.
"""

from typing import Optional

# Close codes worth naming. Anything else is rendered as its number, which
# is more honest than a wrong guess.
_CODES = {
    1000: "normal",
    1001: "going away",
    1002: "protocol error",
    1003: "unsupported data",
    1005: "no status",
    1006: "abnormal — no close frame",
    1007: "invalid payload",
    1008: "policy violation",
    1009: "message too big",
    1011: "internal error",
    1012: "service restart",
    1013: "try again later",
    1015: "TLS failure",
}

# Codes that mean an ordinary, expected end of connection. Everything else is
# worth a warning: a device that reconnects every few minutes on 1011 looks
# identical, in a log of INFO lines, to one that never dropped at all.
ROUTINE = frozenset({1000, 1001})


def _one(code: Optional[int], reason: Optional[str]) -> str:
    name = _CODES.get(code, "unknown")
    text = (reason or "").strip()
    return f"{code} ({name}){f' {text!r}' if text else ''}"


def describe(
    sent_code: Optional[int] = None,
    sent_reason: Optional[str] = None,
    rcvd_code: Optional[int] = None,
    rcvd_reason: Optional[str] = None,
) -> str:
    """
    One clause naming who closed the connection and why.

    Both frames are reported when both exist — a clean shutdown echoes the
    code back, and a mismatch between what we sent and what came back is
    itself worth seeing.
    """
    if sent_code is None and rcvd_code is None:
        return "closed with no close frame either way (TCP reset or link loss)"
    if sent_code is not None and rcvd_code is not None:
        return f"we closed {_one(sent_code, sent_reason)}, peer echoed {_one(rcvd_code, rcvd_reason)}"
    if sent_code is not None:
        return f"we closed it: {_one(sent_code, sent_reason)}"
    return f"peer closed it: {_one(rcvd_code, rcvd_reason)}"


def is_routine(sent_code: Optional[int], rcvd_code: Optional[int]) -> bool:
    """
    Whether this close is the ordinary end of a connection.

    A missing code is NOT routine: it means the socket died without either
    side saying so, which is exactly the case being hunted.
    """
    code = sent_code if sent_code is not None else rcvd_code
    return code in ROUTINE


def from_exception(exc) -> tuple:
    """
    Pull (sent_code, sent_reason, rcvd_code, rcvd_reason) off a
    websockets ConnectionClosed.

    Duck-typed against `.sent` / `.rcvd` rather than the `.code` / `.reason`
    shorthand, which is deprecated in websockets 15 and emits a warning per
    access; requirements pin 17.0.1, where relying on it would be a bet.
    Everything is fetched defensively so a library change degrades to a
    vaguer log line instead of raising inside an exception handler.
    """
    sent = getattr(exc, "sent", None)
    rcvd = getattr(exc, "rcvd", None)
    return (
        getattr(sent, "code", None), getattr(sent, "reason", None),
        getattr(rcvd, "code", None), getattr(rcvd, "reason", None),
    )
