"""
Private listening (docs/listening.md): the Echo scores its own wake word and
sends nothing until it fires. This is the controller's side: waiting for the
Echo's listen report, the no-mic case, the session loop, and the wake and
barge-in events a private Echo sends.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import logging

import em_alarm
import em_announce
import em_barge
import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_leds
import em_listen
import em_oww_models
import em_shadow
import em_state
import em_tasks
import em_turn
import em_wake_claim

log = logging.getLogger("echomuse")


async def await_listen_report(device: em_device.Device) -> None:
    """Start nothing until the Echo reports how it listens (em_listen.awaiting_report)."""
    log.info(f"[{device.device_id}] waiting for the Echo to report private listening — not streaming")
    t0 = asyncio.get_event_loop().time()
    while (em_state.devices.get(device.device_id) is device
           and em_listen.awaiting_report(device.listen_view)):
        await asyncio.sleep(0.25)
        if t0 and asyncio.get_event_loop().time() - t0 > 30:
            log.warning(f"[{device.device_id}] no listen_state report after 30s — not listening")
            t0 = 0


async def no_mic(device: em_device.Device) -> None:
    """No microphone it can read: no mic_start, no scorer, until superseded."""
    log.info(f"[{device.device_id}] no microphone — not listening; speaker only")
    while em_state.devices.get(device.device_id) is device and not device.mic_capable:
        await asyncio.sleep(1.0)


async def private_listen(device: em_device.Device) -> None:
    """
    Wait for the Echo's own wakes and act on them.

    Nothing to score and no stream to watch: the Echo sends audio only inside
    a session its wake opened. mic_start still matters — it is what runs the
    Echo's local wake stream, which feeds its scorer.

    A wake during a turn is a barge-in (or words inside the turn), decided at
    once: the turn is a task, never awaited here, so this loop stays free.
    """
    log.info(f"[{device.device_id}] listening privately — the Echo detects its "
             f"own wake word and streams nothing until it does")
    await device.mic_start()
    while em_state.devices.get(device.device_id) is device and device.private_listening:
        try:
            ev = await asyncio.wait_for(device.listen_wakes.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if device.listen_router.is_closed(ev["session"]):
            # The Echo gave up on it first (listen_end): a wake delayed past
            # the ack timeout arrives with its own close, and acting on it ran
            # a turn that could only end no_speech, holding the arbiter.
            log.info(f"[{device.device_id}] wake for session {ev['session']} "
                     f"ignored — already closed on the Echo "
                     f"({(asyncio.get_event_loop().time() - ev['arrived']) * 1000:.0f}ms "
                     f"after it arrived, {ev['age_ms']}ms old when sent)")
            continue
        busy = (device.voice_lock.locked()
                or (device.private_turn_task is not None
                    and not device.private_turn_task.done()))
        if busy:
            try:
                await _private_barge(device, ev)
            finally:
                device.barge_decided.set()
            continue
        over = device.speaking and not device.timer_alarm_ringing  # an unowned announcement
        if over and not em_announce.wake_takes_turn(ev, device.barge_in_enabled):
            await device.listen_close(ev["session"], "speaking")
            continue
        device.private_turn_task = asyncio.create_task(
            em_announce.stop_then_turn(device, ev, _private_wake_turn) if over
            else _private_wake_turn(device, ev))
        device.private_turn_task.add_done_callback(em_tasks.log_exception)
    log.info(f"[{device.device_id}] no longer listening privately")


async def _private_wake_turn(device: em_device.Device, ev: dict) -> None:
    """
    One wake from a privately listening Echo: arbitrate, then run the turn on
    the session's audio. Mirrors the stream path's detection block in
    `_stream_listen` step for step; the differences are where the audio comes
    from and that nothing here was scored by this controller.
    """
    session = ev["session"]
    score = ev["score"]
    threshold = ev["threshold"] if ev["threshold"] is not None else device.oww_threshold
    age_s = ev["age_ms"] / 1000.0
    if age_s > em_shadow.MAX_PENDING_WAKE_S:
        # The person has finished speaking; answering now answers silence.
        log.warning(f"[{device.device_id}] on-device wake dropped — {age_s:.1f}s old "
                    f"(limit {em_shadow.MAX_PENDING_WAKE_S:.1f}s)")
        await device.listen_close(session, "stale")
        return
    if device.muted:
        await device.listen_close(session, "muted")
        return
    if not device.wake_word_enabled:
        # The Echo scores its own wake word and mic_stop doesn't stop that,
        # so each private wake is declined until the device can be told.
        await device.listen_close(session, "wake_off")
        return
    if ev["floor"] is not None:
        # The controller cannot measure the floor from a stream it does not
        # get; the Echo tracks it the same way and sends it with the wake.
        device.noise_floor = float(ev["floor"])
    log.info(
        f"[{device.device_id}] Wake word detected (source=device, private, "
        f"session={session}, score={score:.3f}, threshold={threshold:.3f}, "
        f"floor={device.noise_floor:.4f})"
    )
    em_dbwriter.submit(db.log_device, device.device_id, "info", "device",
                  f"Wake word detected (score={score:.3f}, device)")
    device.cancel_event.clear()
    if em_alarm.ringing_devices():
        # After the clear: the hold sets this event to cut the burst short.
        await em_alarm.hold_alarms()
    device.last_wake_mono = em_shadow.now() - age_s
    device.last_wake = {
        "model":       em_oww_models.prediction_key(device.oww_model),
        "score":       round(float(score), 4),
        "threshold":   round(float(threshold), 4),
        "noise_floor": round(device.noise_floor, 5),
    }

    serves = esphome.can_serve_turn(device.device_id)
    won_by = device.device_id
    if serves and device.wake_arb_ms > 0 and len(em_state.devices) > 1:
        won_by = await em_wake_claim.claim_wake(device, em_wake_claim.wake_heard_at(device, ev),
                                   em_wake_claim.device_level(ev), by="device",
                                   over_playback=bool(ev.get("barge")))
    if not serves or won_by != device.device_id:
        wake_info = device.last_wake
        device.last_wake = None
        await device.listen_close(session, "no_ha" if not serves else "ceded")
        if not serves:
            await esphome.record_dropped_wake(device, f"wakeword({score:.3f})", wake_info)
            await em_leds.leds_turn_end(device)
            log.info(f"[{device.device_id}] Wake heard but no HA connection — standing down "
                     f"(score={score:.3f})")
            em_dbwriter.submit(db.log_device, device.device_id, "info", "controller",
                          "Wake heard but no HA connection")
        else:
            # The Echo lit its own listening ring at the crossing; nothing on
            # a ceding path darkens it otherwise.
            await em_leds.leds_off(device)
            log.info(f"[{device.device_id}] Wake ceded to {won_by} "
                     f"(arbitration; score={score:.3f})")
            em_dbwriter.submit(db.log_device, device.device_id, "info", "controller",
                          f"Wake ceded to {won_by} (arbitration)")
        return

    await device.listen_ack(session)
    device.oww_paused.set()
    device.oww_paused_since = asyncio.get_event_loop().time()
    await device.beam_lock()
    try:
        await em_turn.run_voice_locked(device, trigger_label=f"wakeword-dev({score:.3f})",
                                is_wakeword=True, session=session)
    finally:
        # Idempotent: normally closed at end of speech already.
        if device.listen_session == session:
            await device.listen_close(session, "turn_end")
        await device.beam_unlock()


async def _private_barge(device: em_device.Device, ev: dict) -> None:
    """
    A wake from a privately listening Echo while a turn is running.

    The stream path's `_barge_watcher` scores the turn's own audio; a private
    Echo sends none after end of speech, so it detects the interruption itself
    (at the barge-in bar with two consecutive frames during playback — the
    same rule as em_barge.decide) and this acts on it exactly as the watcher's
    fire block does. A wake while the user is still speaking is words inside
    the command, and a wake with barge-in off is declined: both are closed.
    """
    session = ev["session"]
    in_playback = device.speaking or device.speaker_busy > 0
    phase = "playback" if in_playback else ("thinking" if device.thinking else None)
    if not device.barge_in_enabled or phase is None or device.barge_detected:
        await device.listen_close(session, "busy")
        log.info(f"[{device.device_id}] wake during a turn declined "
                 f"({'barge-in off' if not device.barge_in_enabled else phase or 'listening'})")
        return
    score = ev["score"]
    threshold = ev["threshold"] if ev["threshold"] is not None else device.barge_threshold
    log.info(f"[{device.device_id}] Barge-in: wake word during {phase} "
             f"(device, score={score:.3f}) — cancelling turn")
    em_dbwriter.submit(db.log_device, device.device_id, "info", "device",
                  f"Barge-in during {phase} (score={score:.3f})")
    device.barge_detected = True
    device.barge_decided.clear()
    if in_playback:
        # Before the claim, whose hold would put reply audio in the session.
        # Marked first: the reply can end inside the hold and the turn record
        # `ok` before abort_ha_run runs.
        esphome.note_barged(device.device_id)
        await device.send_control({"type": "speaker_flush"})
    serves = esphome.can_serve_turn(device.device_id)
    won_by = device.device_id
    if serves and device.wake_arb_ms > 0 and len(em_state.devices) > 1:
        won_by = await em_wake_claim.claim_wake(device, em_wake_claim.wake_heard_at(device, ev),
                                   em_wake_claim.device_level(ev), by="device",
                                   over_playback=True)
    verdict = em_barge.cede(
        serves=serves, won_by=won_by,
        device_id=device.device_id, score=score,
    )
    device.barge_ceded = verdict.ceded
    if device.barge_ceded:
        await device.listen_close(session, "ceded")
        log.info(f"[{device.device_id}] Barge-in ceded to "
                 f"{won_by if serves else 'nothing — no HA'} — stopping playback, "
                 f"not taking the turn")
    else:
        # The interrupting turn reads this session. Opened in the router now,
        # so frames already held are delivered ahead of the live ones.
        await device.listen_ack(session)
        device.listen_session = session
        for chunk in device.listen_router.open(session, asyncio.get_event_loop().time()):
            em_turn.put_voice_frame(device, chunk)
    if ev["floor"] is not None:
        device.noise_floor = float(ev["floor"])
    device.last_wake = {
        "model":       em_oww_models.prediction_key(device.oww_model),
        "score":       round(float(score), 4),
        "threshold":   float(threshold),
        "noise_floor": round(device.noise_floor, 5),
    }
    if verdict.no_ha:
        # Same stand-down as the stream barge watcher (#417) and the same two
        # reasons: no trace at all leaves an outage invisible, and the cue
        # reports the DEVICE's state rather than the turn's outcome.
        #
        # Carried, not recorded — see the same note in _barge_watcher. This
        # path runs before the interrupted turn unwinds, so a row written here
        # is overwritten by its em_turn_record.persist_turn before the ring reads it. The
        # loop's ceded branch is where it lands.
        device.barge_no_ha = (verdict.trigger_label, device.last_wake)
        device.last_wake = None
    device.cancel_event.set()
    device.barge_decided.set()
    if in_playback:
        esphome.abort_ha_run(device.device_id)
    else:
        esphome.cancel_voice_turn(device.device_id, abort_ha=True, reason="barged")
