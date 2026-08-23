"""
_extract_binary_version must know the scheme compile.sh actually produces.

It knew only the old date-based one (`20260614-1152-dev`) while compile.sh
moved to `git describe`. A clean-tree local build therefore extracted
nothing, was labelled `local-<timestamp>`, and could never match the version
the device reported on reboot — so the dashboard announced a completely
successful deploy as "auto-rolled back". Measured on hardware 2026-08-23.

A message that says the opposite of what happened is worse than a plain
failure: the natural response to being told a deploy reverted is to stop
using the feature.

em_api imports aiohttp and the whole controller stack, so the function's
source is executed in a stub namespace — the same approach as
test_update_interval.py, and it runs the code that actually ships.
"""

import re
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[1]


def _extract():
    """The shipped _extract_binary_version, executed standalone."""
    src = (CONTROLLER / "em_api.py").read_text()
    start = src.index("def _extract_binary_version")
    end = src.index("async def _update_failed", start)
    ns: dict = {}
    exec(src[start:end], ns)
    return ns["_extract_binary_version"]


@pytest.mark.parametrize("embedded,expected", [
    # git describe — what compile.sh produces from a clean tree today
    (b"junk\x00v2.12.0-63-g99628d3\x00junk", "v2.12.0-63-g99628d3"),
    (b"\x00v3.0.0-1-gabcdef0\x00",           "v3.0.0-1-gabcdef0"),
    # date scheme — dirty tree, and every binary predating the change
    (b"junk 20260614-1152-dev junk",         "20260614-1152-dev"),
    (b"20260614-0513-release",               "20260614-0513-release"),
])
def test_both_version_schemes_are_recognised(embedded, expected):
    assert _extract()(embedded) == expected


def test_a_dependency_version_is_never_mistaken_for_ours():
    """
    THE REASON BARE vX.Y.Z IS NOT MATCHED.

    A Go binary embeds its dependencies' module versions, so `vX.Y.Z` is
    nowhere near unique. Measured on the real v2.12.0-63-g99628d3 firmware:
    102 occurrences, 9 distinct, including v0.41.0 (x/sys), v1.5.3
    (gorilla/websocket), v1.0.3 (GoTinyAlsa) and v1.1.41 (miekg/dns).

    Our own version happened to sort first in file order on that build. That
    is luck, not a property — a dependency string landing earlier would make
    the controller label the deploy with somebody else's version number and
    then report a rollback when the device disagreed.

    None is the correct answer here: the caller falls back to a
    `local-<timestamp>` label, which is honest about not knowing.
    """
    deps = b"v0.41.0\x00v1.5.3\x00v1.0.3\x00v1.1.41\x00v0.0.0\x00"
    assert _extract()(deps) is None


def test_a_bare_tag_build_extracts_nothing_rather_than_guessing():
    """
    A build made exactly ON a tag has no `-N-gSHA` suffix and is
    indistinguishable from a dependency version. It belongs on the release
    path anyway, where the version comes from the release rather than from
    the bytes.
    """
    assert _extract()(b"v2.13.0 and v1.5.3 and v0.41.0") is None


def test_the_suffixed_form_wins_over_a_stray_date_string():
    """
    Both patterns can be present — a Go binary contains plenty of digits.
    The current scheme must be tried first, or a coincidental date-shaped
    string earlier in the file would win.
    """
    both = b"20200101-0000-x\x00v2.12.0-63-g99628d3\x00"
    assert _extract()(both) == "v2.12.0-63-g99628d3"


def test_the_dashboard_only_claims_a_rollback_when_it_can_prove_one():
    """
    The other half of the same bug. `_pollReconnect` inferred "auto-rolled
    back" from `firmware_ver !== targetVersion` alone, which is sound only
    when the target is exactly what the device will report — true for a
    release, false for a local build with an unreadable version.

    It now takes the version the device was running BEFORE the push, so a
    rollback is a device back on its previous binary rather than any
    mismatch at all.
    """
    src = (CONTROLLER / "static" / "dashboard.jsx").read_text()

    assert "function _pollReconnect(targetVersion, priorVersion)" in src, \
        "the poll must know what the device was running before the push"

    body = src[src.index("function _pollReconnect("):]
    body = body[:body.index("\n  async function ")] if "\n  async function " in body \
        else body[:4000]
    assert "auto-rolled back" in body
    claim = body[:body.index("auto-rolled back")]
    # The COMPARISON, not merely the identifier. Asserting that
    # "priorVersion" appears anywhere before the wording passes even when
    # the gate is replaced by a constant, because the parameter is still
    # named in the signature and the comments — verified by reintroducing
    # exactly that, which the looser assertion missed.
    assert re.search(r"firmware_ver\s*===\s*priorVersion", claim), \
        "the rollback wording must be gated on the device having come back " \
        "on the version it was running BEFORE the push, not on a bare " \
        "mismatch with the target"

    # Every caller has to supply it, or the gate silently reads undefined
    # and the claim quietly becomes unreachable instead of wrong.
    calls = re.findall(r"_pollReconnect\((?!targetVersion)([^)]*)\)", src)
    assert calls, "no _pollReconnect call sites found"
    for args in calls:
        assert "," in args, f"_pollReconnect({args}) is missing priorVersion"
