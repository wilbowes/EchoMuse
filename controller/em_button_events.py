"""
What an Action-button press does on the controller: start or cancel a turn,
dismiss an alarm, or pass the gesture to Home Assistant. The decision of
what a press means is em_button's; this carries it out.

Moved out of em_controller (2026-11) with no change to the body.
"""

import asyncio
import logging

import em_button
import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_leds
import em_tasks
import em_turn

log = logging.getLogger("echomuse")


async def handle_button_event(device: em_device.Device, event: dict):
    click_type = event.get("clickType")
    down       = event.get("down", True)

    if down:
        return

    if click_type == 138:   # DotClick
        # A ringing timer alarm eats the next tap: silencing it is what the
        # user means by pressing the button, not starting a voice turn. Works
        # while muted (the button event still flows over the control plane)
        # and needs no firmware support.
        if device.timer_alarm_task is not None:
            log.info(f"[{device.device_id}] Dot button — dismissing timer alarm")
            await esphome.dismiss_timer_alarm(device.device_id)
            return

        # A HOLD is a separate gesture, forwarded to HA rather than starting a
        # turn. heldMs is measured on the device: timing the down/up messages
        # here would be at the mercy of RTT excursions measured past 1600ms on
        # this fleet, which would misread taps as holds.
        #
        # Absent heldMs (firmware predating this) reads as a tap, so the action
        # button keeps working exactly as before on devices that have not
        # updated — degrade to old behaviour, never to a wrong answer.
        held_ms = event.get("heldMs") or 0
        # Mute is read from the EVENT where the firmware reports it, falling
        # back to the last mute_state message. The event is authoritative: it
        # carries the state at the instant of the press, where device.muted is
        # whatever the last message left behind.
        muted = bool(event.get("muted", device.muted))
        action = em_button.decide(
            held_ms=held_ms,
            hold_ms=esphome.BUTTON_HOLD_MS,
            muted=muted,
            turn_active=device.voice_lock.locked(),
            # ANDed with the capability — see em_button.decide.
            tap_event=(
                device.button_single_tap_event and device.button_hold_capable
            ),
            has_mic=device.mic_capable,
        )

        if action == em_button.HOLD:
            log.info(f"[{device.device_id}] Dot button held {held_ms}ms → HA event")
            esphome.send_button_event(device.device_id, "long")
            return

        if action == em_button.TAP_EVENT:
            window_ms = device.button_multi_tap_ms
            if window_ms <= 0:
                log.info(f"[{device.device_id}] Dot button tap → HA event (single)")
                esphome.send_button_event(device.device_id, "single")
                return

            device.tap_burst.tap(window_ms)
            log.info(
                f"[{device.device_id}] Dot button tap "
                f"{device.tap_burst.count} in burst (window {window_ms}ms)"
            )
            return

        if action == em_button.NO_MIC:
            log.info(f"[{device.device_id}] Dot button tap — no microphone, no turn")
            if device.led_anim_capable and (anim := device.led_scene.get("error_anim")):
                await device.send_led_anim(anim)
            return

        if action == em_button.BLOCKED:
            # Only the TURN is blocked. The hold and tap-event above have
            # already been forwarded, muted or not.
            log.info(f"[{device.device_id}] Dot button tap ignored — mic is muted")
            return

        if action == em_button.CANCEL:
            log.info(f"[{device.device_id}] Dot button — cancelling voice turn")
            device.cancel_event.set()
            esphome.cancel_voice_turn(device.device_id, reason="cancelled")
            # Flush the device's speaker too, or cancelling DURING the spoken
            # response only stops the controller feeding it: the ring clears
            # while up to ~5.5s already in audioChanDepth plays out, and the
            # device carries on talking after you have visibly cancelled it.
            #
            # cancel_event alone cannot fix that — it aborts our end, not the
            # audio already on the device. Mute and barge-in both send this
            # for exactly the same reason; the button was the one deliberate
            # cancel that did not.
            await device.send_control({"type": "speaker_flush"})
        elif not esphome.can_serve_turn(device.device_id):
            # Same stand-down as the wake path, and it needs to be here too:
            # the button is the control someone reaches for precisely when
            # the wake word appears to have done nothing, so it is the worst
            # one to answer with silence. No arbitration to consider — a
            # press names its device — so this is only the cue and the row.
            log.info(f"[{device.device_id}] Dot button but no HA connection — standing down")
            em_dbwriter.submit(db.log_device,
                device.device_id, "info", "controller",
                "Button pressed but no HA connection"
            )
            await em_leds.leds_listening(device)
            await esphome.record_dropped_wake(device, "button", None)
            await em_leds.leds_turn_end(device)
        else:
            log.info(f"[{device.device_id}] Dot button → voice turn")
            device.cancel_event.clear()
            device.oww_paused.set()
            device.oww_paused_since = asyncio.get_event_loop().time()
            async def _button_voice_turn():
                # Button is a deliberate act with no dead zone cost — nothing
                # is being said at the moment of press, so stop/start RTT is
                # fine. Stop the running ch6 stream, restart with lock_mic:true
                # so streamMic calls beam.Lock(beamformingEnabled) and the
                # beamformer selects the best perimeter mic for this turn.
                # mic_start_turn() no-ops if already running, so stop first.
                await device.mic_stop()
                await device.mic_start_turn()
                await em_turn.run_voice_locked(device, trigger_label="button", is_wakeword=False)
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                # Post-turn: back to ch6 omni for OWW listening. mic_stop
                # first: if the turn had no TTS (cancel/error/no-speech), the
                # lock_mic stream from mic_start_turn is still running and a
                # bare mic_start would no-op against it — leaving the GATED,
                # beam-locked turn stream as the permanent wake stream. Safe
                # now that streamMic's exit has the ownership check (the
                # stop/start pair can no longer leak a second stream).
                await device.mic_stop()
                await device.mic_start()
            # Held and logged by em_tasks. The M1 fix (2026-07-05) logged
            # exceptions but kept its reference in a local that died on return.
            em_tasks.spawn(_button_voice_turn())
