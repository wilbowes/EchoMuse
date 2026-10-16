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

# Set once the database is closing. Later submit() calls drop, and the
# warning is logged once rather than once per write.
_sealed = False
_warned = False


def submit(fn, *args, **kwargs) -> concurrent.futures.Future:
    """Queue `fn(*args, **kwargs)` on the writer thread; never blocks."""
    if _sealed:
        global _warned
        if not _warned:
            _warned = True
            log.warning("[db] closed — dropping queued writes")
        fut = concurrent.futures.Future()
        fut.set_result(None)
        return fut
    fut = _pool.submit(fn, *args, **kwargs)
    fut.add_done_callback(_report)
    return fut


def drain(timeout: float = 3.0) -> bool:
    """Block until work queued so far has finished. False on timeout.

    A sentinel on the same single thread runs after everything submit()
    queued before this call. Do not call it from the writer thread: the
    sentinel would wait behind the caller. Shutdown calls it on the loop,
    after the voice servers have stopped and before the database closes.
    """
    if _sealed:
        return True
    fut = _pool.submit(lambda: None)
    try:
        fut.result(timeout=timeout)
        return True
    except concurrent.futures.TimeoutError:
        log.warning("[db] drain timed out with writes still queued")
        return False


def seal() -> None:
    """Drop later writes. Idempotent. Queued work already in the pool still runs."""
    global _sealed
    _sealed = True


def unseal() -> None:
    """Tests, and db.init(), put the writer back. A live process seals once."""
    global _sealed, _warned
    _sealed = False
    _warned = False


def _report(fut: concurrent.futures.Future) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.error(f"[db] queued write failed: {exc!r}")
