"""
A private turn releases the beam lock where it closes the session.

The lock is for capturing the command. Held through the reply, a private
Echo heard barge-ins through whichever edge mic the turn had locked, and when
that was the far side the voice stayed under the echo: 0 of 4 barges against
4 of 4 near side (15LE, 2026-11-06). Firmware since then unlocks when its
session closes; this keeps older firmware right.
"""
import ast

from _sources import MODULES, tree

TURN_CLOSES = {"end_of_speech", "playback"}


def _closes():
    """Every statement list holding an awaited listen_close(reason=...) for a
    turn's own close, with the index of that statement."""
    for path in MODULES:
        for node in ast.walk(tree(path)):
            for field in ("body", "orelse"):
                stmts = getattr(node, field, None)
                if not isinstance(stmts, list):
                    continue
                for i, st in enumerate(stmts):
                    call = st.value.value if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Await)) else None
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "listen_close"):
                        continue
                    reason = next((k.value.value for k in call.keywords
                                   if k.arg == "reason" and isinstance(k.value, ast.Constant)), None)
                    if reason in TURN_CLOSES:
                        yield path, stmts, i, reason


def test_every_turn_close_is_followed_by_beam_unlock():
    found = list(_closes())
    assert {r for *_, r in found} == TURN_CLOSES, "a turn close this guard covers has gone"
    for path, stmts, i, reason in found:
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        ok = (isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Await)
              and isinstance(nxt.value.value, ast.Call)
              and isinstance(nxt.value.value.func, ast.Attribute)
              and nxt.value.value.func.attr == "beam_unlock")
        assert ok, f"{path.name}:{stmts[i].lineno} listen_close({reason!r}) is not followed by beam_unlock()"
