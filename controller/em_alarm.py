"""
The timer alarm as the Echo rings it: starting and stopping the ring, the
burst it plays, holding a ringing alarm through a wake, and dismissing it by
speech. Timer state itself is em_timers'; Home Assistant owns the countdown.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import logging
import os

import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_leds
import em_playback
import em_player
import em_speechgate
import em_state
import em_timers
import em_wire

log = logging.getLogger("echomuse")


async def start_timer_alarm(device: em_device.Device) -> None:
    if device.timer_alarm_task is not None and not device.timer_alarm_task.done():
        return  # already ringing (a second timer finished) — one ring is enough
    log.info(f"[{device.device_id}] Timer alarm — ringing")
    task = asyncio.create_task(_ring_timer_alarm(device))
    device.timer_alarm_task = task

    def _ring_done(t: asyncio.Task) -> None:
        # A ring that dies must say so. Without this the exception sits
        # unretrieved on the task and the only symptom is silence, which is
        # indistinguishable from the timer event never arriving.
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error(f"[{device.device_id}] Timer alarm failed: {exc!r}", exc_info=exc)

    task.add_done_callback(_ring_done)


async def stop_timer_alarm(device: em_device.Device) -> bool:
    """
    Stop a ringing alarm. Returns whether one was running. Flushes the device
    speaker so a dismissal is instant rather than waiting out the ~5.5s already
    buffered on the device (same reasoning as the button-cancel path).
    """
    task = device.timer_alarm_task
    device.timer_alarm_task = None
    if task is None or task.done():
        return False
    device.cancel_event.set()
    await device.send_control({"type": "speaker_flush"})
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return True


def ringing_devices() -> "list[em_device.Device]":
    return [d for d in em_state.devices.values() if d.timer_alarm_ringing]


# A hold outlives the listen it is for by this much, so a ring cannot resume
# between the last frame scored and the verdict being acted on.
_ALARM_HOLD_MARGIN_S = 2.0


async def hold_alarms() -> None:
    """
    Silence every ringing alarm for the length of a dismissal listen.

    Fleet-wide: which Echo wins a wake is decided by arbitration, and the one
    that is ringing can lose it, so the wake is taken to be about whatever is
    ringing. The burst in flight is flushed, since the rest of it is already
    queued on the device. The hold is a deadline (see timer_alarm_hold_t).
    """
    until = (asyncio.get_event_loop().time()
             + em_timers.DISMISS_LISTEN_S + _ALARM_HOLD_MARGIN_S)
    for d in ringing_devices():
        first = not d.timer_alarm_held
        d.timer_alarm_hold_t = until
        if first:
            d.cancel_event.set()
            await d.send_control({"type": "speaker_flush"})


def _release_alarm_holds() -> None:
    """Nobody spoke: the rings resume on their next loop."""
    for d in ringing_devices():
        d.timer_alarm_hold_t = 0.0
        # Set by the hold to cut the burst short; left set it would end the
        # ring as a dismissal.
        d.cancel_event.clear()


async def dismiss_by_speech(device: em_device.Device) -> None:
    """
    A wake word was heard while a timer rings: stop the ring if anyone
    speaks, and let it resume if nobody does (em_timers, "Stopping a ringing
    timer by voice"). Runs in place of the voice turn, so nothing reaches
    Home Assistant; the caller's cleanup closes the listening session.
    """
    await hold_alarms()
    # The listening ring on the Echo that took the wake AND on every Echo that
    # is ringing: the person is looking at the one making the noise, which
    # arbitration may not have picked, and a ring that only goes quiet does
    # not say it is waiting to be told.
    lit = {d.device_id: d for d in [device, *ringing_devices()]}
    for d in lit.values():
        await em_leds.leds_listening(d)
    vad = em_speechgate.new_turn()
    listen = em_timers.DismissListen(esphome.VOICE_PREROLL_DISCARD)
    # Without the detector there is no way to ask whether anyone spoke, and
    # an alarm that cannot be stopped by voice is the worse failure.
    spoke = vad is None
    loop = asyncio.get_event_loop()
    deadline = loop.time() + em_timers.DISMISS_LISTEN_S
    while not spoke:
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            frame = await asyncio.wait_for(device.voice_queue.get(), timeout=left)
        except asyncio.TimeoutError:
            break
        spoke = listen.push(vad.prob(frame))

    if spoke:
        stopped = [d.device_id for d in ringing_devices()]
        for device_id in stopped:
            await esphome.dismiss_timer_alarm(device_id)
        how = ("no speech detector" if vad is None
               else f"speech after {listen.frames * 80}ms")
        log.info(f"[{device.device_id}] Timer alarm stopped by voice "
                 f"({how}; ringing on {', '.join(stopped) or 'nothing'})")
        em_dbwriter.submit(db.log_device, device.device_id, "info", "controller",
                           "Timer alarm stopped by voice")
        if vad is not None:
            # The ring is already silent. Keep the listening ring up until the
            # person has finished speaking; stopping an alarm darkens its
            # Echo, so it is lit again first.
            for d in lit.values():
                await em_leds.leds_listening(d)
            tail = loop.time() + em_timers.DISMISS_TAIL_S
            while not listen.finished:
                left = tail - loop.time()
                if left <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(device.voice_queue.get(), timeout=left)
                except asyncio.TimeoutError:
                    break
                listen.push(vad.prob(frame))
    else:
        _release_alarm_holds()
        log.info(f"[{device.device_id}] Wake over a ringing timer, nobody spoke "
                 f"({listen.frames} frames, peak {listen.peak:.2f}) — ring resumes")
    # Dark everywhere: stopped, that is the confirmation; resuming, each ring
    # repaints its own pulse with its next burst.
    for d in lit.values():
        await em_leds.leds_off(d)


_alarm_pcm_cache: "bytes | None" = None


async def _alarm_burst_pcm() -> bytes:
    """
    The alarm burst as 48kHz mono S16_LE, decoded once and cached.

    The bundled Voice PE sound is already 48kHz mono, so this is a straight
    decode with no resample.

    Returns b"" if the sound cannot be produced, and the caller refuses to
    ring. There is deliberately no synthesised fallback: a device that alerts
    with a different sound from every other one is harder to support than one
    that says loudly that its install is broken, and the file ships in the
    image (Dockerfile) so its absence is a packaging fault, not a runtime
    condition to paper over.
    """
    global _alarm_pcm_cache
    if _alarm_pcm_cache is not None:
        return _alarm_pcm_cache

    if not os.path.exists(em_timers.ALARM_SOUND_FILE):
        log.error(
            f"Timer alarm sound missing at {em_timers.ALARM_SOUND_FILE} — "
            f"timers cannot ring. Is sounds/ present in the image?"
        )
        return b""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", em_timers.ALARM_SOUND_FILE,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(em_wire.SPEAKER_RATE), "-ac", "1", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pcm, err = await proc.communicate()
    except FileNotFoundError:
        log.error("ffmpeg not found — timers cannot ring")
        return b""
    if proc.returncode != 0 or not pcm:
        log.error(
            f"Timer alarm sound failed to decode — timers cannot ring "
            f"({err.decode(errors='replace').strip()[:200]})"
        )
        return b""

    _alarm_pcm_cache = pcm
    return pcm


async def _wait_for_turn_audio(device: em_device.Device) -> bool:
    """
    Block until any voice turn already in flight has fully finished.

    A timer whose duration is shorter than its own triggering turn's round trip
    ("set a timer for one second") reaches FINISHED while that turn's spoken
    reply is still going out, and two writers on the 0x02 plane interleave
    their frames. `device.voice_lock` is held for the WHOLE turn including
    device-confirmed playback, so acquiring it is the barrier; it is released
    again immediately, because a spoken dismissal has to be able to START a
    turn while the alarm rings — holding it for the ring would make the
    feature this ring exists to support impossible.

    Returns whether it had to wait, for the log line. Cancellation while
    waiting is the dismissal path (stop_timer_alarm cancels this task) and
    needs no special handling: the lock is never held across an await here.
    """
    if not device.voice_lock.locked():
        return False
    await device.voice_lock.acquire()
    device.voice_lock.release()
    return True


async def _ring_timer_alarm(device: em_device.Device) -> None:
    """
    Loop the alarm chime until cancelled or MAX_RING_S elapses.

    Takes the speaker like an announcement (interrupt media, restore after),
    but deliberately LEAVES THE MIC RUNNING: the wake word has to be heard
    over the chime, which is the same problem as barge-in over TTS and is
    solved the same way — the device's AEC subtracts its own speaker output and
    the wake word is scored at bargeInThreshold (wake_word_listener). A
    detection holds the ring silent (_hold_alarms) while _dismiss_by_speech
    listens for whether anyone speaks.
    """
    pcm = await _alarm_burst_pcm()
    if not pcm:
        # Already logged, with the reason. Clear the registry so the timer is
        # not left "finished but not ringing", which would make the next
        # CANCELLED try to stop a ring that never started.
        esphome.clear_timers(device.device_id)
        return
    if await _wait_for_turn_audio(device):
        log.info(
            f"[{device.device_id}] Timer alarm — waited for the in-flight "
            f"turn to finish before ringing"
        )
    await em_player.interrupt(device.device_id)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        while loop.time() - t0 < em_timers.MAX_RING_S:
            # A voice turn started by a wake word over the chime owns the
            # speaker plane while it answers. Two writers would interleave
            # frames on it, so the ring waits its turn rather than talking
            # over the response — the duck is an amplitude, this is the
            # mutual exclusion that makes the amplitude meaningful. Tested on
            # speaker_busy and NOT on `speaking`, which clears when the socket
            # writes finish while the device is still playing the response out
            # of its buffer — the ring bursting into that gap is exactly the
            # overlap this guard is for.
            #
            # Held: a wake word was heard, and the ring stays silent until
            # the hold is released or runs out (_hold_alarms).
            if device.speaker_busy or device.timer_alarm_held:
                await asyncio.sleep(0.1)
                continue
            in_turn = device.voice_lock.locked()
            if not in_turn:
                # cancel_event may be left set by a previous turn; clear it so
                # the burst plays in full rather than aborting immediately.
                # NOT while a turn is live: it is that turn's abort signal
                # (barge-in, mute, HA pipeline cancel), and clearing it from
                # here would swallow a cancellation the user asked for.
                device.cancel_event.clear()
                # The turn owns the ring visually while it is listening and
                # thinking — repainting amber every burst would stamp over the
                # listening ring the moment the user starts speaking.
                if device.led_anim_capable:
                    await device.send_led_anim(dict(em_timers.TIMER_ANIM))
            await em_playback.run_post_turn_playback(device, pcm)
            if device.timer_alarm_held:
                continue  # cut short by a hold, which is not a dismissal
            if device.cancel_event.is_set():
                break  # dismissed mid-burst
            await asyncio.sleep(em_timers.BURST_GAP_S)
        else:
            log.warning(
                f"[{device.device_id}] Timer alarm auto-stopped after "
                f"{em_timers.MAX_RING_S:.0f}s with no dismissal"
            )
            # Clear the registry so a later CANCELLED does not try to re-stop a
            # ring that has already ended on its own. State-only — we are
            # already inside the ring task, so we must not re-enter stop.
            esphome.clear_timers(device.device_id)
    except asyncio.CancelledError:
        raise
    finally:
        device.timer_alarm_task   = None
        device.timer_alarm_hold_t = 0.0
        # The mic was never stopped, but a turn taken over the chime may have
        # left it routed — the same defensive restart the turn path ends with.
        await device.mic_start()
        await em_player.resume_interrupted(device.device_id)
        await em_leds.leds_off(device)
