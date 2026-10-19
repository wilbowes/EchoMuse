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

At shutdown `drain` waits for what is queued and `seal` drops what comes
after, so the database can close under a writer that has finished (#889,
from @Dutch365's #968).
"""

from __future__ import annotations

import concurrent.futures
import logging

log = logging.getLogger("echomuse.dbwriter")

_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="em-dbwrite")


_sealed = False
_warned = False


def submit(fn, *args, **kwargs) -> concurrent.futures.Future:
    """Queue `fn(*args, **kwargs)` on the writer thread; never blocks."""
    if _sealed:
        global _warned
        if not _warned:
            _warned = True
            log.warning("[db] closed — dropping writes queued after shutdown")
        fut = concurrent.futures.Future()
        fut.set_result(None)
        return fut
    fut = _pool.submit(fn, *args, **kwargs)
    fut.add_done_callback(_report)
    return fut


def _report(fut: concurrent.futures.Future) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.error(f"[db] queued write failed: {exc!r}")


def drain(timeout: float = 3.0) -> bool:
    """Block until everything queued so far is written. False on timeout.

    A no-op on the same single thread runs after all of it. Never call this
    from the writer thread: it would wait for itself.
    """
    try:
        _pool.submit(lambda: None).result(timeout=timeout)
        return True
    except concurrent.futures.TimeoutError:
        log.warning(f"[db] writes still queued after {timeout:g}s at shutdown")
        return False


def seal() -> None:
    """Drop every later write. Work already queued still runs."""
    global _sealed
    _sealed = True


def unseal() -> None:
    """For tests: a live process seals once."""
    global _sealed, _warned
    _sealed = _warned = False
