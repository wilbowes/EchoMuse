"""
One voice turn on an Echo, start to end: the wake or button that began it,
HA's pipeline run (em_esphome), the reply and its playback, barge-ins over
it, and follow-ups HA continues. Also where a turn's mic frames are queued.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import contextlib
import logging

import em_alarm
import em_barge_watcher
import em_device
import em_esphome as esphome
import em_leds
import em_listen
import em_playback
import em_player
import em_scenes
import em_state
import em_wakeword

log = logging.getLogger("echomuse")


# Bound on waiting for a barge's claim (hold 250ms, wakeArbitrationMs up to
# a few hundred); past it the turn loop acts on the barge as it stands.
BARGE_DECISION_TIMEOUT_S = 3.0


def put_voice_frame(device: em_device.Device, chunk: bytes) -> None:
    """Queue session audio for the turn, dropping the OLDEST when full — the
    same rule as the stream path, for the same reason: STT wants the tail
    contiguous with real time."""
    q = device.voice_queue
    try:
        q.put_nowait(chunk)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(chunk)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


async def run_voice_locked(device: em_device.Device, trigger_label: str = "unknown",
                            is_wakeword: bool = False, session: int | None = None):
    """
    session: the private-listening session this turn listens on
    (docs/listening.md), or None for a turn fed by the stream or by a bounded
    turn stream. Its held audio is delivered AFTER the stale-frame drain below,
    and it is closed at end of speech.

    is_wakeword: explicit flag for whether this turn was triggered by wake-
    word detection (as opposed to a button press). Used to decide preroll
    discard (see C3) — kept as its own parameter rather than inferred by
    parsing trigger_label (which is a free-form string meant for logging/
    trace display, not a control-flow key) so a future change to the label
    format can't silently change behaviour here.
    """
    drained = 0
    while not device.mic_queue.empty():
        try:
            device.mic_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    if drained:
        log.info(f"[{device.device_id}] Voice turn: drained {drained} stale frames")
    if session is not None:
        # After the drain, never before it: the drain is for stale audio, and
        # this is the command. The router held whatever arrived ahead of the
        # wake; from here its live frames land in voice_queue directly.
        device.listen_session = session
        held = device.listen_router.open(session, asyncio.get_event_loop().time())
        for chunk in held:
            put_voice_frame(device, chunk)
        log.info(f"[{device.device_id}] session {session}: {len(held)} frames "
                 f"({len(held) * 80}ms) held ahead of the wake")
    # Voice preempts music: pause an active media session for the whole
    # conversation (incl. continuations) and resume it afterwards. The
    # matching resume_interrupted below only fires if this interrupt
    # actually paused something.
    await em_player.interrupt(device.device_id)
    try:
        async with device.voice_lock:
            if is_wakeword and em_alarm.ringing_devices():
                await em_alarm.dismiss_by_speech(device)
                return
            log.info(f"[{device.device_id}] Voice turn starting (esphome mode)")
            device.listening = True
            await em_leds.leds_listening(device)
            await em_device.push_device_state(device)

            stop_spin = asyncio.Event()
            spin_task = None
            # Barge-in watcher state — reset per turn iteration below.
            watcher          = None
            playback_started = asyncio.Event()

            async def stop_watcher():
                nonlocal watcher
                if watcher is None:
                    return
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning(f"[{device.device_id}] Barge watcher error: {e}")
                watcher = None

            async def cleanup_esphome():
                device.thinking  = False
                device.listening = False
                await em_device.push_device_state(device)
                stop_spin.set()
                if spin_task and not spin_task.done():
                    spin_task.cancel()
                    try:
                        await spin_task
                    except asyncio.CancelledError:
                        pass
                await em_leds.leds_turn_end(device)

            async def on_thinking_esphome():
                nonlocal spin_task, watcher
                if stop_spin.is_set():
                    return  # cleanup already ran; turn is over
                device.thinking  = True
                device.listening = False
                await em_device.push_device_state(device)
                log.info(f"[{device.device_id}] Thinking (esphome)")
                if not device.cancel_event.is_set() and (
                    spin_task is None or spin_task.done()
                ):
                    spin_task = asyncio.create_task(
                        em_leds.leds_spin_green(device, stop_spin)
                    )
                if device.private_listening:
                    # End of speech: close the session (and its beam lock,
                    # or barges are heard on the turn's edge mic) or end a
                    # button/follow-up stream; back to local listening,
                    # which hears a barge-in itself (_private_barge).
                    if device.listen_session is not None:
                        await device.listen_close(reason="end_of_speech")
                        await device.beam_unlock()
                    else:
                        await device.mic_stop()
                    return
                # Barge-in watcher starts here, not at playback: STT has
                # ended (VAD_END), so anything on the mic from now on is
                # a potential interruption. Spans thinking → playback;
                # cancelled in the turn loop's finally.
                if device.barge_in_enabled and (
                    watcher is None or watcher.done()
                ):
                    watcher = asyncio.create_task(
                        em_barge_watcher.barge_watcher(device, playback_started)
                    )

            async def post_turn_play_esphome(pcm_chunks):
                nonlocal spin_task, watcher
                if spin_task is None or spin_task.done():
                    spin_task = asyncio.create_task(
                        em_leds.leds_spin_green(device, stop_spin)
                    )
                meter_refresh_task = None
                meter_on = None
                if device.led_anim_capable:
                    # Playback ring: throb with the response's live audio
                    # level (device-side "meter" pattern, RMS measured at
                    # the ALSA write). Replaces the thinking spinner on
                    # the device; spin_task keeps waiting on stop_event
                    # and its finally still clears the ring at turn end.
                    #
                    # RAISED WHEN AUDIO STARTS, NOT WHEN PLAYBACK IS SET UP.
                    # The meter renders the live speaker RMS, so before the
                    # device's ALSA write begins it draws an unlit ring. On
                    # the buffered path that was invisible (the fetch had
                    # already completed, so frames flushed at socket speed),
                    # but streaming moves the fetch+decode INSIDE playback:
                    # sending it here left the ring dark from the end of the
                    # spinner until HA returned audio — seconds on a slow
                    # response, and indistinguishable from a failed turn
                    # (user report 2026-07-31). The spinner keeps running
                    # until _meter_on fires, so the handover is continuous.
                    #
                    # A streamed response has no known duration up front.
                    # Refresh the same bounded dead-man TTL while PCM arrives:
                    # long speech cannot outlive its meter, but a controller
                    # crash still lets the device clear the ring on its own.
                    meter = dict(device.led_scene["meter_anim"])
                    meter["ttlSec"] = em_scenes.meter_ttl(0.0)

                    async def _refresh_meter_dead_man() -> None:
                        refresh_seconds = max(1.0, meter["ttlSec"] / 2)
                        while True:
                            await asyncio.sleep(refresh_seconds)
                            await device.send_led_anim(meter)

                    async def _meter_on() -> None:
                        nonlocal meter_refresh_task
                        if meter_refresh_task is not None:
                            return
                        await device.send_led_anim(meter)
                        meter_refresh_task = asyncio.create_task(
                            _refresh_meter_dead_man()
                        )

                    # The wrapper is applied by the playback itself, after it
                    # has registered this playback's start waiter (#203).
                    meter_on = _meter_on

                if device.private_listening:
                    # Defensive, for a turn that reached TTS without an end
                    # of speech: close as end of speech would have.
                    if device.listen_session is not None:
                        await device.listen_close(reason="playback")
                        await device.beam_unlock()
                elif device.barge_in_enabled:
                    # Barge-in (§3.2): keep the mic running through
                    # playback — the device's AEC subtracts the speaker
                    # output, which is what makes this safe (enable AEC
                    # before enabling barge-in; without it the watcher
                    # scores raw echo and the raised threshold is the
                    # only defence). The pre-AEC problems the mic_stop
                    # guarded against are gone: AGC no longer exists on
                    # the wake stream (v2.7.0) and echo content is
                    # cancelled at the source (v2.7.3).
                    #
                    # The watcher normally exists already (started at
                    # thinking onset); the phase flag switches it to the
                    # playback threshold. Defensive create for turns
                    # that reach TTS without an STT_VAD_END.
                    playback_started.set()
                    if watcher is None or watcher.done():
                        watcher = asyncio.create_task(
                            em_barge_watcher.barge_watcher(device, playback_started)
                        )
                else:
                    # Acoustic-feedback guard (barge-in off): stop the mic
                    # BEFORE playback, not just in the post-turn finally.
                    # With the mic running through TTS pre-AEC, the device
                    # processed its own speaker echo (63-65 junk frames per
                    # turn measured 2026-07-06) and sent it upstream on the
                    # same Wi-Fi radio receiving the TTS frames (speaker
                    # underruns → audible stutter). The finally's mic_stop
                    # stays as a safety net (StopMic no-ops when already
                    # stopped); restart is owned by the continuation branch /
                    # wake listener / button handler as before.
                    await device.mic_stop()

                try:
                    return await em_playback.run_streaming_post_turn_playback(
                        device, pcm_chunks, on_start=meter_on
                    )
                finally:
                    if meter_refresh_task is not None:
                        meter_refresh_task.cancel()
                        await asyncio.gather(
                            meter_refresh_task, return_exceptions=True
                        )

            # P0-1: no mic_start_turn() here on the initial (wake/button)
            # entry — for a wake turn the stream is already running on
            # ch6 and oww_paused routes frames to voice_queue. The
            # acoustic-feedback guard is mic_stop in
            # post_turn_play_esphome, sent immediately before TTS
            # playback; the finally below is only the safety net.
            #
            # Continuation loop: if HA sets continue_conversation on
            # INTENT_END, re-trigger immediately after TTS+drain rather
            # than returning to OWW idle. The reference implementation
            # (linux-voice-assistant) uses a 0.5s settle delay after TTS
            # before opening the mic — that's already covered by
            # the streaming playback path's buffer drain sleep, so no
            # additional delay is needed here.
            #
            # C2 fix (2026-07-05 review): the `finally` below runs
            # device.mic_stop() on every iteration, including the one
            # that decides to continue — previously nothing ever put the
            # stream back before looping into the next trigger_voice_turn,
            # so a continuation turn streamed from a stopped mic and
            # silently timed out as no_speech every time. Fixed by
            # calling device.mic_start() (no lock_mic — same ch6 stream
            # as the wake path; no-ops if somehow already running) in the
            # continuation branch, before looping.
            #
            # C3 fix: preroll_discard is 0 for button/continuation turns
            # (no wake-word tail to remove — discarding real audio here
            # just clips the first word/words, the exact bug P0-1 fixed
            # on the wake path) and VOICE_PREROLL_DISCARD only for the
            # initial wakeword-triggered turn.
            turn_label      = trigger_label
            preroll_discard = em_listen.tail_discard(
                is_wakeword, session is not None, esphome.VOICE_PREROLL_DISCARD)
            while True:
                should_continue = False
                try:
                    should_continue = await esphome.trigger_voice_turn(
                        device=device,
                        on_thinking=on_thinking_esphome,
                        post_turn_play=post_turn_play_esphome,
                        trigger_label=turn_label,
                        preroll_discard=preroll_discard,
                    )
                finally:
                    # Watcher spans thinking→playback and is owned here:
                    # every exit path (normal, barge, error, cancel)
                    # must stop it before the next iteration re-arms.
                    await _await_barge_decision(device)
                    await stop_watcher()
                    # On barge the mic stays up: the user's follow-up
                    # command is already flowing into voice_queue and a
                    # mic_stop/start cycle here would drop the words
                    # spoken in the same breath as the wake word.
                    if not device.barge_detected:
                        await device.mic_stop()
                    await cleanup_esphome()
                    log.info(f"[{device.device_id}] Voice turn complete (esphome mode)")

                if device.barge_detected and device.barge_ceded:
                    # Another Echo won the interrupting utterance, or there is
                    # no pipeline behind this one. Playback has already been
                    # cancelled and that stays cancelled — the user spoke over
                    # this device and it must stop talking regardless of who
                    # answers. What it must NOT do is run the turn as well.
                    #
                    # The no-HA half records and cues HERE, not where the barge
                    # fired (#417). By now the interrupted turn has persisted
                    # and left last_turn_outcome as "barged", and
                    # _leds_turn_end reads exactly that — so recording earlier
                    # wrote a row nobody could see and a cue for an outcome
                    # that no longer existed. The watcher only carries the
                    # reason across.
                    no_ha = device.barge_no_ha
                    device.barge_detected = False
                    device.barge_ceded    = False
                    device.barge_no_ha    = None
                    device.cancel_event.clear()
                    device.last_wake = None
                    if no_ha:
                        # As the button's stand-down does a few hundred lines
                        # down: the listening ring is already lit from turn
                        # start, so this is a hold rather than a repaint, and
                        # barge_detected must be clear for the cue to survive
                        # _leds_turn_end's own guard.
                        await em_leds.leds_listening(device)
                        await esphome.record_dropped_wake(device, *no_ha)
                        await em_leds.leds_turn_end(device)
                    break

                if device.barge_detected:
                    # Barge-in: the watcher cancelled playback because
                    # the wake word was spoken over it. Re-enter a fresh
                    # turn immediately — same shape as continuation, but
                    # with the wake-word preroll discard (there IS a
                    # "…rhasspy" tail to drop this time).
                    device.barge_detected = False
                    device.cancel_event.clear()
                    log.info(f"[{device.device_id}] Barge-in: starting interrupting turn")
                    await device.mic_start()  # defensive no-op if running
                    # Re-arm listening state — cleanup_esphome() in the
                    # finally just turned the ring off, which left the
                    # device dark while it was actually listening for the
                    # interrupting command (looked dead — user report
                    # 2026-07-08). Same re-arm as the continuation branch;
                    # no voice_queue drain here though, the follow-up
                    # words spoken after "hey rhasspy" are already in it.
                    device.listening = True
                    await em_leds.leds_listening(device)
                    await em_device.push_device_state(device)
                    turn_label      = "barge-in"
                    # A barge-in the Echo heard itself opened a session
                    # (_private_barge), which starts after the wake word.
                    preroll_discard = em_listen.tail_discard(
                        True, device.listen_session is not None,
                        esphome.VOICE_PREROLL_DISCARD)
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                    continue

                if should_continue and not device.cancel_event.is_set():
                    log.info(f"[{device.device_id}] Continuing conversation (HA requested)")
                    # C2 fix: put the mic stream back before looping —
                    # the finally above just stopped it, and the next
                    # trigger_voice_turn will read from voice_queue,
                    # which is fed only while the device stream is
                    # running. No lock_mic — same ch6 stream as wake.
                    #
                    # A private Echo has no wake stream to reuse: the answer
                    # to a follow-up rides a bounded turn stream, exactly as
                    # a button press does — the user is expected to speak,
                    # and it ends at their end of speech.
                    if em_wakeword.follow_up_needs_turn_stream(
                            private=device.private_listening,
                            enabled=device.wake_word_enabled):
                        await device.mic_stop()
                        await device.mic_start_turn()
                    else:
                        await device.mic_start()
                    # Wake sound (#1095): the mic is open for an answer, with no wake word said.
                    if getattr(device, "wake_sound", False) and device.wake_cue_capable:
                        await device.play_cue("wake")
                    # Fresh stream starts with the VAD gate closed — the
                    # user must speak again from zero, same onset cost
                    # as any post-mic_stop restart. Acceptable for v1 of
                    # continuation (see review C2 wrinkle note); §3.4's
                    # device preroll ring will fix this properly later.
                    # Drain stale frames accumulated during TTS playback
                    # before the next turn begins — same as post-wake drain.
                    drained = 0
                    while not device.voice_queue.empty():
                        try:
                            device.voice_queue.get_nowait()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
                    if drained:
                        log.debug(f"[{device.device_id}] Continuation: drained {drained} stale frames")
                    # Re-arm listening state for the follow-up turn.
                    device.listening = True
                    await em_leds.leds_listening(device)
                    await em_device.push_device_state(device)
                    turn_label      = "continuation"
                    preroll_discard = 0
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                else:
                    break

    finally:
        # Drain voice_queue BEFORE clearing oww_paused. If we clear first,
        # handle_data immediately starts routing new frames to mic_queue —
        # correct. But voice_queue still contains frames that arrived during
        # the turn (post-TTS playback, during the buffer drain sleep). Those
        # frames will sit in voice_queue until the NEXT wake detection flips
        # oww_paused back, at which point they arrive at _stream_mic_audio as
        # preamble before the user has said anything — Whisper then transcribes
        # 10+ seconds of ambient noise mixed with the actual utterance.
        # Draining here, while oww_paused is still set, ensures voice_queue is
        # empty before routing flips. The post-turn drain in wake_word_listener
        # (after _run_voice_locked returns) becomes a belt-and-braces no-op.
        _drained = 0
        while not device.voice_queue.empty():
            try:
                device.voice_queue.get_nowait()
                _drained += 1
            except asyncio.QueueEmpty:
                break
        if _drained:
            log.info(
                f"[{device.device_id}] oww_paused drain: "
                f"{_drained} stale frames cleared before routing flip"
            )
        if device.listen_session is not None:
            # Every exit path, including a cancel mid-command: the Echo must
            # not keep sending for a turn that is over.
            with contextlib.suppress(Exception):
                await device.listen_close(reason="turn_end")
        device.oww_paused.clear()
        device.oww_paused_since = None
        log.info(f"[{device.device_id}] oww_paused cleared")
        # Release the arbitration claim so another device answering a
        # genuinely new utterance isn't suppressed by a stale window.
        em_state.wake_arbiter.release(device.device_id)
        # Conversation over — un-pause a media session this turn preempted.
        await em_player.resume_interrupted(device.device_id)


async def _await_barge_decision(device: em_device.Device) -> None:
    """Wait for a barge's claim to resolve before the turn loop acts on it.

    A barge sets barge_detected, flushes, then holds its claim. A reply the
    flush ends inside the hold ends the turn early, and the loop started the
    interrupting turn before the claim's cancel_event landed on it, or before
    barge_ceded said another Echo had won (VVV 2026-10-23: reply cut, barge
    turn gone in 199ms). Bounded so a lost decision cannot hang the loop.
    """
    if device.barge_detected and not device.barge_decided.is_set():
        try:
            await asyncio.wait_for(device.barge_decided.wait(), BARGE_DECISION_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning(f"[{device.device_id}] barge decision not made in "
                        f"{BARGE_DECISION_TIMEOUT_S}s — acting on it as it stands")
