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
