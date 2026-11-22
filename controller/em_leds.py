"""
The ring of LEDs as a turn drives it: off, listening, the thinking spin,
and the end-of-turn outcome animation.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import logging

import em_device

log = logging.getLogger("echomuse")


# LEDs
NUM_LEDS = 12


def _make_leds(r, g, b):
    return [{"id": i, "r": r, "g": g, "b": b} for i in range(NUM_LEDS)]


async def leds_off(device: em_device.Device):
    if device.led_anim_capable:
        await device.send_led_anim({"pattern": "off"})
    else:
        await device.set_leds(_make_leds(0, 0, 0))


# Turn outcomes that get a distinguishing ring cue at turn end. Everything
# else ("ok", "cancelled", "barged") ends silently: the user either heard a
# reply or cut it off themselves, so a cue would be noise.
#
# Rhythm carries the meaning, not colour — a new colour would collide with
# red (mute), orange (link down) or cyan (volume), and the point of the
# cue is to be understandable without a legend. One slow throb reads as
# "nothing heard"; fast blinks read as "something went wrong".
_OUTCOME_ANIM = {
    "no_speech":     "nospeech_anim",
    "no_tts":        "error_anim",
    "tts_error":     "error_anim",
    "timeout":       "error_anim",
    "stream_timeout": "error_anim",
    # Not an error — HA took the wake word and ended the run deliberately,
    # which the satellite setup flow does on every prompt. Needs a cue
    # because the turn is over in milliseconds: without one the ring lights
    # and clears too fast to register, and a device that worked perfectly
    # looks like it glitched.
    "pipeline_refused": "ack_anim",
    # Nothing upstream to answer: no ESPHome server, or HA has never dialled
    # this device's port. The turn dies in milliseconds like the refusal
    # above, but the causes are opposite — there the pipeline answered and
    # declined, here there is no pipeline — so the cue says so in the colour
    # the device already uses for a missing link. See em_scenes.no_ha_anim.
    "no_ha":         "no_ha_anim",
    # HA chose to say nothing ("never mind"). Without a cue the ring went
    # from thinking to dark, which read as cut off. See em_scenes.done_anim.
    "silent_reply":  "done_anim",
}


# How long the listening ring is held before the no_ha cue replaces it. The
# wake word was heard, and an orange fault flashed on its own reads as "the
# device did nothing"; the ack has to land first. Costs the wake listener the
# same delay before it restarts, which is the price of the ring saying
# anything at all on a turn that lasted milliseconds.
NO_HA_HOLD_S = 0.6


async def leds_turn_end(device: em_device.Device):
    """
    Clear the ring at turn end, playing a brief self-clearing cue first if
    the turn ended in a way the user would otherwise have no signal for.

    The cue anims carry a 1s TTL, so the device retires them on its own
    ticker with no follow-up message — nothing to leak if the controller
    dies in between, and a continuation/barge repaint simply supersedes it
    via the animator's generation counter.
    """
    outcome = device.last_turn_outcome
    device.last_turn_outcome = None
    key = _OUTCOME_ANIM.get(outcome or "")
    # Barge-in re-enters a fresh turn immediately and repaints listening —
    # a cue there would flash for a few frames and read as a glitch.
    if key and device.led_anim_capable and not device.barge_detected:
        anim = device.led_scene.get(key)
        if anim:
            log.info(f"[{device.device_id}] Turn ended '{outcome}' — ring cue")
            if outcome == "no_ha":
                # The listening ring is still lit from turn start (30s TTL),
                # so this is a hold, not a repaint.
                await asyncio.sleep(NO_HA_HOLD_S)
            await device.send_led_anim(anim)
            return
    await leds_off(device)


async def leds_listening(device: em_device.Device):
    if device.led_anim_capable:
        await device.send_led_anim(device.led_scene["listening_anim"])
    else:
        await device.set_leds(device.led_scene["listening"], listening=True)


async def leds_spin_green(device: em_device.Device, stop_event: asyncio.Event):
    # Name is historical — the spinner renders whatever the device's scene
    # says (head+trail dot for solid scenes, rotating palette for pride).
    #
    # led_anim firmware animates locally: one message starts the spinner,
    # the device runs it on its own ticker (controller event-loop stalls
    # and WiFi jitter can't judder it), and this task just waits to send
    # the stop. Legacy firmware falls back to controller-rendered frames.
    if device.led_anim_capable:
        try:
            await device.send_led_anim(device.led_scene["spin_anim"])
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await leds_off(device)
        return
    spin_frame = device.led_scene["spin_frame"]
    pos = 0
    try:
        while not stop_event.is_set():
            await device.set_leds(spin_frame(pos))
            pos = (pos + 1) % NUM_LEDS
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        pass
    finally:
        await leds_off(device)
