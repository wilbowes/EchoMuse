"""
Background tasks are held until they finish, and helper waits are torn down.

Two shapes of the same fault. A task whose result is discarded is held only
weakly by the event loop, so it can be collected mid-run and its exception is
reported late and far from the cause (em_tasks). And a task created to race an
Event.wait() and then abandoned stays pending: `_run_post_turn_playback`
cancelled its helpers at the end of its try instead of in its finally, so a
cancelled playback left them behind, seen on the dev add-on on 2026-09-25 as
"Task was destroyed but it is pending!" with coro=<Event.wait()>.
"""

import ast
import asyncio
import gc
import logging
from pathlib import Path

import em_tasks

CONTROLLER = Path(__file__).resolve().parents[1]


# ── em_tasks.spawn ──────────────────────────────────────────────────────────

def test_a_spawned_task_survives_garbage_collection():
    async def main():
        gate = asyncio.Event()
        done = []

        async def work():
            await gate.wait()
            done.append(True)

        em_tasks.spawn(work())      # reference discarded on purpose
        await asyncio.sleep(0)
        gc.collect()
        gate.set()
        await asyncio.sleep(0.01)
        return done, em_tasks.live_count()

    done, live = asyncio.run(main())
    assert done == [True]
    assert live == 0                # released once finished


def test_a_failure_is_logged_when_it_happens(caplog):
    async def main():
        async def boom():
            raise RuntimeError("shell plane gone")
        em_tasks.spawn(boom(), name="reconcile")
        await asyncio.sleep(0.01)

    with caplog.at_level(logging.ERROR, logger="echomuse.tasks"):
        asyncio.run(main())
    msgs = [r.getMessage() for r in caplog.records]
    assert any("reconcile" in m and "shell plane gone" in m for m in msgs)


def test_cancellation_is_not_reported_as_failure(caplog):
    async def main():
        t = em_tasks.spawn(asyncio.sleep(10))
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.sleep(0.01)

    with caplog.at_level(logging.ERROR, logger="echomuse.tasks"):
        asyncio.run(main())
    assert not caplog.records


# ── no discarded tasks in the controller ────────────────────────────────────

MODULES = ["em_api.py", "em_controller.py", "em_esphome.py"]


def _discarded_tasks(path: Path) -> list[str]:
    """asyncio.create_task / ensure_future whose task is thrown away."""
    src = path.read_text()
    tree = ast.parse(src)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("create_task", "ensure_future")):
            continue
        if ast.get_source_segment(src, n.func.value) not in ("asyncio", "asyncio.get_event_loop()"):
            continue
        p = parents[n]
        chained = (isinstance(p, ast.Attribute) and isinstance(parents[p], ast.Call)
                   and isinstance(parents[parents[p]], ast.Expr))
        if isinstance(p, ast.Expr) or chained:
            out.append(f"{path.name}:{n.lineno}")
    return out


def test_no_task_is_created_and_discarded():
    found = [x for m in MODULES for x in _discarded_tasks(CONTROLLER / m)]
    assert not found, (
        "tasks created and thrown away (use em_tasks.spawn): " + ", ".join(found))


def test_the_discard_check_sees_both_shapes(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("async def f():\n"
                 "    asyncio.create_task(g())\n"
                 "    asyncio.create_task(g()).add_done_callback(h)\n"
                 "    t = asyncio.create_task(g())\n")
    assert _discarded_tasks(p) == ["m.py:2", "m.py:3"]


# ── helper waits are torn down on every exit ────────────────────────────────

WAIT_MODULES = ["em_controller.py", "em_esphome.py", "em_earlytts.py"]


def _unfinalised_waits(path: Path) -> list[str]:
    """
    Tasks wrapping `<x>.wait()` whose name never appears in a finally of the
    function that created them.
    """
    tree = ast.parse(path.read_text())
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        waits = {}
        for n in ast.walk(fn):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "attr", None) in ("create_task", "ensure_future")
                    and n.value.args and isinstance(n.value.args[0], ast.Call)
                    and getattr(n.value.args[0].func, "attr", None) == "wait"):
                waits[n.targets[0].id] = n.lineno
        if not waits:
            continue
        in_finally = {x.id for t in ast.walk(fn) if isinstance(t, ast.Try)
                      for stmt in t.finalbody for x in ast.walk(stmt)
                      if isinstance(x, ast.Name)}
        out += [f"{path.name}:{line} {name} in {fn.name}"
                for name, line in waits.items() if name not in in_finally]
    return out


def test_every_wait_task_is_torn_down_in_a_finally():
    found = [x for m in WAIT_MODULES for x in _unfinalised_waits(CONTROLLER / m)]
    assert not found, ("Event.wait() tasks not cancelled in a finally: "
                       + ", ".join(found))


def test_the_wait_check_sees_a_cancel_outside_the_finally(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("async def f(ev):\n"
                 "    t = asyncio.create_task(ev.wait())\n"
                 "    try:\n"
                 "        await g()\n"
                 "        t.cancel()\n"
                 "    finally:\n"
                 "        pass\n")
    assert _unfinalised_waits(p) == ["m.py:2 t in f"]
