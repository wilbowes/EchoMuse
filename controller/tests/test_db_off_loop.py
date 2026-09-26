"""
Database work stays off the event loop, and queued writes keep their order.

em_controller made 21 synchronous writes and a read from coroutines. Each
held the loop for the write plus any wait on em_db's lock, which executor
threads share: measured 2026-09-26 on the dev box with a 20,000-turn activity
read holding the lock, loop lateness reached 114ms p99 / 122ms max with the
writes on the loop, against 18ms / 42ms queued.

One of those writes was also a bug: the disconnect path's log_device raises
FOREIGN KEY constraint failed for a device just deleted, and raising inside
handle_control's `finally` skipped the `_devices.pop` and service release after
it. em_dbwriter.submit never raises.
"""

import ast
import logging
import threading
import time
from pathlib import Path

import em_dbwriter

CONTROLLER = Path(__file__).resolve().parents[1]


def _sync_db_calls_in_coroutines(path: Path, allow=()) -> list[str]:
    """db.<fn>(...) called directly in an async def's own body."""
    tree = ast.parse(path.read_text())
    found = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            # A nested def or lambda runs wherever it is called (usually an
            # executor); its body is not the coroutine's.
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.AsyncFunctionDef):
                visit(child, child)
                continue
            if (fn is not None and isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "db"
                    and child.func.attr not in allow):
                found.append(f"{path.name}:{child.lineno} db.{child.func.attr} in {fn.name}")
            visit(child, fn)

    visit(tree, None)
    return found


def test_em_controller_coroutines_make_no_synchronous_db_calls():
    # db.init runs once in main() before anything else is scheduled.
    found = _sync_db_calls_in_coroutines(CONTROLLER / "em_controller.py", allow={"init"})
    assert not found, (
        "synchronous database calls on the event loop:\n  " + "\n  ".join(found)
        + "\nQueue writes nothing reads back with em_dbwriter.submit; await "
          "reads with loop.run_in_executor.")


def test_the_guard_sees_a_synchronous_call(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        "async def f():\n"
        "    db.log_device(1)\n"
        "    await loop.run_in_executor(None, lambda: db.get(1))\n"
        "    em_dbwriter.submit(db.log_device, 1)\n"
        "    def inner():\n"
        "        db.write(1)\n")
    assert _sync_db_calls_in_coroutines(p) == ["m.py:2 db.log_device in f"]


def test_push_log_event_queues_its_write():
    tree = ast.parse((CONTROLLER / "em_api.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_push_log_event")
    submits = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", None) == "submit"
               and getattr(n.func.value, "id", None) == "em_dbwriter"]
    executors = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "run_in_executor"]
    assert submits and not executors


# ── em_dbwriter ─────────────────────────────────────────────────────────────

def _drain():
    em_dbwriter.submit(lambda: None).result(timeout=5)


def test_submit_returns_before_the_write_runs():
    gate = threading.Event()
    t = time.perf_counter()
    em_dbwriter.submit(gate.wait, 5)
    assert time.perf_counter() - t < 0.1
    gate.set()
    _drain()


def test_writes_run_in_submission_order_on_one_thread():
    order, threads = [], set()

    def write(i):
        time.sleep(0.001 * (i % 3))   # uneven work must not reorder
        order.append(i)
        threads.add(threading.get_ident())

    for i in range(50):
        em_dbwriter.submit(write, i)
    _drain()
    assert order == list(range(50))
    assert len(threads) == 1


def test_a_failed_write_is_logged_not_raised(caplog):
    def boom():
        raise RuntimeError("FOREIGN KEY constraint failed")

    with caplog.at_level(logging.ERROR, logger="echomuse.dbwriter"):
        em_dbwriter.submit(boom)      # must not raise here
        _drain()
    assert any("FOREIGN KEY" in r.getMessage() for r in caplog.records)

    done = []
    em_dbwriter.submit(done.append, 1)
    _drain()
    assert done == [1]                # and the writer carries on
