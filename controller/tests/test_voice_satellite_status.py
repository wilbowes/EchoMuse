"""
#349: a device with no Home Assistant connection must not read as healthy.

`_start_esphome_voice_turn` refuses a turn on exactly two conditions —
no registered server, or no active satellite — and neither reached the
dashboard. `deviceState()` has no rank for it, so such a device renders
as idle: the same thing a working device renders as. Measured on the
production controller, 23 wake events in 14 hours could not start a turn
while all three tiles read healthy throughout.

These guards pin the three things that would each silently undo it. The
suite cannot import em_esphome (it pulls in aiohttp via em_api), which is
why this is source inspection rather than a call — the same constraint
that put em_barge and em_runbarrier in their own modules.

Comments are stripped before every source assertion. Three guards in this
tree have matched the comment explaining a rule instead of the code
implementing it; the shape of that bug is now known and designed out.
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _strip_py_comments(src: str) -> str:
    """
    Comments AND docstrings. Stripping only `#` lines is the version of this
    helper that fails: the prose explaining why a field is absent names the
    field, so the guard matches its own rationale and passes whatever the
    code does. Written the wrong way first, caught by this file's own
    fourth-state test, and left documented because it is now the third time
    a source guard in this tree has done it.
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(re.sub(r"#.*$", "", line) for line in src.splitlines())


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _fn_source(path: Path, header: str, strip) -> str:
    src = strip(path.read_text())
    start = src.index(header)
    return src[start:start + 4_000]


def test_get_status_reads_what_the_turn_path_reads():
    """
    The readout and the decision must come from one source.

    A status assembled from flags beside the decision can disagree with
    it, and it would do so on the panel someone opens *because* the
    device is not answering. `speaking` drifted from its own push for
    exactly this reason and cost a release.
    """
    body = _fn_source(CONTROLLER / "em_esphome.py",
                      "def get_status(", _strip_py_comments)
    assert "_servers.get(device_id)" in body, \
        "server presence must be read from the same registry get_server uses"
    assert "get_satellite()" in body, \
        "HA presence must be the satellite the turn path requires, not a flag"


def test_status_has_no_fourth_state():
    """
    The BT proxy reports haSubscribed because a connected-but-unsubscribed
    HA is real there. Voice has no equivalent — the precondition is the
    satellite existing — so a fourth field would describe something this
    code does not track, and the dashboard would render a state that can
    never be reached.
    """
    body = _fn_source(CONTROLLER / "em_esphome.py",
                      "def get_status(", _strip_py_comments)
    assert "haSubscribed" not in body
    for field in ("port", "listening", "haConnected"):
        assert f'"{field}"' in body, f"get_status must report {field}"


def test_api_exposes_it_beside_the_bt_proxy():
    """
    Same payload, same shape, one poll. If this drops out, the dashboard
    silently falls back to rendering nothing rather than erroring.
    """
    body = _fn_source(CONTROLLER / "em_api.py",
                      "def _merge_device(", _strip_py_comments)
    assert '"voiceSatellite"' in body
    assert "em_esphome.get_status(device_id)" in body


def test_dashboard_renders_the_state_and_not_just_the_port():
    """
    The row used to print a bare port number, which answered a question
    nobody had. The port stays — it is what a stale HA config entry is
    keyed on — but the state is what the row is for.
    """
    src = _strip_js_comments(
        (CONTROLLER / "static" / "dashboard.jsx").read_text())
    assert "device.voiceSatellite" in src, \
        "the Status tab must read the satellite state"
    assert "'Voice assistant'" in src, \
        "the row must be labelled for what it reports, not for the protocol"
    for state in ("HA connected", "Waiting for HA", "Port down",
                  "No satellite server"):
        assert state in src, f"missing the {state!r} state"
    # A row that renders nothing when the field is absent is the failure
    # this whole change exists to prevent, one level up.
    assert "if (!vs)" in src, \
        "a missing voiceSatellite must render as a fault, not as blank"
