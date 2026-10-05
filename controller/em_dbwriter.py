"""
Database writes nothing waits for, off the event loop and in order.

em_controller made synchronous `db.*` writes from coroutines — a device log
line on every wake, the base OS and kernel at registration, last-seen at
disconnect. Each one is a transaction with a commit and a prune, behind
em_db's single `_db_lock`, which the executor threads share. So the write cost
the loop its own time, plus however long a thread held the lock for a slow
read (an activity query, a support bundle): wake handling, speaker frames and
pings all waited on it.

Awaiting each write in the default executor would free the loop but still hold
up the coroutine that logged, and writes spread across the pool's threads can
commit out of order. So writes nothing reads back go to ONE worker thread:
`submit` returns immediately and the writes happen in the order submitted.
Reads, and writes whose result the caller needs, still use run_in_executor.

A failed write is logged, never raised: the caller has moved on, and a log line
that could not be stored is not a reason to fail the turn that produced it.
"""

from __future__ import annotations

import concurrent.futures
import logging

log = logging.getLogger("echomuse.dbwriter")

_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="em-dbwrite")


def submit(fn, *args, **kwargs) -> concurrent.futures.Future:
    """Queue `fn(*args, **kwargs)` on the writer thread; never blocks."""
    fut = _pool.submit(fn, *args, **kwargs)
    fut.add_done_callback(_report)
    return fut


def _report(fut: concurrent.futures.Future) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.error(f"[db] queued write failed: {exc!r}")
