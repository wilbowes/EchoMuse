"""
#306: /api/system/status and the support bundle reported loop_lag_peak_ms
as 0.0 while the log right next to them showed the loop stalling at 881ms.

Cause: em_start.py execvp's em_controller.py, so the running controller is
__main__. The readers did `import em_controller`, which does not find
__main__ under that name and loads a SECOND, never-initialised copy —
reading defaults instead of the live monitor's peak.

The fix resolves the running module object. em_api imports aiohttp and the
whole stack, so it is deliberately not importable here (see conftest); the
helper's source is extracted and executed in a stub namespace instead,
testing the code that actually ships.
"""

import re
import sys
import types
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _load_helper(monkeypatched_sys):
    src = (CONTROLLER / "em_api.py").read_text()
    start = src.index("def _running_controller_module")
    ends = [i for i in (src.find("\ndef ", start + 10),
                        src.find("\nasync def ", start + 10),
                        src.find("\n@", start + 10)) if i != -1]
    ns = {"sys": monkeypatched_sys}
    exec(src[start:min(ends)], ns)
    return ns["_running_controller_module"]


def test_production_resolves_main_not_a_fresh_copy():
    """In production __main__ IS the running em_controller: it carries the
    attribute the monitor writes, so it must win over a name import."""
    main = types.ModuleType("__main__")
    main._loop_lag_peak_ms = 881.0          # what the monitor wrote
    fake = types.SimpleNamespace(modules={"__main__": main})
    got = _load_helper(fake)()
    assert got is main, "the reader must resolve the RUNNING module"


def test_under_pytest_the_imported_module_wins():
    """Under pytest, __main__ is the test runner and has no such attribute;
    then the explicitly imported em_controller module is correct."""
    main = types.ModuleType("__main__")      # pytest's main: no attribute
    ctrl = types.ModuleType("em_controller")
    ctrl._loop_lag_peak_ms = 42.0
    fake = types.SimpleNamespace(modules={"__main__": main,
                                          "em_controller": ctrl})
    got = _load_helper(fake)()
    assert got is ctrl


def test_neither_present_returns_none_without_raising():
    fake = types.SimpleNamespace(modules={})
    got = _load_helper(fake)()
    assert got is None


def test_both_reader_sites_use_the_helper():
    """
    The bug lived in two call sites doing `import em_controller as _ctrl`.
    Neither may remain: both must go through the resolver.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    assert "import em_controller as _ctrl" not in src, \
        "importing by name loads a second, uninitialised module copy"
    assert src.count("_running_controller_module()") >= 2, \
        "status endpoint AND support bundle must resolve via the helper"


def test_every_auth_decorator_is_followed_by_the_function_it_decorates():
    """
    #309 review: a helper landed BETWEEN @auth.require_auth and
    _get_system_status — stealing the decorator, leaving the status endpoint
    unauthenticated and raising on call.

    The invariant is general, not about that one helper: EVERY
    @auth.require_* must be immediately followed by the handler it decorates,
    optionally after further decorators. A guard naming the helper passes for
    every other sandwich anyone writes next, which is the shape the original
    had — and an unauthenticated endpoint does not fail loudly, it serves.

    Matched per LINE with the decorator first on the line. em_api.py contains
    two PROSE mentions of these decorators, one of which is `Do NOT add
    @auth.require_admin here` — so a whole-file regex counts a comment saying
    the opposite of what it appears to say, and then asserts about the line
    after it. That trap has been hit three times in this tree.
    """
    lines = (CONTROLLER / "em_api.py").read_text().splitlines()
    decorator = re.compile(r"\s*@auth\.require_(\w+)\s*$")
    handler   = re.compile(r"\s*async def \w+\(\s*request\b")

    sites = 0
    for i, line in enumerate(lines):
        m = decorator.match(line)
        if not m:
            continue
        sites += 1
        j = i + 1
        while j < len(lines) and re.match(r"\s*@", lines[j]):
            j += 1   # stacked decorators are fine; keep walking
        following = lines[j] if j < len(lines) else "<end of file>"
        assert handler.match(following), (
            f"em_api.py:{i + 1}: @auth.require_{m.group(1)} is followed by "
            f"{following.strip()!r}, not the async handler it decorates — "
            f"whatever sits between them takes the decorator and the "
            f"endpoint is left unauthenticated"
        )

    # A guard that silently matches nothing is the failure this replaced.
    assert sites >= 40, \
        f"expected the full decorator surface, found only {sites} sites"
