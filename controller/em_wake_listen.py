"""
The controller-side wake listener for each Echo: supervision that restarts
it with backoff, the choice between private listening and the continuous
stream, and the stream loop that scores the wake word and starts the turn.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import logging

import numpy as np

import em_alarm
import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_events
import em_leds
import em_listen
import em_oww_models
import em_oww_warmup
import em_player
import em_private_listen
import em_process_stats
import em_shadow
import em_state
import em_tasks
import em_turn
import em_wake_claim
import em_wake_model
import em_wakeword
import em_wire

log = logging.getLogger("echomuse")


# Control-plane RTT probing. 5s rather than the old 30s keepalive cadence:
# characterising jitter needs samples, and one tiny JSON message per device
# per 5s is negligible next to the 256 kbps continuous mic upload each
# device already holds open. Samples are aggregated in memory and flushed on
# the existing ~30s stats report, so the DB cost is unchanged.
# How long oww_paused may be set with NO turn holding voice_lock before
# the wake loop treats it as stuck and clears it. Generous: a genuine
# turn can run to the spinner's 135s TTL, and this only ever fires when
# the lock is ALSO free, which no running turn allows.
OWW_PAUSE_STUCK_S = 180.0


# How long a freshly established data connection gets to deliver its first
# mic frame before the wake watchdog stops treating the silence as
# "device still warming up" and starts the zombie-stream ladder (#299,
# review on PR #311: a boolean guard made the Office-style zombie — deaf
# for hours — unrecoverable, because no frame ever arrives on the fresh
# connection and only the escalation repairs it).
FRESH_CONN_GRACE_S = 30.0


# Restarting the wake listener must never become a hot loop. A listener that
# raises IMMEDIATELY on start would otherwise be recreated from its own done
# callback as fast as the event loop can run it, which starves everything
# else: control-plane pings stop, and the device drops the connection on its
# own 10s ping timeout. Measured on 2026-08-20 as a device that disconnected
# and reconnected on every barge-in — the supervision turned a silent death
# into a visible one, and then into an outage.
WAKE_RESTART_BACKOFF_S     = 1.0    # first retry


WAKE_RESTART_MAX_BACKOFF_S = 60.0   # ceiling; never gives up entirely


# Ran at least this long before dying = a fresh incident, not a loop. Without
# it an occasional fault would inherit the backoff of an unrelated one from
# hours earlier and take a minute to recover from something momentary.
WAKE_RESTART_HEALTHY_S     = 60.0


def supervise_wake_listener(device: "em_device.Device", failures: int = 0) -> asyncio.Task:
    """
    Run wake_word_listener, and put it back if it ever falls over.

    A bare create_task was the whole failure mode. wake_word_listener catches
    only CancelledError, so ANY other exception ended the task — and because
    the connection handler holds a reference, asyncio never garbage-collects
    it and never logs "Task exception was never retrieved". The device then
    scores wake words, reports them, and nothing consumes them: `pending_wake`
    is read inside the dead loop. From the outside the device looks healthy
    and simply stops answering, with not one line to say so. Only a device
    reconnect or a controller restart brought it back.

    This is the second time this shape has bitten. A NameError in this module
    once killed wake listening fleet-wide, and the response was to fix the
    NameError — which leaves the fragility. The most important loop in the
    controller should not be able to die quietly.

    Restarts are logged at ERROR and are NOT rate-limited into silence: a loop
    that keeps dying is worth a noisy log, because the alternative is a device
    that is deaf and says nothing. Cancellation is the ordinary teardown path
    and is left alone.
    """
    def _restart(task: asyncio.Task) -> None:
        if task.cancelled():
            return                      # ordinary teardown
        # A superseded listener stands down by returning cleanly (see the guard
        # at the top of wake_word_listener): the device reconnected and a newer
        # listener already owns it. That is ordinary teardown, not a death — so
        # neither log it as one nor restart (restarting would leave an orphan
        # scoring frames for a device this object no longer represents).
        if em_state.devices.get(device.device_id) is not device:
            log.info(
                f"[{device.device_id}] wake word listener superseded by a "
                f"newer connection — standing down"
            )
            return
        ran_for = asyncio.get_event_loop().time() - started
        exc = task.exception()
        if exc is None:
            # A current listener's loop is `while True` and never returns on its
            # own, so a normal return here is still a death.
            log.error(
                f"[{device.device_id}] wake word listener returned "
                f"unexpectedly after {ran_for:.0f}s — restarting. The device "
                f"was deaf until now."
            )
        else:
            log.error(
                f"[{device.device_id}] wake word listener crashed after "
                f"{ran_for:.0f}s — restarting. The device was deaf until now.",
                exc_info=exc,
            )
        # A listener that ran a while and then fell over is a fresh incident;
        # one that dies instantly, every time, is a loop. Only the second
        # needs slowing down, and conflating them would make an occasional
        # fault take progressively longer to recover from.
        n = 1 if ran_for >= WAKE_RESTART_HEALTHY_S else failures + 1
        delay = min(WAKE_RESTART_BACKOFF_S * (2 ** (n - 1)),
                    WAKE_RESTART_MAX_BACKOFF_S)

        async def _later() -> None:
            await asyncio.sleep(delay)
            if em_state.devices.get(device.device_id) is not device:
                return
            device.oww_task = supervise_wake_listener(device, failures=n)

        if n > 1:
            log.error(
                f"[{device.device_id}] wake word listener has now failed "
                f"{n} times in a row — retrying in {delay:.0f}s"
            )
        em_tasks.spawn(_later())

    started = asyncio.get_event_loop().time()
    t = asyncio.create_task(wake_word_listener(device))
    t.add_done_callback(_restart)
    return t


async def _after_model_release(device_id: str) -> None:
    """Hand freed heap back to the OS and say what is still loaded: a model
    that outlives its release is the #512 leak again, and this line is
    where it shows."""
    await asyncio.to_thread(em_process_stats.release_freed_memory)
    log.info(f"[{device_id}] wake models released; "
             f"{len(em_state.live_oww_models)} still loaded on this controller")


async def wake_word_listener(device: em_device.Device):
    """
    Listen for this Echo's wake word, whichever side detects it.

    Two paths, chosen per Echo from what it reports (docs/listening.md):
    `_private_listen` when the Echo scores its own wake word and sends nothing
    until it fires, `_stream_listen` when it streams continuously and this
    controller scores it (or, for firmware from before private listening, when
    it streams and triggers). Each returns when the Echo moves to the other,
    so a mode change in the dashboard takes effect without a reconnect. Only
    a superseded connection ends this loop.
    """
    while em_state.devices.get(device.device_id) is device:
        if not device.mic_capable:
            await em_private_listen.no_mic(device)
        elif em_listen.awaiting_report(device.listen_view):
            await em_private_listen.await_listen_report(device)
        elif device.private_listening:
            # Nothing here scores a private Echo, so its cached models (three
            # ONNX sessions each) are released, not held for the life of the
            # process; going back to streaming rebuilds them.
            if em_state.forget_oww_models(device.device_id):
                em_tasks.spawn(_after_model_release(device.device_id))
            await em_private_listen.private_listen(device)
        else:
            await _stream_listen(device)


async def _stream_listen(device: em_device.Device):
    loop = asyncio.get_event_loop()

    current_model_name = device.oww_model
    current_speex_ns    = device.oww_speex_ns
    log.info(
        f"[{device.device_id}] OWW: loading model {current_model_name} "
        f"(speex_ns={current_speex_ns})"
    )
    # Reused across reconnects via the module cache (#512): a fresh Device is
    # created per connection, so building on the Device leaked a model per
    # reconnect. A cache hit is reset() to match a fresh build.
    model = await em_wake_model.acquire_wake_model(device, current_model_name, current_speex_ns)
    # NB: for custom models owwModel is a file path but openwakeword keys
    # the prediction dict by the filename stem — never score by the raw name.
    model_key = em_oww_models.prediction_key(current_model_name)

    # The Echo may have gone private, or still be waiting to report, while the
    # model loaded: never send mic_start into that (em_listen.awaiting_report).
    if device.private_listening or em_listen.awaiting_report(device.listen_view):
        return
    log.info(f"[{device.device_id}] OWW: starting (initial threshold={device.oww_threshold:.3f})")
    await device.mic_start()

    buf = bytearray()
    # openwakeword seeds its classifier window with embeddings of random noise
    # on construction AND on every reset(), so scores are partly scores of that
    # noise until the window has refilled. Un-warmed at construction to match
    # the model above; re-armed at every reset() below. See em_oww_warmup.
    warmup = em_oww_warmup.WarmupGate()
    last_near_miss_log_ts = 0.0  # Q4: rate-limit near-miss INFO logging to 1/2s
    nm_pending = 0    # near-misses buffered since the last hourly-rollup flush
    nm_max     = 0.0  # highest buffered near-miss score
    dead_streak = 0   # consecutive 10s mic_queue timeouts (resets on any frame)
    stray_stopped_at = 0.0  # last mic_stop sent for a stream up with the wake word off
    backlog_log = em_listen.BacklogLog()
    try:
        while True:
            # Now that the model is shared via the module cache (#512), a
            # superseded listener must not score into it: on a reconnect the
            # old listener can still loop for a moment before its connection's
            # teardown cancels it, and two listeners scoring one stateful model
            # corrupt each other's feature window. Only the current connection's
            # listener (the one still in em_state.devices) may touch the model; a
            # superseded one stands down here. The replacement listener reset()s
            # the model on acquire, wiping any residue from this one.
            if em_state.devices.get(device.device_id) is not device:
                return
            if device.private_listening:
                # The Echo now listens privately; wake_word_listener switches.
                return
            if device.oww_model != current_model_name or device.oww_speex_ns != current_speex_ns:
                new_name  = device.oww_model
                new_speex = device.oww_speex_ns
                log.info(
                    f"[{device.device_id}] OWW: reloading model "
                    f"{current_model_name} → {new_name} "
                    f"(speex_ns {current_speex_ns} → {new_speex})"
                )
                try:
                    # Replaces this device's cached model (dropping the old
                    # one), so a changed wake word does not accumulate models.
                    new_model = await em_wake_model.acquire_wake_model(
                        device, new_name, new_speex
                    )
                    model             = new_model
                    model_key         = em_oww_models.prediction_key(new_name)
                    current_model_name = new_name
                    current_speex_ns  = new_speex
                    buf.clear()
                    # A new model is noise-seeded exactly like a reset one.
                    warmup.reset()
                    log.info(f"[{device.device_id}] OWW: model reloaded → {new_name} (speex_ns={new_speex})")
                except Exception as e:
                    log.error(
                        f"[{device.device_id}] OWW: failed to load {new_name} "
                        f"(speex_ns={new_speex}): {e} "
                        f"— reverting to {current_model_name} (speex_ns={current_speex_ns})"
                    )
                    device.oww_model     = current_model_name
                    device.oww_speex_ns  = current_speex_ns
            try:
                payload = await asyncio.wait_for(
                    device.mic_queue.get(), timeout=10.0
                )
                # #299: a frame on this connection — the watchdog's link
                # guard below may stand down from here on.
                device.frames_seen_this_connection = True
            except asyncio.TimeoutError:
                # The wake stream is ungated and continuous (device sends
                # every 80ms, silence included — hardware mute still produces
                # zero-filled frames), so 10s of nothing on mic_queue means
                # the stream died — NOT ordinary silence, as it did when the
                # device VAD gate existed on this stream. Exception: during a
                # voice turn frames route to voice_queue instead, so an idle
                # mic_queue is expected while oww_paused is set.
                if device.oww_paused.is_set():
                    # Normally correct: during a turn the frames go to
                    # voice_queue, so an idle mic_queue means nothing.
                    #
                    # But this branch is also why a stuck flag is INVISIBLE.
                    # While it is set the wake loop drops every frame and this
                    # watchdog stands down, so the device is deaf with nothing
                    # to notice or end it — no error, no warning, and the
                    # device itself keeps scoring happily and reporting wakes
                    # that nothing consumes (`pending_wake` is read further
                    # down this same loop). Seen on 2026-08-20; only an add-on
                    # restart brought it back.
                    #
                    # Paused with NO turn holding voice_lock is a state that
                    # cannot be legitimate, so it is safe to end. The lock is
                    # the discriminator rather than the elapsed time alone: a
                    # long turn is fine, and the spinner TTL alone runs to
                    # 135s. Only the combination is broken.
                    stuck_for = (
                        loop.time() - device.oww_paused_since
                        if device.oww_paused_since is not None else 0.0
                    )
                    if (not device.voice_lock.locked()
                            and stuck_for > OWW_PAUSE_STUCK_S):
                        log.error(
                            f"[{device.device_id}] oww_paused stuck for "
                            f"{stuck_for:.0f}s with no voice turn holding the "
                            f"lock — clearing. The device was deaf for that "
                            f"long; this is a bug, please report it with the "
                            f"lines above."
                        )
                        device.oww_paused.clear()
                        device.oww_paused_since = None
                    continue
                if not em_wakeword.wake_allowed(mic_muted=device.muted,
                                                enabled=device.wake_word_enabled):
                    # Hardware mute is device-sovereign: the device rejects
                    # every mic_start while muted, so a silent stream is the
                    # expected state — retrying just spams both logs every
                    # 10s. The device restarts its own wake stream on unmute
                    # (and device.muted clears with the mute_state message),
                    # so the watchdog resumes naturally if that ever fails.
                    # Same for the wake word being off (#286).
                    dead_streak = 0
                    continue
                # #299: "no frames" has two causes, and the ladder below
                # assumes only one (a zombie stream device-side still holding
                # micActive). The distinction needs BOTH state and time:
                #
                #   - data plane fully down → frames are explained by the
                #     transport; nothing to point a repair at. Stand down,
                #     do not count the outage against the streak.
                #   - connected, but no frame since the connection opened:
                #     ambiguous! Freshness says give the device a moment;
                #     but past the grace window this silence IS the
                #     zombie-stream signature — the device still streams to
                #     a superseded socket, no frame will EVER arrive on the
                #     fresh one, and the escalation below is the only repair
                #     (Office, 2026-07-16: deaf 4.7h without it — which is
                #     exactly why this guard is time-bounded rather than a
                #     boolean, per the review on PR #311).
                if device.data_ws is None:
                    continue
                if not device.frames_seen_this_connection:
                    if loop.time() - device.data_connected_at < FRESH_CONN_GRACE_S:
                        log.debug(
                            f"[{device.device_id}] OWW: no mic frames yet on a "
                            f"fresh data connection — giving it "
                            f"{FRESH_CONN_GRACE_S:.0f}s before treating the "
                            f"silence as a zombie stream"
                        )
                        continue
                    log.warning(
                        f"[{device.device_id}] OWW: data connection is up but "
                        f"delivered no frame in {FRESH_CONN_GRACE_S:.0f}s — "
                        f"treating as zombie stream"
                    )
                dead_streak += 1
                if dead_streak < 3:
                    log.warning(
                        f"[{device.device_id}] OWW: no mic frames for 10s on the "
                        f"continuous wake stream — sending defensive mic_start"
                    )
                    await device.mic_start()
                else:
                    # Bare mic_start hasn't worked — the classic cause is a
                    # zombie stream device-side still holding micActive
                    # against a superseded data connection (Office,
                    # 2026-07-16: deaf 4.7h while every bare mic_start was
                    # refused "already active"). mic_stop releases whatever
                    # stream exists, wherever it points; the fresh mic_start
                    # then lands on the live connection. Safe at this point
                    # by construction: 30s+ of zero frames with no turn in
                    # flight (oww_paused checked above) means there is no
                    # healthy stream to interrupt.
                    log.warning(
                        f"[{device.device_id}] OWW: no mic frames for "
                        f"{dead_streak * 10}s and defensive mic_start isn't "
                        f"helping — escalating to mic_stop + mic_start"
                    )
                    await device.mic_stop()
                    await device.mic_start()
                continue

            dead_streak = 0

            # Behind by more than a second: skip to the last half-second
            # (#946). Spliced, not reset: reset() seeds the model with noise.
            payload, skipped = em_listen.skip_backlog(
                device.mic_queue, payload, loop.time())
            if skipped:
                buf.clear()
                device.wake_levels.clear()
                if note := backlog_log.add(skipped, loop.time()):
                    log.warning(f"[{device.device_id}] OWW: {note}")
            if payload is None:
                continue

            # VAD sentinel (string; None accepted defensively — the pre-B5
            # encoding) — flush partial audio so OWW never scores across a
            # stream boundary.
            if payload is None or isinstance(payload, str):
                buf.clear()
                device.wake_levels.clear()
                continue

            if device.oww_paused.is_set():
                continue

            if not em_wakeword.wake_allowed(mic_muted=device.muted,
                                            enabled=device.wake_word_enabled):
                buf.clear()
                device.wake_levels.clear()
                if (em_wakeword.stray_stream(mic_muted=device.muted,
                                             enabled=device.wake_word_enabled)
                        and loop.time() - stray_stopped_at > 2.0):
                    # Once per 2s: frames already in flight keep arriving
                    # for a moment after the stop.
                    stray_stopped_at = loop.time()
                    log.info(f"[{device.device_id}] audio arriving with the "
                             f"wake word off — stopping the stream")
                    await device.mic_stop()
                continue

            buf.extend(payload)
            # The chunk scored below is completed by this payload.
            arrived = em_listen.arrival(payload, loop.time())
            heard = em_listen.captured(payload, arrived)
            while len(buf) >= em_wire.CHUNK_BYTES:
                frame   = bytes(buf[:em_wire.CHUNK_BYTES])
                del buf[:em_wire.CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)

                # A ringing timer alarm is the one case where our own speaker
                # output must NOT make us deaf: dismissing it by voice is the
                # point, and the chime occupies 1.68s out of every 2.3s, so
                # skipping frames while it plays would leave almost nothing to
                # score. Same acoustics as barge-in over TTS — AEC subtracts
                # the chime, and the score is judged against the barge
                # threshold below rather than the wake threshold.
                ringing = device.timer_alarm_ringing
                if device.speaking and not ringing:
                    continue

                # Per-room noise floor tracking (measurement only — the audio
                # is never modified). Asymmetric EWMA: follows drops quickly
                # (α=0.3) so it converges down fast, rises slowly (α=0.008 ≈
                # 10s time constant at 12.5 chunks/s) so speech bursts don't
                # drag it up. Feeds the SNR-relative no-speech detection in
                # em_esphome._stream_mic_audio and the diagnostics below.
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                if device.noise_floor == 0.0:
                    device.noise_floor = rms
                elif rms < device.noise_floor:
                    device.noise_floor += 0.3 * (rms - device.noise_floor)
                else:
                    device.noise_floor += 0.008 * (rms - device.noise_floor)
                device.wake_levels.push(rms)

                prediction = await loop.run_in_executor(
                    None, model.predict, samples
                )
                score = em_oww_models.scalar_score(prediction.get(model_key, 0.0))
                # Exactly once per chunk the MODEL saw, whatever the score —
                # the `device.speaking` skip above returns before predict(),
                # so the gate and the model stay in step.
                trusted = warmup.feed()

                # Log any score above noise floor so we can see near-misses
                # and understand whether failed wakes are "close but below
                # threshold" vs "not registering at all".
                #
                # Q4 fix (2026-07-05 review): this was DEBUG-only, invisible
                # in a normal INFO deployment — exactly the data needed for
                # threshold tuning was blind by default. Now: (1) the debug
                # line stays for verbose troubleshooting, (2) an INFO line
                # fires too, rate-limited to at most once per 2s per device
                # so a run of near-misses doesn't flood the log, and (3) a
                # persistent near_misses counter is exposed to the dashboard
                # via device_update so the count is visible without tailing
                # logs at all.
                # Below-threshold only (2026-07-15 fix): scores at or above
                # the wake threshold are detections, not near-misses — the
                # old `score > 0.05` gate counted every successful wake as a
                # near-miss too, inflating both the dashboard counter and
                # the wake_counters rollup (near_miss_max = the wake score).
                # During music playback the mic hears the speaker ~25dB
                # louder than the person, so wake scores are depressed —
                # the same physics barge-in handles during TTS. Score
                # against the (lower) barge threshold while a media
                # session plays, but only when barge-in is enabled: that's
                # the user's opt-in to trusting AEC not to self-trigger.
                eff_threshold = device.oww_threshold
                if device.barge_in_enabled and em_player.is_playing(device.device_id):
                    eff_threshold = min(eff_threshold, device.barge_threshold)
                # A ringing alarm is our own speaker output too, and louder at
                # the mic than the person trying to dismiss it. Same opt-in as
                # above: bargeInEnabled is the user's statement that they trust
                # AEC here. Without it the ring stays dismissable by button and
                # by HA — old behaviour, not a wrong answer.
                if device.barge_in_enabled and ringing:
                    eff_threshold = min(eff_threshold, device.barge_threshold)

                if trusted and 0.05 < score < eff_threshold:
                    device.oww_near_misses += 1
                    nm_pending += 1
                    nm_max = max(nm_max, float(score))
                    log.debug(
                        f"[{device.device_id}] OWW score: {score:.3f} "
                        f"(threshold={device.oww_threshold:.3f}, "
                        f"rms={rms:.4f}, floor={device.noise_floor:.4f})"
                    )
                    now = asyncio.get_event_loop().time()
                    if now - last_near_miss_log_ts >= 2.0:
                        last_near_miss_log_ts = now
                        log.info(
                            f"[{device.device_id}] OWW near-miss: score={score:.3f} "
                            f"(threshold={device.oww_threshold:.3f}, "
                            f"total near-misses={device.oww_near_misses})"
                        )
                        await em_events.push({
                            "type":      "device_update",
                            "device_id": device.device_id,
                            "state":     {"owwNearMisses": device.oww_near_misses},
                        })
                        # Flush the buffered near-miss counts into the hourly
                        # persistent rollup. Riding this rate-limited branch
                        # caps the DB cost at one upsert per 2s per device,
                        # however noisy the room.
                        _nm, _mx = nm_pending, nm_max
                        nm_pending, nm_max = 0, 0.0
                        await loop.run_in_executor(
                            None,
                            lambda: db.bump_wake_counters(
                                device.device_id,
                                near_misses=_nm, near_miss_max=_mx,
                            ),
                        )

                # Who gets to start a turn. In "on" mode the device decides and
                # this controller's own crossing is demoted to a measurement —
                # see em_shadow.decide_wake_source for why it keeps scoring at
                # all. In every other mode this is exactly the old condition.
                if not trusted and score >= eff_threshold:
                    log.info(
                        f"[{device.device_id}] Wake score {score:.3f} >= "
                        f"{eff_threshold:.3f} ignored — openwakeword warm-up, "
                        f"{warmup.progress()} chunks since reset"
                    )
                ctrl_hit = trusted and score >= eff_threshold
                dev_wake, dev_age = device.pending_wake.take()
                if dev_wake is None and dev_age is not None:
                    # Expired before anything could act on it. Worth a warning
                    # rather than a debug line: it is a wake the user spoke and
                    # did not get, and the age is the only evidence of why.
                    log.warning(
                        f"[{device.device_id}] on-device wake dropped — "
                        f"{dev_age:.1f}s old (limit "
                        f"{em_shadow.MAX_PENDING_WAKE_S:.1f}s)"
                    )
                source = em_shadow.decide_wake_source(
                    device.oww_on_device, dev_wake, ctrl_hit
                )
                if trusted or source != "none":
                    device.wake_capture.consider(
                        enabled=device.wake_clip_capture,
                        minimum=device.wake_clip_min_score,
                        score=float(score), threshold=float(eff_threshold),
                        model=current_model_name,
                        device_score=(dev_wake["score"] if dev_wake is not None else None),
                        trigger_source=(source if source != "none" else None),
                    )
                if ctrl_hit and device.oww_on_device == em_shadow.MODE_ON:
                    # "on" mode, and this controller heard it too. Recorded for
                    # the comparison and nothing else — the device is driving.
                    #
                    # Recorded whoever WON, which is the whole point. Gating
                    # this on source == "none" lost the crossing whenever the
                    # device's wake and ours landed on the same iteration — and
                    # the turn then drains mic_queue, so there is no second
                    # chance — writing a NULL that reads as "the controller
                    # missed it" when the controller had in fact scored it
                    # identically. Two of the first three trial turns agreed to
                    # four decimal places and the third recorded a miss for
                    # exactly this reason (2026-08-10). A comparison that is
                    # wrong only in the direction that flatters the feature is
                    # the worst kind.
                    device.ctrl_shadow.record_cross(score, 0)
                    device.ctrl_shadow.active = True

                if source != "none":
                    if source == "device":
                        score      = dev_wake["score"]
                        # The bar the DEVICE cleared, which during playback is
                        # the lower barge-in one. Falls back to ours when the
                        # device did not report one, rather than recording a
                        # threshold the wake was never judged against.
                        if dev_wake["threshold"] is not None:
                            eff_threshold = dev_wake["threshold"]
                    log.info(
                        f"[{device.device_id}] Wake word detected "
                        f"(source={source}, score={score:.3f}, "
                        f"threshold={eff_threshold:.3f}, "
                        f"rms={rms:.4f}, floor={device.noise_floor:.4f}"
                        + (f", scored {(loop.time() - arrived) * 1000:.0f}ms "
                           f"after arrival, {(arrived - heard) * 1000:.0f}ms "
                           f"in transit" if source == "controller" else "")
                        + ")"
                    )
                    em_dbwriter.submit(db.log_device,
                        device.device_id, "info", "device",
                        f"Wake word detected (score={score:.3f}, {source})"
                    )
                    if em_alarm.ringing_devices():
                        # Silence the ring now rather than at turn start: the
                        # person is already speaking by the time the turn is
                        # set up.
                        await em_alarm.hold_alarms()
                        log.info(f"[{device.device_id}] Wake over a ringing "
                                 f"timer — ring held")
                    if not device.voice_lock.locked():
                        # P0-1: do NOT send mic_stop/mic_start_turn.
                        # The stream stays running continuously. Flipping
                        # oww_paused routes subsequent frames to voice_queue.
                        # The VAD gate is already open mid-utterance — that's
                        # how OWW got the wake-word audio — so command audio
                        # flows in with zero re-trigger delay and zero RTT gap.
                        # Wake-word tail bleed ("…Jarvis") is handled by the
                        # preroll discard in _stream_mic_audio.
                        # TTS mic_stop/mic_start remains untouched — that
                        # acoustic-feedback guard is load-bearing.
                        #
                        # The reset runs in the executor and is NOT awaited
                        # here: routing to the turn (oww_paused, below) must
                        # happen on this tick, or frames arriving during the
                        # reset land in mic_queue and the turn drains them as
                        # stale — the start of the command. Nothing scores
                        # with the model until listening resumes, which
                        # awaits it first.
                        model_reset = em_wake_model.reset_model(model)
                        warmup.reset()
                        buf.clear()
                        device.cancel_event.clear()
                        # Wake detail for the turn's persistent record —
                        # popped by esphome.trigger_voice_turn.
                        # float(): OWW scores are numpy float32 — sqlite3
                        # stores those as a 4-byte BLOB, which then breaks
                        # JSON serialisation of the row (2026-07-14).
                        # Monotonic instant of THIS detection, for correlating
                        # the device's shadow crossing at turn-persist time.
                        # em_shadow.now() rather than a clock of our own: both
                        # sides of that subtraction must come from one place,
                        # and this module does not import time (it uses the
                        # event loop's clock) — reaching for time.monotonic()
                        # here raised NameError on the first wake detection and
                        # killed the listener for the rest of the process.
                        #
                        # For a device-triggered wake this is the CROSSING
                        # instant, not now: the device reported how long ago it
                        # fired, and using arrival time instead would fold the
                        # network hop into every comparison and every
                        # arbitration decision — the one thing this fleet's
                        # 1.1-2.6s RTT excursions make certain to matter.
                        device.last_wake_mono = (
                            dev_wake["at"] if source == "device" else em_shadow.now()
                        )
                        device.last_wake = {
                            "model":       model_key,
                            "score":       round(float(score), 4),
                            # The EFFECTIVE threshold this wake actually cleared,
                            # not the nominal one. During playback with barge-in
                            # enabled the bar drops to bargeInThreshold, and
                            # recording 0.5 there produced rows that contradicted
                            # themselves — wake_score 0.055 against
                            # wake_threshold 0.5, i.e. "woke below its own bar"
                            # (present in the data since at least 2026-07-25).
                            # It is also what lets the on-device comparison tell
                            # a real miss from a wake the device was never asked
                            # to look for.
                            "threshold":   round(float(eff_threshold), 4),
                            "noise_floor": round(device.noise_floor, 5),
                        }
                        device.oww_paused.set()
                        device.oww_paused_since = asyncio.get_event_loop().time()
                        log.debug(
                            f"[{device.device_id}] OWW: oww_paused set, "
                            f"routing to voice_queue (no mic_stop/mic_start_turn)"
                        )
                        # Lock the beamformer onto the speaker's perimeter mic
                        # NOW, mid-utterance — the onset detector has the
                        # freshest possible signal at this moment. No stream
                        # restart; released by beam_unlock post-turn (and
                        # implicitly by any TTS mic stop/start cycle).
                        await device.beam_lock()

                        # Multi-device arbitration: if this utterance also
                        # woke another Echo, only the best-placed one should
                        # answer. Capture routing (oww_paused, beam lock) is
                        # already set up above ON PURPOSE — the winner's
                        # command audio must be flowing from the first
                        # syllable, so we arm optimistically and revert on
                        # loss. Solo fleets skip the window entirely.
                        # A device with no pipeline behind it stands down
                        # HERE, before arbitration, and never runs a turn it
                        # cannot finish. Reads what the turn path reads —
                        # the same get_server/get_satellite pair
                        # trigger_voice_turn refuses on — so a device counted
                        # as able cannot turn out to be unable a tick later
                        # for any reason the controller already knows about.
                        serves = esphome.can_serve_turn(device.device_id)
                        won_by = device.device_id
                        if serves and device.wake_arb_ms > 0 and len(em_state.devices) > 1:
                            # Held ARB_HOLD_S from when it was heard, so
                            # every Echo that heard it is counted.
                            # Capture time, not arrival (docs/listening.md):
                            # a device wake reports its age; ours dates from
                            # when its frame was captured (CaptureClock).
                            if source == "device":
                                heard_at = em_listen.heard_at(
                                    loop.time(),
                                    (em_shadow.now() - dev_wake["at"]) * 1000.0,
                                    device.rtt_est.srtt)
                            else:
                                # Already includes the link's least delay.
                                heard_at = heard
                            won_by = await em_wake_claim.claim_wake(
                                device, heard_at,
                                device.wake_levels.measure(device.mic_gain_db),
                                over_playback=bool(ringing or device.speaking))
                        if not serves or won_by != device.device_id:
                            wake_info = device.last_wake
                            device.oww_paused.clear()
                            device.oww_paused_since = None
                            device.last_wake = None
                            await device.beam_unlock()
                            ceded = 0
                            while not device.voice_queue.empty():
                                try:
                                    device.voice_queue.get_nowait()
                                    ceded += 1
                                except asyncio.QueueEmpty:
                                    break
                            if not serves:
                                # No pipeline behind this device. The cue is
                                # about the DEVICE's state, not a turn's
                                # outcome, so it fires whether or not another
                                # Echo took the utterance — that is the whole
                                # point: it says the wake word worked and the
                                # controller is here (the listening ring the
                                # device already lit, held briefly) and Home
                                # Assistant is not (the orange flash). Three
                                # facts, and none of them is a state light:
                                # it self-clears on the device's own ticker.
                                await esphome.record_dropped_wake(
                                    device, f"wakeword({score:.3f})", wake_info
                                )
                                await em_leds.leds_turn_end(device)
                                log.info(
                                    f"[{device.device_id}] Wake heard but no "
                                    f"HA connection — standing down "
                                    f"(score={score:.3f}, discarded {ceded} "
                                    f"frames)"
                                )
                                em_dbwriter.submit(db.log_device,
                                    device.device_id, "info", "controller",
                                    "Wake heard but no HA connection"
                                )
                                await model_reset
                                continue
                            # The loser is lit: since #263 the device draws
                            # the listening ring at its own crossing, before
                            # it can possibly know it will lose. Nothing else
                            # on this path darkens it — leds_off and
                            # _leds_turn_end both live on the turn path a
                            # ceding device never reaches — so without this
                            # the ring burns until listening_anim's 30s TTL
                            # retires it. Plain off, not _leds_turn_end:
                            # there was no turn, so an outcome cue would be
                            # inventing one.
                            await em_leds.leds_off(device)
                            log.info(
                                f"[{device.device_id}] Wake ceded to "
                                f"{won_by} (arbitration; score={score:.3f}, "
                                f"discarded {ceded} frames)"
                            )
                            em_dbwriter.submit(db.log_device,
                                device.device_id, "info", "controller",
                                f"Wake ceded to {won_by} (arbitration)"
                            )
                            await model_reset
                            continue

                        # "wakeword-dev" rather than a separate field: every
                        # reader of trigger already matches on the "wakeword"
                        # prefix (including em_turn_record.persist_turn's shadow block), so
                        # this distinguishes the two sources in the Activity
                        # tab and in queries without any of them changing.
                        label = "wakeword-dev" if source == "device" else "wakeword"
                        # Wake sound (#120), only now that the wake has won
                        # arbitration: a ceded or HA-less wake `continue`d above
                        # and stays silent. A private-listening wake gets its
                        # sound from listen_ack instead, on the Echo.
                        if getattr(device, "wake_sound", False) and device.wake_cue_capable:
                            await device.play_cue("wake")
                        await em_turn.run_voice_locked(device, trigger_label=f"{label}({score:.3f})", is_wakeword=True)
                        # Back to ch6 omni for wake listening. Belt-and-braces
                        # for turns that never restarted the stream (no-TTS
                        # outcomes: error, no-speech, cancel) — a lock left
                        # in place would point wake listening at one
                        # perimeter mic instead of omni.
                        await device.beam_unlock()

                        drained = 0
                        while not device.voice_queue.empty():
                            try:
                                device.voice_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        if drained:
                            log.info(
                                f"[{device.device_id}] OWW: "
                                f"drained {drained} stale frames post-turn"
                            )
                        await model_reset
                        await em_wake_model.reset_model(model)
                        warmup.reset()
                        buf.clear()
                        # mic_start without lock_mic — device stays on ch6 omni
                        # (beamforming=off), same stream as OWW listening.
                        # This is a defensive restart only: if the stream
                        # somehow died during the turn, this revives it.
                        # If already running, the device no-ops it.
                        log.info(f"[{device.device_id}] OWW: defensive mic_start (no lock_mic)")
                        await device.mic_start()
                    else:
                        log.info(
                            f"[{device.device_id}] Voice turn active — "
                            f"ignoring wake"
                        )
                        await em_wake_model.reset_model(model)
                        warmup.reset()

    except asyncio.CancelledError:
        await device.mic_stop()
        raise
