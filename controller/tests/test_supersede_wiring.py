"""
#506: the supersede is reached from the question path, and the teardown
invariant it must not weaken is still there.

em_supersede's own tests cover the decision. These cover the two things a
pure test cannot see — that the decision is consulted at all, and that the
guard #333 added is still in place. em_esphome is not importable by the CI
suite, so this reads the source; asked structurally, because a text window
here is a claim about formatting (the same lesson as #754's tests).
"""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CONTROLLER = Path(__file__).resolve().parents[1]


def _nodes(stmt):
    yield stmt
    for _, val in ast.iter_fields(stmt):
        for node in (val if isinstance(val, list) else (val,)):
            if isinstance(node, ast.AST):
                yield from _nodes(node)


def _dump(stmt) -> str:
    return " ".join(ast.dump(n) for n in _nodes(stmt))


def _func(name: str) -> ast.AST:
    tree = ast.parse((CONTROLLER / "em_esphome.py").read_text())
    for n in _nodes(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in em_esphome.py")


def test_a_question_inside_a_turn_reaches_the_supersede():
    body = _dump(_func("_start_conversation_turn"))
    assert "em_supersede" in body and "decide" in body, (
        "_start_conversation_turn must consult em_supersede.decide — without "
        "it the question queues behind the very turn that cannot progress "
        "until the question is answered (#506)"
    )


def test_the_supersede_runs_before_the_question_starts():
    fn = _func("_start_conversation_turn")
    body = _dump(fn)
    # The callback is fetched by name — it is a string on the owning server,
    # not an attribute — so match the constant, and the LAST one, which is
    # the call rather than the getattr that found it.
    asked = body.rindex("Constant(value='_start_conversation')")
    assert body.index("decide") < asked, (
        "the supersede must come before the question's turn is requested, or "
        "the voice lock is still held when it asks"
    )


def test_the_supersede_cancels_rather_than_only_aborting():
    """
    Two halves and both are needed. `cancel_turn` sets _turn_cancelled and
    releases the voice lock, which is what unblocks the question;
    `abort_ha_run` alone leaves the turn waiting on a TTS that will never
    arrive, which is the deadlock.

    And it must be cancel_turn and NOT abort_ha_run, because abort_ha_run
    defaults _turn_end_reason to "barged" — and nobody here spoke over the
    assistant. The reason comes from the verdict for that reason.
    """
    body = _dump(_func("_start_conversation_turn"))
    assert "cancel_turn" in body, (
        "the supersede must go through cancel_turn — abort_ha_run alone "
        "leaves the turn blocked on the TTS wait that caused the deadlock"
    )
    assert "attr='abort_ha_run'" not in body, (
        "abort_ha_run defaults _turn_end_reason to 'barged'; a superseded "
        "turn was not barged, and the dashboard groups wake statistics by "
        "trigger prefix"
    )
    assert "reason" in body, "the recorded reason must come from the verdict"


def test_the_teardown_guard_is_still_there():
    """
    #333: if a turn ends while HA's run is live, abort it. This is the
    invariant that covers returns nobody has written yet, and the supersede
    sits on a path that ends turns — it must not have been traded away.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    tree = ast.parse(src)
    found = False
    for n in _nodes(tree):
        if isinstance(n, ast.If):
            t = _dump(n.test)
            if ("_run_started" in t and "_run_finished" in t
                    and ast.unparse(n.test).startswith("self._run_started")):
                found = True
                break
    assert found, (
        "the `if self._run_started and not self._run_finished: end_ha_run()` "
        "teardown guard (#333) is gone — it is what stops a stale run's tail "
        "reaching the next turn"
    )
