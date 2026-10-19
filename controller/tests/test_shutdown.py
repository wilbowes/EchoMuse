"""A stop signal must end main(), and a stuck cleanup must not outlive it.

Before #889 neither signal had a handler and the controller is PID 1, so the
kernel dropped SIGTERM and every stop ended in SIGKILL after 10 seconds.
"""
import asyncio
import logging
import os
import signal
import threading

import pytest

import em_shutdown


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_resolves_the_future_with_its_name(sig):
    async def run():
        stop = em_shutdown.install(asyncio.get_running_loop())
        os.kill(os.getpid(), sig)
        return await asyncio.wait_for(stop, timeout=2)

    assert asyncio.run(run()) == sig.name


def test_a_second_signal_is_ignored():
    # Cleanup is running by then; a repeat must not raise InvalidStateError
    # inside the loop's signal handling.
    async def run():
        stop = em_shutdown.install(asyncio.get_running_loop())
        os.kill(os.getpid(), signal.SIGTERM)
        first = await asyncio.wait_for(stop, timeout=2)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.05)
        return first

    assert asyncio.run(run()) == "SIGTERM"


def test_both_signals_are_handled():
    # SIGTERM is what docker and Supervisor send; SIGINT is Ctrl-C under
    # compose. Dropping either from the default is the regression.
    assert set(em_shutdown.SIGNALS) == {signal.SIGTERM, signal.SIGINT}


def test_the_budget_is_inside_the_ten_second_grace_period():
    assert 0 < em_shutdown.BUDGET_S < 10


def test_the_backstop_exits_non_zero_when_cleanup_overruns():
    exited = threading.Event()
    codes = []

    def fake_exit(code):
        codes.append(code)
        exited.set()

    em_shutdown.backstop(logging.getLogger("test"), budget_s=0.05, _exit=fake_exit)
    assert exited.wait(2), "the backstop never fired"
    assert codes == [1]


def test_a_cancelled_backstop_does_not_exit():
    codes = []
    timer = em_shutdown.backstop(logging.getLogger("test"), budget_s=0.05,
                                 _exit=codes.append)
    timer.cancel()
    timer.join(1)
    assert codes == []
    assert timer.daemon


# The database closes last, after the writer has finished (#968, @Dutch365).

@pytest.fixture
def writer():
    import em_dbwriter
    yield em_dbwriter
    em_dbwriter.unseal()


def test_drain_waits_for_writes_already_queued(writer):
    done = []
    gate = threading.Event()
    writer.submit(lambda: (gate.wait(1), done.append(1)))
    threading.Timer(0.1, gate.set).start()
    assert writer.drain(timeout=2)
    assert done == [1]


def test_drain_gives_up_on_a_stuck_writer(writer):
    gate = threading.Event()
    writer.submit(gate.wait, 2)
    try:
        assert not writer.drain(timeout=0.1)
    finally:
        gate.set()
        writer.drain(timeout=2)


def test_a_sealed_writer_drops_later_writes(writer, caplog):
    ran = []
    writer.seal()
    with caplog.at_level(logging.WARNING, logger="echomuse.dbwriter"):
        writer.submit(ran.append, 1)
        writer.submit(ran.append, 2)
    assert writer.drain(timeout=2)
    assert ran == []
    assert sum("dropping" in r.message for r in caplog.records) == 1


def test_close_checkpoints_and_can_be_called_twice(tmp_path):
    import em_db as db
    path = tmp_path / "t.db"
    db.init(str(path))
    db.close()
    db.close()
    # TRUNCATE leaves an empty WAL, so nothing is waiting to be replayed.
    wal = tmp_path / "t.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_the_database_closes_after_the_writer_drains(monkeypatch):
    import em_db
    import em_dbwriter
    calls = []
    monkeypatch.setattr(em_dbwriter, "drain", lambda: calls.append("drain"))
    monkeypatch.setattr(em_dbwriter, "seal", lambda: calls.append("seal"))
    monkeypatch.setattr(em_db, "close", lambda: calls.append("close"))
    asyncio.run(em_shutdown.close_database())
    assert calls == ["drain", "seal", "close"]
