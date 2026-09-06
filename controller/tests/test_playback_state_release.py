"""
`speaking` must be cleared on EVERY exit from _run_post_turn_playback.

Found live on 2026-08-28, EA 2.22.0-ea.2. A timer alarm dismissed with the dot
button left the dashboard showing Speaking, and then the device stopped
answering its wake word entirely — ring lighting on the device's own local
paint and expiring on TTL, with nothing in the log between the parked
`oww_wake` and silence.

One cause, three symptoms:

  stop_timer_alarm cancels the ring task -> CancelledError raised inside
  _run_post_turn_playback -> the `finally` runs (speaker_busy is decremented)
  -> the exception propagates -> `await device._set_speaking(False)`, which sat
  AFTER the try, is never reached.

`speaking` then stays set for the life of the process, and the wake listener's
guard is `if device.speaking and not ringing: continue` — so once the alarm
stops ringing the device is deaf, permanently, and never even reaches
model.predict().

The chime occupies 1.68s out of every 2.3s (em_timers), so about three
dismissals in four land mid-burst. That is why it presents as intermittent.

This is an AST check rather than a text search: a grep for the call finds the
comment explaining it and passes anyway, which has now happened five times in
this suite (see the source-guard notes in test_deploy.py).
"""

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in em_controller.py — renamed?")


def _calls_in(nodes) -> list[str]:
    """Dotted names of every call appearing anywhere under `nodes`."""
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                out.append(sub.func.attr)
    return out


def _try_blocks(fn) -> list[ast.Try]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]


def test_speaking_is_cleared_in_a_finally():
    """
    The clear must be reachable when the coroutine is cancelled, which means
    inside a finally. Anywhere else and a dismissed alarm leaves the device
    deaf until the controller restarts.
    """
    fn = _function("_run_post_turn_playback")

    in_finally = any(
        "_set_speaking" in _calls_in(t.finalbody) for t in _try_blocks(fn)
    )
    assert in_finally, (
        "_run_post_turn_playback must clear `speaking` from a finally. "
        "Cancellation skips anything after the try, and the wake listener "
        "goes deaf while the flag is set."
    )


def test_speaker_busy_is_cleared_in_a_finally():
    """The sibling invariant, and the one that was already right."""
    fn = _function("_run_post_turn_playback")

    released = False
    for t in _try_blocks(fn):
        for n in t.finalbody:
            for sub in ast.walk(n):
                if (isinstance(sub, ast.AugAssign)
                        and isinstance(sub.target, ast.Attribute)
                        and sub.target.attr == "speaker_busy"):
                    released = True
    assert released, (
        "speaker_busy must be decremented from a finally — a leaked count "
        "blocks the timer ring for the life of the process."
    )


def test_the_clear_survives_the_cancellation_that_triggered_it():
    """
    Awaiting plainly in a finally during cancellation can be interrupted
    before it completes. The clear is shielded so it still runs, and the
    shield must be the thing wrapping the _set_speaking call rather than
    something else in the same block.
    """
    fn = _function("_run_post_turn_playback")

    shielded = False
    for t in _try_blocks(fn):
        for n in t.finalbody:
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "shield"
                        and "_set_speaking" in _calls_in(sub.args)):
                    shielded = True
    assert shielded, (
        "the _set_speaking(False) in the finally must be wrapped in "
        "asyncio.shield, or the cancellation that caused it can interrupt "
        "the clear as well."
    )


def test_the_wake_listener_still_gates_on_speaking():
    """
    Pins the other half of the pair. If this guard is ever removed, the bug
    above stops being fatal and the tests above stop protecting anything — so
    a future reader should be told they moved together, not discover it.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        if "speaking" in names and any(
            isinstance(b, ast.Continue) for b in node.body
        ):
            found = True
    assert found, (
        "the wake listener no longer skips frames while `speaking` is set. "
        "If that is deliberate, test_speaking_is_cleared_in_a_finally is no "
        "longer protecting against a deaf device and its docstring is stale."
    )
