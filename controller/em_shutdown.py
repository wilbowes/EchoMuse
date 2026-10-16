"""Stop the controller when the process is asked to stop.

The add-on runs this process as PID 1 (`init: false`). Linux does not apply
the default terminate action to PID 1, so an unhandled SIGTERM sits until
the supervisor sends SIGKILL. The next start then finds the ports still
bound. A handler registered on the loop runs for PID 1.
"""

import logging
import signal

import em_dbwriter

log = logging.getLogger("echomuse")


def _on_signal(stop, sig) -> None:
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = str(sig)
    log.info(f"Received {name} — shutting down")
    stop.set()


def install(loop, stop) -> None:
    """Set `stop` on SIGTERM or SIGINT. Safe to call once, from main()."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, stop, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            log.warning(f"No signal handler for {sig}")


async def finish(tasks, azc, info, runner, deps=None) -> None:
    """Close players, voice and BT listeners, mDNS, the API, and the database.

    The WebSocket servers have already left their context by the time this
    runs, which sends the device links a close frame. The database is last
    so a turn written during teardown still lands.
    """
    if deps is None:
        import em_ble_proxy
        import em_db as db
        import em_esphome
        import em_player
        deps = (
            em_player.stop_all,
            em_ble_proxy.stop_ble_proxy_servers,
            em_esphome.stop_esphome_servers,
            db.close,
        )
    stop_players, stop_ble, stop_voice, close_db = deps
    await stop_players()
    await stop_ble()
    await stop_voice()
    for task in tasks:
        task.cancel()
    try:
        await azc.async_unregister_service(info)
        await azc.async_close()
    except Exception:
        log.warning("mDNS shutdown failed", exc_info=True)
    await runner.cleanup()
    # Voice has stopped. Drain what teardown queued, then refuse anything
    # that arrives after the connection is gone — one warning, not an error
    # per write ("db.init() has not been called").
    em_dbwriter.drain()
    em_dbwriter.seal()
    close_db()
    log.info("EchoMuse Controller stopped")
