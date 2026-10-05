"""
Background tasks that nothing awaits.

`asyncio.create_task` returns a task the event loop holds only weakly, so one
whose result is discarded can be garbage-collected before it finishes (the
asyncio docs say to keep a reference), and an exception it raises is reported
only when it is collected, as "Task exception was never retrieved", long after
and far from the cause. `spawn` keeps each task in a set until it is done and
logs its exception at the moment it happens.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("echomuse.tasks")

_live: set[asyncio.Task] = set()


def spawn(coro, *, name: str | None = None) -> asyncio.Task:
    """Start `coro` as a task held until it finishes. Returns the task."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _live.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task) -> None:
    _live.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"background task {task.get_name()} failed: {exc!r}",
                  exc_info=exc)


def live_count() -> int:
    """Tasks started with spawn and not yet finished."""
    return len(_live)
