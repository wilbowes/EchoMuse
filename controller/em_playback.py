"""
Playing a reply or an announcement on an Echo after a turn: the buffered
and streamed paths, the meter that lights the ring when sound starts, and the
per-device limiter and bass guard for streams the controller processes.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import contextlib
import logging

import em_device
import em_eq
import em_limiter
import em_mbc
import em_playstart
import em_wire

log = logging.getLogger("echomuse")


def _limiter_for(device):
    """Adapter: a Device's limiter config -> em_limiter.for_stream."""
    return em_limiter.for_stream(
        em_wire.SPEAKER_RATE,
        device.limiter_enabled,
        device.limiter_threshold,
        device.limiter_release,
    )


def _guard_for(device):
    """Adapter: a Device's bass-guard config -> em_mbc.for_stream."""
    return em_mbc.for_stream(
        em_wire.SPEAKER_RATE,
        device.bass_guard_enabled,
        device.bass_guard_db,
    )


async def run_post_turn_playback(device: em_device.Device, voice_response: bytes) -> None:
    """
    Post-turn timing concern: EQ, stream to device, acoustic-feedback wait.

    voice_response is 48kHz mono S16_LE PCM (em_tts_audio.fetch decodes at the
    wire rate now — no controller-side resample). Returns once the device
    audio buffer has drained (or cancel_event fires), so the caller can
    safely restart the mic without acoustic feedback into the next turn.
    """
    # Built here rather than inside _prepare_pcm so the stages survive the
    # call and can be asked what they actually did — see em_eq.describe_*.
    # One response is one buffer, so these are per-response instances and
    # carry no state between turns.
    on_device = device.output_chain_on_device
    _limiter = None if on_device else _limiter_for(device)
    _guard   = None if on_device else _guard_for(device)
    log.info(
        f"[{device.device_id}] Output chain: "
        + ("on the device" if on_device else
           em_eq.describe_chain(device.eq_bands, device.eq_loudness, _limiter, _guard))
    )
    # EQ is a solid numpy crunch (hundreds of ms for a long response) — run
    # it off the event loop, which otherwise freezes every device's LED
    # frames, shell proxying, and WS handling right as playback starts
    # (observed as spinner stutter and console typing judder).
    def _prepare_pcm() -> bytes:
        if on_device:
            return voice_response
        return em_eq.apply(voice_response, em_wire.SPEAKER_RATE, device.eq_bands,
                           device.eq_loudness, limiter=_limiter,
                           guard=_guard)

    # Held from the moment we commit to playing until the device's own
    # playback_stats says the audio stopped — so it stays set while the device
    # plays out of its ~5.5s buffer, which is exactly the window `speaking`
    # does NOT cover (that clears when the socket write finishes, near-instantly
    # however slow the link is). The timer ring tests it before each burst so a
    # chime cannot interleave with a response on the 0x02 plane; see
    # _ring_timer_alarm. A counter rather than a flag because an announcement
    # can overlap a turn's playback, and try/finally because a cancelled turn
    # that leaked it would block the ring for the life of the process.
    device.speaker_busy += 1
    # Created inside the try, torn down in the finally; None until then, so an
    # error in the EQ step does not turn the finally into a NameError.
    playback = cancel_task = done_task = stream_task = timeout_task = None
    try:
        _t_eq0 = asyncio.get_event_loop().time()
        speaker_pcm = await asyncio.get_event_loop().run_in_executor(None, _prepare_pcm)
        device.playback_eq_ms = int(
            (asyncio.get_event_loop().time() - _t_eq0) * 1000
        )
        log.info(
            f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes "
            f"({len(speaker_pcm)//em_wire.SPEAKER_BYTES} periods) — "
            f"{em_eq.describe_activity(_limiter, _guard)}"
        )
        cancel_task    = asyncio.create_task(device.cancel_event.wait())
        playback       = device.begin_playback()
        done_task      = asyncio.create_task(playback.ended.wait())
        stream_task    = asyncio.create_task(device.stream_speaker(speaker_pcm))
        t_stream_start = asyncio.get_event_loop().time()
        # Opens the delivery window measured against the device's
        # playback_stats report (see Device.playback_send_t0).
        device.playback_send_t0 = t_stream_start

        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during playback")
            stream_task.cancel()
        else:
            if not device.cancel_event.is_set():
                # Wait for the DEVICE to say it finished, rather than estimating.
                #
                # The old code slept `audio_duration - elapsed` and declared
                # completion. Two things made that wrong, and both bite hardest
                # on exactly the links that need the most patience: `elapsed` is
                # socket-write time (which completes near-instantly however slow
                # the wire is) and was *subtracted*, and the estimate had no
                # visibility of how long the device's own buffer took to drain.
                # Measured 2026-07-24: the ring cleared 6.1s before the audio
                # actually stopped on a Retreat turn, 3.2s early on Lounge.
                #
                # playback_stats is emitted once the device's audio channel has
                # drained after EOS, so it is the real end of audio. The timeout
                # is only a backstop for the report never arriving (device drop,
                # pre-v2.9 firmware): generous, because ending the turn early is
                # the failure we are fixing. cancel_event is still raced — a
                # barge-in or a mute usually lands in this window, and an
                # uncancellable wait here is what caused the 5.7s dead window
                # fixed on 2026-07-10.
                audio_duration = len(speaker_pcm) / (em_wire.SPEAKER_RATE * 2) + em_wire.SPEAKER_PRIME_SECONDS
                elapsed        = asyncio.get_event_loop().time() - t_stream_start
                device.playback_send_ms = int(elapsed * 1000)
                timeout        = audio_duration * 2 + 10.0
                log.info(
                    f"[{device.device_id}] Socket write took {elapsed:.1f}s "
                    f"(NOT delivery — see delivery_ms), awaiting device "
                    f"playback_stats (est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
                )
                timeout_task = asyncio.create_task(asyncio.sleep(timeout))
                await asyncio.wait(
                    [done_task, cancel_task, timeout_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if device.cancel_event.is_set():
                    log.info(f"[{device.device_id}] Cancelled during playback drain")
                elif done_task.done():
                    actual = asyncio.get_event_loop().time() - t_stream_start
                    log.info(
                        f"[{device.device_id}] Playback complete "
                        f"(device-confirmed after {actual:.1f}s, est {audio_duration:.1f}s)"
                    )
                else:
                    # Ring held the full backstop. Either the device never
                    # reported (worth knowing) or delivery was pathological.
                    log.warning(
                        f"[{device.device_id}] Playback completion timed out after "
                        f"{timeout:.1f}s with no playback_stats — clearing ring anyway"
                    )
    finally:
        # Every helper task ends here, on every exit. These cancels used to sit
        # at the end of the try, so a playback that was itself cancelled (a
        # barge-in, a dismissed timer chime) left two Event.wait() tasks
        # pending: the one on the playback Event surfaced as "Task was destroyed but
        # it is pending!" once the Event was dropped (dev add-on, 2026-09-25),
        # and the one on cancel_event, which lives as long as the device, never
        # surfaced at all. The stream task kept sending too. Same teardown as
        # _run_streaming_post_turn_playback, minus the await: nothing here may
        # stand between a cancellation and the speaker_busy release below.
        # A cancelled task is scheduled to run its cancellation, so the loop
        # holds it until it finishes; no reference is needed.
        for t in (cancel_task, done_task, stream_task, timeout_task):
            if t is None:
                continue
            if not t.done():
                t.cancel()
            elif not t.cancelled() and (exc := t.exception()) is not None:
                log.warning(f"[{device.device_id}] playback helper failed: {exc!r}")
        device.speaker_busy -= 1
        # Retire the waiter whether or not the device ever reported. A
        # cancelled playback — barge-in, mute, a device that dropped — never
        # gets its report, and a waiter left queued would take the NEXT
        # playback's report and desynchronise every one after it.
        if playback is not None:
            device.end_playback(playback)

        # The real end of audio, not the end of the socket write. The device
        # reports playback_stats once its audio channel drains after EOS, and
        # everything above waits for exactly that — so this is the one place
        # that knows the speaker has actually stopped. Clearing it in the
        # stream task's finally instead dropped the tile out of Speaking
        # seconds early, the same mistake the ring made until 2026-07-24.
        #
        # IN the finally, for exactly the reason speaker_busy is (#366). This
        # sat after the try, so a CANCELLED playback never reached it and
        # `speaking` stayed set for the life of the process. stop_timer_alarm
        # cancels the ring task, and the chime occupies 1.68s of every 2.3s —
        # so roughly three dismissals in four land mid-burst and leak it. The
        # cost is not a wrong tile: the wake listener skips every frame while
        # `speaking` is set and the alarm is not ringing, so the device goes
        # permanently deaf, its parked on-device wake never collected and its
        # ring left to expire on TTL.
        #
        # shield so the clear survives the cancellation that caused it, and
        # suppress so a failure here cannot replace the exception on its way
        # out. The flag itself is assigned synchronously inside _set_speaking,
        # so it is cleared even if the dashboard push never lands.
        with contextlib.suppress(BaseException):
            await asyncio.shield(device._set_speaking(False))


async def run_streaming_post_turn_playback(device: em_device.Device, pcm_chunks,
                                           on_start=None) -> int:
    """
    Play decoded HA TTS while the HTTP response is still arriving.

    The voice-turn path keeps one ffmpeg decoder, one stateful EQ chain, and
    one device 0x02...0x03 stream alive across all synthesized utterances.
    Closing any layer at an arbitrary network boundary would corrupt decoding,
    reset the EQ filters, or turn each chunk into a separate announcement.
    """
    if device.output_chain_on_device:
        log.info(f"[{device.device_id}] Streaming EQ: on the device")
        stream_eq = em_eq.Passthrough()
    else:
        log.info(
            f"[{device.device_id}] Streaming EQ: bands={device.eq_bands} "
            f"loudness={device.eq_loudness}"
        )
        stream_eq = em_eq.StreamingEQ(
            em_wire.SPEAKER_RATE,
            device.eq_bands,
            device.eq_loudness,
            limiter=_limiter_for(device),
            guard=_guard_for(device),
        )
    # Registered BEFORE streaming starts, so a report that arrives while we
    # are still writing has a waiter to resolve. A fresh Event per playback
    # cannot carry a stale set from the previous response, which is what the
    # clear() this replaces was for.
    playback = device.begin_playback()
    # The meter is raised from this playback's own start waiter, registered
    # above with its end waiter. The device holds audio until roughly the
    # prime window is queued, so the byte count the controller sends leads the
    # speaker; only this waiter is the instant its ALSA write begins.
    meter_task = None
    if on_start is not None:
        started = playback.started if device.playback_start_capable else None
        pcm_chunks, meter_task = em_playstart.meter_at_playback_start(
            pcm_chunks, on_start,
            int(em_wire.SPEAKER_PRIME_SECONDS * em_wire.SPEAKER_RATE * 2),
            started)
    cancel_task = asyncio.create_task(device.cancel_event.wait())
    done_task   = asyncio.create_task(playback.ended.wait())
    stream_task = asyncio.create_task(
        device.stream_speaker_chunks(pcm_chunks, stream_eq)
    )
    t_stream_start = asyncio.get_event_loop().time()

    try:
        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during streamed playback")
            return 0

        total_pcm, eq_ms, first_send_time, send_ms, paced_ms = stream_task.result()
        device.playback_eq_ms = eq_ms
        device.playback_send_ms = send_ms
        stream_elapsed = asyncio.get_event_loop().time() - t_stream_start
        audio_duration = total_pcm / (em_wire.SPEAKER_RATE * 2) + em_wire.SPEAKER_PRIME_SECONDS

        if not total_pcm:
            log.info(f"[{device.device_id}] Streamed response contained no audio")
            return 0

        # Wait for the DEVICE to say it finished, exactly as the buffered path
        # does — do NOT sleep a computed `audio_duration - elapsed`.
        #
        # That estimate was removed on 2026-07-24 because it has no visibility
        # of the device's own buffer and cleared the ring 6.1s early on Retreat
        # and 3.2s on Lounge. Streaming makes it worse, not better: the time
        # spent streaming already covers most of the audio duration, so the
        # remainder computes to ~0 and the wait vanishes entirely while up to
        # ~5.5s is still queued in audioChanDepth. playback_stats is emitted
        # once that channel drains after EOS, so it is the real end of audio.
        # The timeout is only a backstop for the report never arriving (device
        # drop, pre-v2.9 firmware). cancel_event is still raced so a barge-in
        # or mute lands promptly.
        timeout = audio_duration * 2 + 10.0
        log.info(
            f"[{device.device_id}] Streamed {total_pcm} bytes "
            f"({total_pcm//em_wire.SPEAKER_BYTES} periods) in {stream_elapsed:.1f}s "
            f"while HA generated audio (socket writes {send_ms}ms — NOT "
            f"delivery, see delivery_ms; paced {paced_ms}ms holding "
            f"{em_wire.VOICE_LEAD_S:.0f}s of lead); awaiting device playback_stats "
            f"(est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
        )
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))
        try:
            await asyncio.wait(
                [done_task, cancel_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)

        if device.cancel_event.is_set():
            log.info(f"[{device.device_id}] Cancelled during streamed buffer drain")
        elif done_task.done():
            log.info(f"[{device.device_id}] Streamed playback complete (device reported)")
        else:
            log.warning(
                f"[{device.device_id}] No playback_stats after {timeout:.1f}s — "
                f"continuing (device drop or pre-v2.9 firmware)"
            )
        return total_pcm
    finally:
        # See _run_post_turn_playback: the end of audio is what the DEVICE
        # reports, not when the last byte reached the socket. In the finally so
        # a cancel or an error leaves the tile idle rather than stuck Speaking.
        await device._set_speaking(False)
        if meter_task is not None:
            meter_task.cancel()
            await asyncio.gather(meter_task, return_exceptions=True)
        device.end_playback(playback)
        for t in (cancel_task, done_task):
            t.cancel()
        if not stream_task.done():
            stream_task.cancel()
        await asyncio.gather(cancel_task, done_task, stream_task, return_exceptions=True)
        # Close the async generator explicitly rather than leaving it to the
        # event loop's asyncgen hooks: its finally is what kills ffmpeg, and on
        # a barge-in (a routine path here) deferring that to GC leaves a decoder
        # running for an indeterminate window.
        aclose = getattr(pcm_chunks, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as e:
                log.debug(f"[{device.device_id}] TTS stream close: {e}")
