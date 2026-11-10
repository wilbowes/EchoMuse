"""
SIGTERM stops the controller cleanly (#1073).

em_start execs em_controller, so in a container it is PID 1, and the kernel
drops any signal PID 1 has no handler for: `docker stop` waited out its grace
period and SIGKILLed it, and the database was never closed. These run the real
controller as a process, because a handler can only be proven by a signal.
"""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import em_db as db
import em_dbwriter

CONTROLLER = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(proc, text: str, timeout: float) -> str:
    """Read the controller's output until `text` appears or it exits."""
    out = []
    deadline = time.monotonic() + timeout
    os.set_blocking(proc.stdout.fileno(), False)
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            out.append(line)
            if text in line:
                return "".join(out)
        elif proc.poll() is not None:
            break
        else:
            time.sleep(0.05)
    pytest.fail(f"no {text!r} within {timeout}s:\n" + "".join(out))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_runs_the_teardown_and_closes_the_database(tmp_path):
    env = dict(os.environ,
               DB_PATH=str(tmp_path / "echomuse.db"),
               SERVER_HOST="127.0.0.1", SERVER_IP="127.0.0.1",
               SERVER_PORT=str(_free_port()), SERVER_TLS_PORT="0",
               API_PORT=str(_free_port()),
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "em_controller.py"], cwd=CONTROLLER,
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for(proc, "waiting for devices", 60)
        proc.send_signal(signal.SIGTERM)
        out = _wait_for(proc, "EchoMuse Controller stopped", 8)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert "Database closed" in out
    wal = tmp_path / "echomuse.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0, \
        "the WAL was not folded back into echomuse.db"


def test_close_folds_the_wal_in_and_is_safe_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    path = tmp_path / "em.db"
    db.init(str(path))
    with db._tx() as c:
        c.execute("INSERT INTO system_config(key, value) VALUES ('k', 'v')")
    assert (tmp_path / "em.db-wal").stat().st_size > 0
    db.close()
    db.close()
    wal = tmp_path / "em.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    row = sqlite3.connect(path).execute(
        "SELECT value FROM system_config WHERE key = 'k'").fetchone()
    assert row == ("v",)
    with pytest.raises(AssertionError):
        db._q("SELECT 1")


def test_drain_runs_every_queued_write_first(monkeypatch):
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(em_dbwriter, "_pool", pool)
    done = []
    for i in range(5):
        em_dbwriter.submit(lambda i=i: (time.sleep(0.02), done.append(i)))
    em_dbwriter.drain()
    assert done == [0, 1, 2, 3, 4]
    with pytest.raises(RuntimeError):
        em_dbwriter.submit(done.append, 5)
