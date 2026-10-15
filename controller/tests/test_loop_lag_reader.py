"""
#306: /api/system/status and the support bundle reported loop_lag_peak_ms
as 0.0 while the log right next to them showed the loop stalling at 881ms.

Cause: em_start.py execvp's em_controller.py, so the running controller is
__main__. The readers did `import em_controller`, which does not find
__main__ under that name and loads a SECOND, never-initialised copy —
reading defaults instead of the live monitor's peak.

The peak now lives in em_state, which is only ever imported by name, so the
writer and both readers share one module object.
"""

import ast
import re
from pathlib import Path

import em_api
import em_state

CONTROLLER = Path(__file__).resolve().parents[1]


def test_the_bundle_reads_the_peak_the_monitor_wrote(monkeypatch):
    monkeypatch.setattr(em_state, "loop_lag_peak_ms", 881.04)
    assert em_api._controller_stats()["loop_lag_peak_ms"] == 881.0


def test_the_status_endpoint_reads_em_state():
    src = (CONTROLLER / "em_api.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_get_system_status")
    assert "em_state.loop_lag_peak_ms" in ast.unparse(fn)


def test_the_monitor_writes_em_state():
    src = (CONTROLLER / "em_controller.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "event_loop_lag_monitor")
    assert "em_state.loop_lag_peak_ms = " in ast.unparse(fn)


def test_no_module_imports_the_running_controller_by_name():
    """Importing em_controller by name is what loaded the second copy. Nothing
    the running controller imports may do it."""
    for path in CONTROLLER.glob("em_*.py"):
        if path.name == "em_controller.py":
            continue
        tree = ast.parse(path.read_text())
        # An import under `if TYPE_CHECKING:` never runs.
        typing_only = {id(n) for top in ast.walk(tree)
                       if isinstance(top, ast.If)
                       and getattr(top.test, "id", None) == "TYPE_CHECKING"
                       for n in ast.walk(top)}
        for node in ast.walk(tree):
            if id(node) in typing_only:
                continue
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert "em_controller" not in names, \
                f"{path.name} imports em_controller by name"


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
