"""
No statements after a return, anywhere in the controller.

Written because two attribute initialisers sat after `return out` inside
Device.drain_rtt() for months. They never ran, so Device.__init__ never
defined playback_send_ms — the attribute existed only once a SUCCESSFUL
playback had assigned it. A barge-in cancel skips that assignment, so the
next playback_stats message raised AttributeError inside the control-plane
handler and took the whole device connection down with it. Measured
2026-08-20 as a device disconnecting and reconnecting on every barge-in.

Nothing caught it. It is valid Python, pyflakes does not report unreachable
code, and the test suite cannot import em_controller at all — which is
precisely where it was hiding. An AST walk costs nothing and covers the
whole class rather than the one instance.
"""

import ast
import pathlib

import pytest

CONTROLLER = pathlib.Path(__file__).resolve().parent.parent

# Every module, not just the ones the suite can import — the unimportable
# ones are exactly where this hides.
MODULES = sorted(p for p in CONTROLLER.glob("em_*.py"))


def _unreachable(tree: ast.AST) -> list[tuple[str, int]]:
    """(description, lineno) for any statement that can never execute."""
    found = []
    TERMINAL = (ast.Return, ast.Raise, ast.Continue, ast.Break)
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, TERMINAL):
                    nxt = block[i + 1]
                    found.append(
                        (f"{type(stmt).__name__.lower()} on line "
                         f"{stmt.lineno} is followed by unreachable code",
                         nxt.lineno)
                    )
    return found


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_unreachable_statements(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    dead = _unreachable(tree)
    assert not dead, (
        f"{path.name} has code that can never run:\n"
        + "\n".join(f"  line {ln}: {why}" for why, ln in dead)
        + "\n\nIf it is an initialiser it is not initialising anything — that "
          "is how Device.playback_send_ms came to be missing."
    )


def test_the_detector_actually_detects():
    """A guard that cannot fail is not a guard."""
    tree = ast.parse("def f():\n    return 1\n    x = 2\n")
    assert _unreachable(tree)
    tree = ast.parse("def f():\n    x = 2\n    return 1\n")
    assert not _unreachable(tree)
