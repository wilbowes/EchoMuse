"""
The /data and /shell planes of a connected Echo: mic frames, wake clips, BLE
frames and close reasons on /data; the shell socket handed to em_shell on
/shell. Both follow the device's /control connection (em_link_gate).

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import json
import logging

from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol
import websockets

import em_ble_proxy
import em_db as db
import em_esphome as esphome
import em_link_gate
import em_listen
import em_state
import em_tasks
import em_turn
import em_wake_samples
import em_wire
import em_wsclose

log = logging.getLogger("echomuse")


def _store_wake_candidate(device_id: str, sample: dict) -> int | None:
    """Write the bounded clip and its review metadata off the event loop."""
    name = em_wake_samples.filename(device_id)
    if name is None or not em_wake_samples.save(device_id, name, sample["pcm"]):
        return None
    record = {k: v for k, v in sample.items() if k != "pcm"}
    record["audio_file"] = name
    try:
        return db.insert_wake_sample(device_id, record)
    except Exception:
        em_wake_samples.remove(device_id, [name])
        raise


async def _persist_wake_candidate(device, sample: dict) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _store_wake_candidate, device.device_id, sample)


async def handle_data(ws: WebSocketServerProtocol, secure: bool = False):
    device = None
    remote = ws.remote_address
    # Initialised here, not in the handler below: the finally block reports
    # it on EVERY exit path, including the ones that never raise
    # ConnectionClosed at all.
    _close_why = (None, None, None, None)

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "identify":
            log.warning(
                f"[data] First message from {remote} was not identify — closing"
            )
            await ws.close()
            return

        device_id = msg["device_id"]

        if not await em_link_gate.link_auth_ok(ws, device_id, secure, "data"):
            await ws.close()
            return

        for _ in range(20):
            device = em_state.devices.get(device_id)
            if device is not None:
                break
            await asyncio.sleep(0.1)

        if device is None:
            log.warning(f"[data] Unknown device_id: {device_id} — closing")
            await ws.close()
            return

        why = em_link_gate.not_from_control(device, ws, secure)
        if why:
            log.warning(f"[data] {device_id}: {why} — rejecting")
            device = None   # the finally must not treat the live device as ours
            await ws.close()
            return

        replaced, device.data_ws = device.data_ws, ws
        if replaced is not None and replaced is not ws:
            # #751: a redial means the old link is dead; left open it lasts
            # to the ping timeout. Playback carries on over this connection.
            em_tasks.spawn(replaced.close(code=1000, reason="replaced"))
        # The connection bridge rides this plane: ask what the device holds
        # now that there is somewhere for the answer to arrive.
        em_tasks.spawn(em_ble_proxy.sync_slots(device.device_id))
        # #299: a fresh connection has by definition sent nothing yet — the
        # no-frames watchdog gives it FRESH_CONN_GRACE_S before treating
        # the silence as a zombie stream.
        device.frames_seen_this_connection = False
        device.data_connected_at = asyncio.get_event_loop().time()
        # A session's audio rode the old connection, and the device closes
        # its session on a link drop; nothing from before is routable.
        device.listen_router.reset()
        device.data_ready.set()
        log.info(f"[data] Data connection established: {device_id}")

        # One bad frame must not close the data connection (#255), same
        # reading as the control plane. This loop runs per frame though
        # (~23/s), so a persistent fault would flood the log at frame
        # rate: one traceback per 5s window, suppressed count reported
        # when the next window opens.
        _frame_err_last: float = float("-inf")
        _frame_err_suppressed = 0
        async for raw in ws:
            try:
                if not isinstance(raw, bytes):
                    continue
                # Checked before the mic-frame guards below, which would
                # otherwise drop this on the header-length test.
                if raw and raw[0] == em_wire.BLE_ADVERTS_TYPE:
                    try:
                        body = json.loads(raw[1:])
                    except Exception:
                        log.warning(
                            f"[{device.device_id}] malformed ble_adverts frame dropped"
                        )
                        continue
                    em_ble_proxy.forward_adverts(
                        device.device_id, body.get("adverts") or []
                    )
                    continue
                if raw and raw[0] == em_wire.BLE_GATT_TYPE:
                    em_ble_proxy.gatt_from_device(device.device_id, raw[1:])
                    continue
                if raw and raw[0] == em_listen.FRAME_TYPE:
                    # Private-listening session audio. The router holds it
                    # until its wake arrives, and drops it once the session
                    # is closed — never routed by a flag.
                    parsed = em_listen.parse_frame(raw)
                    if parsed is None:
                        continue
                    session, _seq, pcm = parsed
                    device.frames_seen_this_connection = True
                    # Never fed to wake_capture: this is private-session audio,
                    # already past the user's own wake word, and its pre-roll
                    # would otherwise hold whatever a PRIOR session last said
                    # (#696 review) — this frame type carries nothing else.
                    for chunk in device.listen_router.frame(
                            session, pcm, asyncio.get_event_loop().time()):
                        em_turn.put_voice_frame(device, chunk)
                    continue
                if len(raw) <= em_wire.MIC_HEADER_LEN:
                    continue
                if raw[0] != em_wire.MIC_FRAME_TYPE:
                    continue
                if len(raw) == em_wire.MIC_HEADER_LEN + 1 and raw[em_wire.MIC_HEADER_LEN] in (em_wire.VAD_END_TYPE, em_wire.VAD_NO_SPEECH_TIMEOUT_TYPE):
                    sentinel = (
                        esphome.VAD_SENTINEL_TIMEOUT
                        if raw[em_wire.MIC_HEADER_LEN] == em_wire.VAD_NO_SPEECH_TIMEOUT_TYPE
                        else esphome.VAD_SENTINEL_END
                    )
                    q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
                    if q.full():
                        try:
                            q.get_nowait()
                            log.warning(f"[{device.device_id}] queue full — dropped one frame to deliver VAD sentinel")
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(sentinel)
                    except asyncio.QueueFull:
                        log.error(f"[{device.device_id}] VAD sentinel lost — queue still full after drain")
                    continue
                _now = asyncio.get_event_loop().time()
                pcm = raw[em_wire.MIC_HEADER_LEN:]
                # Keep bounded pre-roll only while capture is enabled. The
                # capture object owns the ring and post-roll limits.
                if device.wake_clip_capture or device.wake_capture.active is not None:
                    completed = device.wake_capture.feed_audio(pcm)
                    if completed is not None:
                        em_tasks.spawn(_persist_wake_candidate(device, completed))\
                            .add_done_callback(em_tasks.log_exception)
                payload = em_listen.Frame(
                    pcm, _now,
                    # A VAD-gated turn stream skips frames, so its sequence
                    # does not count time; arrival is the best it offers.
                    _now if device.mic_gated else device.capture_clock.observe(
                        int.from_bytes(raw[1:3], "big"), _now))
                q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Drop the OLDEST frame, not the newest — keeps the tail of
                    # the audio contiguous with real time, which is what OWW and
                    # STT care about. Dropping the newest froze the queue at a
                    # stale snapshot while fresh speech was discarded.
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

            except asyncio.CancelledError:
                raise
            except Exception:
                _now = asyncio.get_running_loop().time()
                if _now - _frame_err_last >= 5.0:
                    if _frame_err_suppressed:
                        log.error(
                            f"[{device.device_id}] {_frame_err_suppressed} data-frame errors suppressed")
                    log.exception(
                        f"[{device.device_id}] Data handler failed - frame dropped, connection kept")
                    _frame_err_last = _now
                    _frame_err_suppressed = 0
                else:
                    _frame_err_suppressed += 1
    except asyncio.TimeoutError:
        log.warning(f"[data] Identify timeout from {remote}")

    except websockets.exceptions.ConnectionClosed as e:
        # Keep the reason — see em_wsclose. Discarding it here is what made
        # three sessions of intermittent mid-response drops undiagnosable.
        _close_why = em_wsclose.from_exception(e)

    except Exception as e:
        log.error(f"[data] Handler error: {e}")

    finally:
        if device:
            if device.data_ws is ws:
                device.data_ws = None
                device.data_ready.clear()
            _msg = (f"[data] Data connection closed: {device.device_id} — "
                    f"{em_wsclose.describe(*_close_why)}")
            if em_wsclose.is_routine(_close_why[0], _close_why[2]):
                log.info(_msg)
            else:
                # A device dropping every few minutes on a keepalive timeout
                # is indistinguishable from one that never dropped, in a log
                # of INFO lines.
                log.warning(_msg)


async def handle_shell(ws: WebSocketServerProtocol, path: str, secure: bool = False):
    import aiohttp as _aiohttp

    # Path may carry a query: /shell/{device_id}?pty=1 signals that the
    # device actually established a PTY session (it may have been requested
    # but failed to allocate — the device falls back to a plain pipe and
    # omits the flag). The dashboard needs the established mode, not the
    # requested one, to pick its input framing.
    device_id, _, query = path.removeprefix("/shell/").partition("?")
    pty_mode = "pty=1" in query
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    if not await em_link_gate.link_auth_ok(ws, device_id, secure, "shell"):
        await ws.close()
        return

    live = em_state.devices.get(device_id)
    why = "device is not connected" if live is None else em_link_gate.not_from_control(live, ws, secure)
    if why:
        log.warning(f"[shell] {device_id}: {why} — rejecting")
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id} (pty={pty_mode})")

    done_future  = em_state.shell_pending.get(device_id)
    dashboard_ws = em_state.shell_dashboard.get(device_id)

    if done_future is None or done_future.done():
        log.warning(f"[shell] No pending shell request for {device_id} — closing")
        await ws.close()
        return

    if dashboard_ws is None:
        log.info(f"[shell] Programmatic session: {device_id}")
        done_future.set_result(ws)
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=300.0)
        except (asyncio.TimeoutError, Exception):
            pass
        log.info(f"[shell] Programmatic session ended: {device_id}")
        return

    log.info(f"[shell] Proxying: {device_id}")

    # Tell the dashboard which mode the device established before any
    # shell bytes flow: PTY sessions use framed input (0x00 stdin /
    # 0x01 resize) and emit terminal escape sequences; pipe sessions
    # (pre-PTY firmware) are raw both ways.
    try:
        await dashboard_ws.send_str(json.dumps({"type": "shell_meta", "pty": pty_mode}))
    except Exception:
        pass

    async def device_to_dashboard():
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await dashboard_ws.send_bytes(msg)
                else:
                    await dashboard_ws.send_str(msg)
        except Exception:
            pass

    async def dashboard_to_device():
        try:
            async for msg in dashboard_ws:
                if msg.type == _aiohttp.WSMsgType.BINARY:
                    await ws.send(msg.data)
                elif msg.type == _aiohttp.WSMsgType.TEXT:
                    await ws.send(msg.data.encode())
                elif msg.type in (_aiohttp.WSMsgType.CLOSE,
                                  _aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass

    tasks = [
        asyncio.create_task(device_to_dashboard()),
        asyncio.create_task(dashboard_to_device()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        log.info(f"[shell] Session ended: {device_id}")
        if not done_future.done():
            done_future.set_result(None)
