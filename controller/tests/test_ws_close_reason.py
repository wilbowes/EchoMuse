"""
Why a device WebSocket closed — the line that did not exist.

Every device-plane drop logged "Data connection closed: <id>" and nothing
more, because the reason was discarded twice over: the handlers caught
`ConnectionClosed` and `pass`ed, throwing away the exception carrying the
close frames, and `websockets.server` is pinned to CRITICAL so the library
never printed its own account either. Three sessions chasing intermittent
mid-response drops had timing to reason from and no stated cause
(2026-08-30).

em_controller imports websockets and aiohttp and cannot be imported by the
CI test job, so the rendering lives in em_wsclose and is tested here; that
the handlers actually call it is asserted against the source.
"""

import re
from pathlib import Path

import em_wsclose as wsc

CONTROLLER = Path(__file__).resolve().parents[1]


class _Close:
    """Duck-type of websockets.frames.Close."""

    def __init__(self, code, reason=""):
        self.code = code
        self.reason = reason


class _Exc:
    def __init__(self, sent=None, rcvd=None):
        self.sent = sent
        self.rcvd = rcvd


# ── Which side closed ────────────────────────────────────────────────────────
#
# The three cases want different investigations, so the line has to say which
# it was: we gave up on the device, the device went first, or the TCP
# connection died with nobody saying anything.

def test_a_close_we_sent_is_attributed_to_us():
    out = wsc.describe(1011, "keepalive ping timeout", None, None)
    assert "we closed" in out
    assert "1011" in out and "keepalive ping timeout" in out


def test_a_close_the_peer_sent_is_attributed_to_them():
    out = wsc.describe(None, None, 1001, "going away")
    assert "peer closed" in out
    assert "1001" in out


def test_no_frame_either_way_says_so_rather_than_guessing():
    """
    The socket died with neither side announcing it — a network event, not a
    decision, and it must not be reported as one.
    """
    out = wsc.describe(None, None, None, None)
    assert "no close frame" in out
    # No single cause asserted: this is also the path taken when the handler
    # exits on a generic error rather than a close.
    assert "TCP reset" in out and "error logged above" in out


def test_both_frames_are_reported_when_both_exist():
    out = wsc.describe(1000, "", 1000, "")
    assert "we closed" in out and "echoed" in out


# ── Severity ─────────────────────────────────────────────────────────────────

def test_an_ordinary_close_is_routine():
    assert wsc.is_routine(1000, 1000)
    assert wsc.is_routine(None, 1001)


def test_a_keepalive_timeout_is_not_routine():
    """The case this was written for. 1011 landing at INFO would leave a
    device dropping every few minutes looking identical to one that never
    dropped at all."""
    assert not wsc.is_routine(1011, None)


def test_a_missing_code_is_not_routine():
    """
    No code means the socket died without either side saying so, which is
    exactly what is being hunted — it must not be filed as a clean exit.
    """
    assert not wsc.is_routine(None, None)


# ── Extraction ───────────────────────────────────────────────────────────────

def test_the_frames_are_read_off_the_exception():
    parts = wsc.from_exception(
        _Exc(sent=_Close(1011, "keepalive ping timeout"), rcvd=None))
    assert parts == (1011, "keepalive ping timeout", None, None)
    assert wsc.describe(*parts).startswith("we closed it")


def test_an_exception_without_frames_degrades_instead_of_raising():
    """
    `.sent`/`.rcvd` are used rather than the `.code`/`.reason` shorthand,
    which is deprecated in websockets 15 and warns on every access while
    requirements pin 17.0.1. A library change must cost a vaguer log line,
    never an exception raised inside an exception handler.
    """
    assert wsc.from_exception(object()) == (None, None, None, None)
    assert wsc.from_exception(_Exc()) == (None, None, None, None)


def test_an_unknown_code_is_rendered_not_guessed():
    out = wsc.describe(4999, "custom", None, None)
    assert "4999" in out and "unknown" in out


# ── The wiring, asserted on source ───────────────────────────────────────────

def test_both_device_planes_report_the_reason():
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "import em_wsclose" in src
    # The bug was `except ConnectionClosed: pass` — two of them, one per
    # plane. Neither may come back.
    assert not re.search(r"except websockets\.exceptions\.ConnectionClosed:\s*\n\s*pass",
                         src), "a plane is discarding the close reason again"
    assert len(re.findall(r"em_wsclose\.from_exception\(", src)) >= 2, \
        "both the control and data planes must capture the close frames"


def test_the_data_plane_cannot_report_an_unset_reason():
    """
    The finally block logs on EVERY exit path, including the ones that never
    raise ConnectionClosed, so the tuple has to exist before the try.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    handler = src[src.index("async def handle_data("):]
    handler = handler[:handler.index("\n    try:")]
    assert "_close_why = (None, None, None, None)" in handler, \
        "_close_why must be initialised before the try, or the finally NameErrors"
