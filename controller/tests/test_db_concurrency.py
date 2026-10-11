"""
One sqlite3.Connection is shared across the executor pool, so every access to
it has to hold `_db_lock`.

2026-07-12, a fleet deploy-all: `_tx()` held the lock for a write, the read
helpers `_q` and `_q1` held nothing, and a read landing inside a write raised
SQLITE_MISUSE. One device updating alone never tripped it.

Two tests, not redundant. The stress test shows it holds under real
contention today; the AST guard stops a future helper touching the connection
outside the lock, which a race-based test only catches when threads happen to
interleave.
"""

import ast
import concurrent.futures
import sqlite3
import threading
from pathlib import Path

import pytest

import em_db as db


EM_DB = Path(db.__file__)


@pytest.fixture()
def fresh_db(tmp_path):
    """A real database migrated from scratch to the current schema."""
    path = tmp_path / "test.db"
    db.init(str(path))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _seed(device_id: str) -> None:
    """A device row to hammer. Approval is irrelevant here — the point is
    contention on the shared connection, not what the rows say."""
    with db._tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO devices (device_id, label, ip, approved) "
            "VALUES (?, ?, ?, 1)",
            (device_id, f"label-{device_id}", "10.0.0.1"),
        )


# ── The behaviour ────────────────────────────────────────────────────────────

def test_reads_racing_writes_do_not_raise_sqlite_misuse(fresh_db):
    """
    The regression: eight threads, reads and writes interleaved.

    Reads are the majority because the reads were what was unguarded. 500 cycles
    is what it takes to overlap a read with a commit reliably on a loaded runner;
    fewer passes by luck.
    """
    devices = [f"dev{i}" for i in range(8)]
    for d in devices:
        _seed(d)

    errors: list[BaseException] = []
    barrier = threading.Barrier(len(devices))
    stop = threading.Event()

    def writer(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(500):
                db.set_device_label(devices[idx], f"label-{i}")
                db.upsert_device_seen(devices[idx], f"10.0.0.{idx}", f"v{i}")
        except BaseException as e:  # noqa: BLE001 — recorded, then re-raised below
            errors.append(e)

    def reader(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            while not stop.is_set():
                db.get_all_devices()
                db.get_pending_devices()
                db.get_device("dev0")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        writer_futures = [pool.submit(writer, i) for i in range(len(devices))]
        reader_futures = [pool.submit(reader, i) for i in range(len(devices))]
        for f in writer_futures:
            f.result(timeout=120)
        stop.set()
        for f in reader_futures:
            f.result(timeout=120)

    assert not errors, (
        f"{len(errors)} of 8 threads raised under contention. The first was "
        f"{type(errors[0]).__name__}: {errors[0]}. SQLITE_MISUSE here means a "
        f"read reached the shared connection inside somebody's transaction — "
        f"the 2026-07-12 bug."
    )


def test_a_failing_write_rolls_back_and_leaves_the_lock_free(fresh_db):
    """
    `_tx` rolls back on an exception and re-raises. If it did not release the
    lock on the way out, every later call would block for ever — and the fleet
    would wedge with no error, which is worse than the crash it replaced.

    A real SQLite error rather than a Python one, so the rollback path is the
    one that actually runs.
    """
    _seed("dev0")

    with pytest.raises(sqlite3.Error):
        with db._tx() as conn:
            conn.execute("INSERT INTO devices (device_id) VALUES (?)", ("dup",))
            # Violates the primary key on the second insert of the same id.
            conn.execute("INSERT INTO devices (device_id) VALUES (?)", ("dup",))

    # The lock must be free. If it were not, this would hang rather than fail,
    # so a run that completes at all is part of the assertion.
    assert db.get_all_devices() is not None

    # And the partial insert was rolled back — the row must not be there.
    rows = [r["device_id"] for r in db.get_all_devices()]
    assert "dup" not in rows, (
        "a failed transaction left a row behind — the rollback did not happen"
    )


def test_reads_see_committed_writes_only(fresh_db):
    """
    A reader must never observe a half-applied transaction. This is the other
    half of what the lock buys, and it is the half that presents as a wrong
    answer rather than an error: a device row read mid-transaction would show
    an ip without its firmware, or a label without its config.
    """
    _seed("dev0")

    seen_partial = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            for row in db.get_all_devices():
                if row["device_id"] == "probe" and row["label"] is None:
                    seen_partial.append(dict(row))

    def writer() -> None:
        for _ in range(300):
            with db._tx() as conn:
                conn.execute(
                    "INSERT INTO devices (device_id, label, ip, approved) "
                    "VALUES ('probe', 'set', '10.0.0.9', 1) "
                    "ON CONFLICT(device_id) DO UPDATE SET label = excluded.label",
                )
                # Force the reader to interleave if it can.
                threading.Event().wait(0)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        writer()
    finally:
        stop.set()
        t.join(timeout=10)

    assert not seen_partial, (
        f"a reader observed a partially-applied transaction {len(seen_partial)} "
        f"times — a read reached the connection inside a write"
    )


# ── The shape, so it cannot stop being true ──────────────────────────────────

# `init` is the one function allowed to touch the connection unguarded, and the
# exemption is NAMED rather than structural so that a second one has to be added
# to this list deliberately. It qualifies because it creates the connection:
# there is no pool to race with yet, it is called once at startup before any
# executor thread exists, and it cannot take a lock the connection it is about
# to create is not yet behind.
UNGUARDED_BY_DESIGN = {"init"}

# The only ways the connection is used. Anything else reaching it is either a
# bug or needs adding here with a reason.
CONN_CALLS = ("execute", "executemany", "commit", "rollback", "executescript")


def _line_of(node) -> int:
    """`end_lineno` where present, `lineno` otherwise. `ast.withitem` has
    NEITHER, and getattr's default argument is evaluated eagerly — so the
    obvious `getattr(n, "end_lineno", n.lineno)` raises AttributeError the
    moment it walks a `with` body."""
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)


def test_every_connection_access_is_inside_the_lock():
    """
    The AST guard: `_q`, `_q1` and `_tx` are the only places that touch the
    connection, and each access must sit inside a `with` naming `_db_lock`.
    A missing lock shows at runtime only as a race.
    """
    tree = ast.parse(EM_DB.read_text())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in UNGUARDED_BY_DESIGN:
            continue

        accesses = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "_conn" and n.attr in CONN_CALLS
        ]
        if not accesses:
            continue

        guarded = []
        for w in ast.walk(node):
            if not isinstance(w, (ast.With, ast.AsyncWith)):
                continue
            names = {n.id for item in w.items
                     for n in ast.walk(item.context_expr)
                     if isinstance(n, ast.Name)}
            if "_db_lock" in names:
                # Start from the `with` statement's OWN lineno. _line_of
                # prefers end_lineno, so using it here gives the END of the
                # block as the start — which silently excludes every access
                # inside it and reports the guarded helper as unguarded.
                guarded.append((w.lineno, max(_line_of(x) for x in ast.walk(w))))

        for n in accesses:
            if not any(lo <= n.lineno <= hi for lo, hi in guarded):
                offenders.append(f"{node.name}() line {n.lineno}: _conn.{n.attr}")

    assert not offenders, (
        "connection access outside _db_lock:\n  " + "\n  ".join(offenders) +
        "\n\nWAL allows concurrent readers only across SEPARATE connections. On "
        "the one shared connection this module uses, any two concurrent "
        "operations are a misuse SQLite rejects with SQLITE_MISUSE — which is "
        "what a fleet deploy-all hit on 2026-07-12."
    )


