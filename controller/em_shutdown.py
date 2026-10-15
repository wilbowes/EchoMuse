"""Stopping when asked (#889).

The controller is PID 1 in its container (`init: false`, and em_start.py
`exec`s it), and Linux does not apply a signal's default action to PID 1. With
no handler installed, SIGTERM was dropped: every `docker stop` and every
add-on stop waited out the 10 second grace period and ended in SIGKILL, with
nothing closed, and a start inside those 10 seconds failed on a port still
held.

`install` turns SIGTERM and SIGINT into a result `main()` awaits, so its
existing `finally` runs. `backstop` is the limit on that: cleanup that hangs
must not put the SIGKILL back.
"""
import asyncio
import os
import signal
import threading

SIGNALS = (signal.SIGTERM, signal.SIGINT)

# Under docker's and Supervisor's 10 second grace period, with room to exit.
BUDGET_S = 8.0


def install(loop: asyncio.AbstractEventLoop, signals=SIGNALS) -> asyncio.Future:
    """A future that resolves to the name of the first stop signal received.

    A second signal is ignored: cleanup is already running and `backstop`
    bounds it.
    """
    stop: asyncio.Future = loop.create_future()

    def on_signal(sig: signal.Signals) -> None:
        if not stop.done():
            stop.set_result(sig.name)

    for sig in signals:
        loop.add_signal_handler(sig, on_signal, sig)
    return stop


def backstop(log, budget_s: float = BUDGET_S, _exit=os._exit) -> threading.Timer:
    """Exit non-zero if cleanup has not finished within `budget_s`.

    A thread, because the thing it guards against is the event loop being
    stuck. Daemon, so a cleanup that finishes is never kept waiting for it.
    """
    def give_up() -> None:
        log.error(f"Shutdown did not finish in {budget_s:g}s — exiting anyway")
        _exit(1)

    timer = threading.Timer(budget_s, give_up)
    timer.daemon = True
    timer.start()
    return timer
