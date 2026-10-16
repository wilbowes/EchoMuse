"""SIGTERM has to end the process, because the add-on is PID 1."""

import asyncio
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import em_db
import em_dbwriter
import em_shutdown

CONTROLLER = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _writer_left_open():
    yield
    em_dbwriter.unseal()


def test_sigterm_ends_a_waiting_loop():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import asyncio, em_shutdown\n"
         "async def main():\n"
         "    stop = asyncio.Event()\n"
         "    em_shutdown.install(asyncio.get_running_loop(), stop)\n"
         "    print('READY', flush=True)\n"
         "    await stop.wait()\n"
         "asyncio.run(main())\n"],
        cwd=str(CONTROLLER),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line.strip() == "READY", proc.stderr.read()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=5) == 0


def test_finish_stops_players_listeners_and_the_database():
    calls = []

    async def stop_players():
        calls.append("players")

    async def stop_ble():
        calls.append("ble")

    async def stop_voice():
        calls.append("voice")

    def close_db():
        calls.append("db")

    class Task:
        def cancel(self):
            calls.append("cancel")

    class Azc:
        async def async_unregister_service(self, info):
            calls.append("mdns")

        async def async_close(self):
            calls.append("mdns-close")

    class Runner:
        async def cleanup(self):
            calls.append("runner")

    asyncio.run(em_shutdown.finish(
        (Task(),), Azc(), object(), Runner(),
        deps=(stop_players, stop_ble, stop_voice, close_db),
    ))
    assert calls == [
        "players", "ble", "voice", "cancel", "mdns", "mdns-close",
        "runner", "db",
    ]


def test_the_handler_sets_the_stop_event_for_a_real_or_unknown_signal():
    stop = asyncio.Event()
    em_shutdown._on_signal(stop, signal.SIGTERM)
    assert stop.is_set()
    other = asyncio.Event()
    em_shutdown._on_signal(other, 99999)
    assert other.is_set()


def test_install_registers_on_a_running_loop_and_survives_a_refusal():
    seen = []

    class Loop:
        def add_signal_handler(self, sig, cb, *args):
            seen.append(sig)
            if sig == signal.SIGINT:
                raise RuntimeError("not the main thread")

    em_shutdown.install(Loop(), asyncio.Event())
    assert signal.SIGTERM in seen

    async def on_a_real_loop():
        loop = asyncio.get_running_loop()
        em_shutdown.install(loop, asyncio.Event())
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    asyncio.run(on_a_real_loop())


def test_finish_imports_the_real_stoppers(monkeypatch):
    import em_db
    monkeypatch.setattr(em_db, "close", lambda: None)

    class Azc:
        async def async_unregister_service(self, info):
            raise OSError("mdns down")

        async def async_close(self):
            pass

    class Runner:
        async def cleanup(self):
            pass

    asyncio.run(em_shutdown.finish((), Azc(), object(), Runner()))


def test_close_checkpoints_and_a_second_close_is_quiet(tmp_path):
    path = tmp_path / "em.db"
    em_db.init(str(path))
    em_db.close()
    assert em_db._conn is None
    em_db.close()
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        version = conn.execute(
            "SELECT value FROM system_config WHERE key = 'schema_version'"
        ).fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
    finally:
        conn.close()
    assert version == "31"
    assert "outcome" in cols


def test_finish_drains_the_writer_before_the_database_closes():
    src = (CONTROLLER / "em_shutdown.py").read_text()
    finish = src[src.index("async def finish"):]
    assert finish.index("await stop_voice()") < finish.index("em_dbwriter.drain(")
    assert finish.index("em_dbwriter.drain(") < finish.index("em_dbwriter.seal(")
    assert finish.index("em_dbwriter.seal(") < finish.index("close_db()")


def test_queued_writes_land_and_later_ones_drop_once(tmp_path, caplog):
    device_id = "DOTDRAIN00000001"
    gate = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        gate.wait(5)

    em_db.init(str(tmp_path / "em.db"))
    try:
        em_db.ensure_device_token(device_id)
        em_dbwriter.submit(block)
        assert started.wait(2)
        em_dbwriter.submit(
            em_db.log_device, device_id, "info", "controller",
            "queued-during-shutdown")
        assert em_dbwriter.drain(0.05) is False
        gate.set()
        assert em_dbwriter.drain(3) is True
        messages = [row["message"] for row in em_db.get_device_logs(device_id)]
        assert "queued-during-shutdown" in messages
        em_db.close()
        em_dbwriter.seal()
        caplog.set_level(logging.WARNING, logger="echomuse.dbwriter")
        caplog.clear()
        em_dbwriter.submit(
            em_db.log_device, device_id, "info", "controller", "after-close")
        em_dbwriter.submit(
            em_db.log_device, device_id, "info", "controller", "after-close-2")
        assert "db.init() has not been called" not in caplog.text
        dropped = [r for r in caplog.records if "dropping queued writes" in r.message]
        assert len(dropped) == 1
    finally:
        gate.set()
        em_dbwriter.unseal()
        try:
            em_dbwriter.drain(2)
        except Exception:
            pass
        em_db.close()