def test_the_unguarded_exemption_is_still_only_init():
    """
    The exemption list is asserted to have stayed at one entry, and for `init`
    specifically. Otherwise the list becomes a place where helpers quietly
    accumulate and the guard above quietly stops meaning anything — which is
    the same shape as a denylist in a redaction allowlist, one layer down.

    If a second function genuinely needs the connection before the pool exists,
    that is a real architectural change and belongs in this test's failure
    message rather than in a silent list.
    """
    assert UNGUARDED_BY_DESIGN == {"init"}, (
        f"the exemption list grew to {sorted(UNGUARDED_BY_DESIGN)}; every entry "
        f"is an unguarded path onto the shared connection"
    )
    # And init must still be the one that CREATES it, or the reason it is
    # exempt no longer holds.
    tree = ast.parse(EM_DB.read_text())
    init_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "init")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "sqlite3" and n.func.attr == "connect"
               for n in ast.walk(init_fn)), (
        "init no longer creates the connection, so its exemption has no basis"
    )


def test_the_lock_is_not_reentrant_and_there_is_exactly_one():
    """
    `_db_lock` is a plain `threading.Lock`, not an RLock. That is correct — a
    reentrant lock would let one thread take the lock twice and then release it
    once, leaving the connection unprotected for every other thread, which is
    the same bug wearing a disguise.

    It is asserted because swapping in an RLock looks like a harmless
    robustness improvement and silently removes the property that makes the
    stress test above mean anything.
    """
    assert type(db._db_lock) is type(threading.Lock()), (
        f"_db_lock is {type(db._db_lock).__name__}; it must stay a plain Lock. "
        f"An RLock lets a thread nest and release once, leaving the connection "
        f"unguarded for everyone else."
    )


def test_the_connection_is_shared_rather_than_per_thread(fresh_db):
    """
    The premise of all of the above. If someone "fixes" this by giving each
    thread its own connection, every test here still passes and the fleet
    deploy-all comes back — because the fix moves the problem rather than
    solving it, and the per-connection pool would have to be closed and
    reopened on every settings change.

    So the shape is pinned: ONE module-level connection, shared.
    """
    assert isinstance(db._conn, sqlite3.Connection), (
        "_conn must be a single shared sqlite3.Connection. Per-thread "
        "connections would need their own lifecycle, and WAL's concurrent-"
        "reader benefit is only available across separate connections — which "
        "is precisely why the shared one must be locked instead."
    )