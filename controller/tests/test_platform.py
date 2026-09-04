"""
The base OS a device booted is a runtime VALUE, and absence is not FireOS.

Two things are guarded here. The decision itself — which payloads mean
anything on which base — and the strings, which are spelled in Go on the
device and in Python on the controller and must match exactly. A typo in
either place makes every device read as Android forever: the debloat keeps
being pushed at emOS devices, `pm hide` keeps running against nothing, and
nothing anywhere reports a problem. That is the same silent-typo failure
test_capabilities.py exists to prevent, so it is checked the same way.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import em_platform  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORM_GO = ROOT / "device" / "internal" / "platform" / "platform.go"
CONTROL_GO = ROOT / "device" / "internal" / "client" / "control.go"


def _strip_comments(src: str) -> str:
    """
    Drop // comments and block comments before matching.

    Source guards that grep raw text find the comment explaining the rule and
    pass on it — that has happened three times in this repo. Everything below
    matches against code only.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.split("\n"))


# ─── The decision ─────────────────────────────────────────────────────────────

def test_emos_is_not_android():
    """A device that said emos gets no Android payloads."""
    assert em_platform.android_userspace("emos") is False


def test_fireos_is_android():
    assert em_platform.android_userspace("fireos") is True


def test_absence_keeps_todays_behaviour():
    """
    Firmware too old to report the field, and a device that has not sent
    stats yet, must both behave exactly as they did before this existed.

    This is the degrade-to-old-behaviour rule. Reading None as emOS would
    silently stop debloating the entire existing fleet.
    """
    assert em_platform.android_userspace(None) is True


def test_unknown_values_are_android():
    """
    An unrecognised value resolves toward Android, not toward emOS.

    A future base we have not heard of, or a corrupted field, must not be
    treated as "definitely emOS" — the positive claim is the only thing that
    turns Android payloads off.
    """
    for value in ("unknown", "", "linux", "EMOS", " emos", "emosaic"):
        assert em_platform.android_userspace(value) is True, value


def test_label_never_invents_fireos():
    """
    A device that has not said what it runs is reported as unknown.

    Naming it FireOS in a panel states a fact nobody established — the same
    reason aec_ref returns None rather than "sw".
    """
    assert em_platform.label("emos") == "emOS"
    assert em_platform.label("fireos") == "FireOS"
    assert em_platform.label(None) == "unknown"
    assert em_platform.label("something else") == "unknown"


# ─── The strings, mirrored across the two languages ──────────────────────────

def test_values_match_the_firmware():
    """
    The three answers are spelled identically in Go and Python.

    Read out of the Go constant block rather than hardcoded here, so this
    fails if the firmware renames one rather than passing on a stale copy.
    """
    src = _strip_comments(PLATFORM_GO.read_text())
    block = re.search(r"const \(\s*(.*?)\s*\)", src, re.S)
    assert block, "could not find the const block in platform.go"
    go_values = dict(re.findall(r'(\w+)\s*=\s*"([^"]+)"', block.group(1)))
    assert go_values == {
        "EmOS": em_platform.EMOS,
        "FireOS": em_platform.FIREOS,
        "Unknown": em_platform.UNKNOWN,
    }, f"Go and Python disagree about the base OS values: {go_values}"


def test_wire_key_matches_the_firmware():
    """
    The register field is named the same at both ends.

    A mismatch here is invisible: the controller reads a key nothing sends,
    every device reports None, and None correctly degrades to Android — so
    the fleet keeps working and the feature simply never activates.
    """
    src = _strip_comments(CONTROL_GO.read_text())
    assert re.search(r'"%s":\s*platform\.Base\(\)' % em_platform.REGISTER_KEY, src), \
        "the register message does not carry the base OS"


def test_base_os_rides_registration_not_stats():
    """
    It must arrive with REGISTRATION, not on the stats tick.

    This is the whole bug that PR #427 shipped and #428 fixed. The only
    consumer is the payload reconcile, which runs the instant a device
    connects — roughly 30 seconds before the first stats report. Riding the
    tick meant the field read as unknown exactly when it was asked, so an
    emOS device was sent the Magisk service.d script and each attempt sat out
    the full 120s transfer timeout. Measured on EFF, 2026-09-04: 240s.
    """
    stats_src = _strip_comments(
        (ROOT / "device" / "internal" / "client" / "stats.go").read_text())
    assert "BaseOs" not in stats_src, \
        "the base OS is back on the stats message, where it is 30s too late"
    assert em_platform.REGISTER_KEY not in stats_src
    assert not hasattr(em_platform, "STATS_KEY"), \
        "STATS_KEY implies a stats-borne value; the field rides registration"
