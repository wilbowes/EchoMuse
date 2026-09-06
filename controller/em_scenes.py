"""
LED ring scenes — palettes for the controller-rendered ring states.

A scene decides what "listening" (solid ring) and "thinking" (spinner)
look like. The controller renders every animation frame and the device
just paints it, so scenes are entirely controller-side; device-local
displays keep fixed colours on purpose — the mute ring stays red in every
scene (it's a privacy indicator, not decoration) and the volume arc stays
cyan.

Config keys (global or per-device, pushed live like any other config):
  ledScene        — "standard" | "airy" | "malevolent" | "pride" | "custom"
  ledListenColor  — "#RRGGBB", custom scene only
  ledThinkColor   — "#RRGGBB", custom scene only

resolve(config) returns everything em_controller's LED helpers need:
  listening   — ready-to-send list of 12 {id,r,g,b} dicts
  spin_frame  — fn(pos) -> list of 12 {id,r,g,b} dicts for spinner frame N
"""

NUM_LEDS = 12

# The device's own "no controller" orange, at full brightness — allLEDs(255*br,
# 40*br, 0) in device/cmd/server.go's pulseOrange. Reused rather than picked so
# the two "nothing upstream" signals are the same colour; the device scales
# brightness itself when it renders a pulse.
LINK_ORANGE = (255, 40, 0)


def _solid(r: int, g: int, b: int) -> list:
    return [(r, g, b)] * NUM_LEDS


def _hsv(h: float, s: float, v: float) -> tuple:
    """h in degrees; returns 8-bit RGB."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


# One hue per LED around the wheel — the pride ring. Value capped below
# full so white-ish hues don't visually swamp the saturated ones.
_RAINBOW = [_hsv(i * 360 / NUM_LEDS, 1.0, 0.75) for i in range(NUM_LEDS)]

# Each preset: listening palette (12 triples), spinner head/trail colours,
# and rotate=True to spin the listening palette itself instead of a
# head+trail dot (pride's rotating rainbow).
_PRESETS = {
    "standard": {
        "listening":  _solid(0, 180, 0),
        "spin_head":  (0, 200, 0),
        "spin_trail": (0, 60, 0),
        "rotate":     False,
    },
    "airy": {
        # Pale sky blue — calm, low-saturation.
        "listening":  _solid(80, 150, 200),
        "spin_head":  (150, 205, 255),
        "spin_trail": (25, 45, 70),
        "rotate":     False,
    },
    "malevolent": {
        # Deep crimson-magenta with an ember spinner. Deliberately NOT pure
        # red (180,0,0) — that's the mute ring and must stay unambiguous.
        "listening":  _solid(110, 0, 45),
        "spin_head":  (210, 45, 0),
        "spin_trail": (55, 8, 0),
        "rotate":     False,
    },
    "pride": {
        "listening":  list(_RAINBOW),
        "spin_head":  None,
        "spin_trail": None,
        "rotate":     True,
    },
}


def _hex_to_rgb(value, default: tuple) -> tuple:
    try:
        s = str(value).lstrip("#")
        if len(s) != 6:
            return default
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, TypeError):
        return default


def _leds(palette: list) -> list:
    return [{"id": i, "r": r, "g": g, "b": b} for i, (r, g, b) in enumerate(palette)]


# Seconds added on top of a response's own duration when sizing the meter
# ring's dead-man TTL. Also the placeholder TTL on the base spec.
METER_TTL_PAD = 20

# Dead-man TTL for the thinking spinner. It must outlast everything the
# spinner spans — HA's think time AND the TTS fetch — because the ring is
# showing "working on it" for the whole of both. That makes it coupled to
# _fetch_tts_audio's timeout (60s, x2 attempts): a shorter TTL would clear
# the ring part-way through exactly the long responses that need it most,
# which is the same class of bug as the playback estimate this release
# replaces. Keep these two in step if either moves.
SPIN_TTL = 135

# Meter response-curve config keys → the wire field the device reads, with
# the range the dashboard offers. The device clamps independently
# (resolveMeter in animator.go) — this is the UI range, not the guard.
_METER_KEYS = {
    "meterAttack": ("attack", 0.05, 1.0),
    "meterDecay":  ("decay",  0.02, 1.0),
    "meterFloor":  ("floor",  0.0,  0.6),
    "meterGamma":  ("gamma",  1.0,  3.5),
    "meterRef":    ("ref",    0.02, 1.0),
    "meterCurve":  ("curve",  0.3,  2.0),
}


def _meter_curve(config: dict) -> dict:
    """
    Pull the meter response-curve overrides out of a device config.

    Only keys actually present are emitted: an absent field means "use the
    firmware default", which keeps the wire spec small and lets the device
    move its defaults without every stored config pinning the old ones.
    """
    out = {}
    for cfg_key, (wire_key, lo, hi) in _METER_KEYS.items():
        if cfg_key not in config or config[cfg_key] is None:
            continue
        try:
            out[wire_key] = min(hi, max(lo, float(config[cfg_key])))
        except (TypeError, ValueError):
            continue
    return out


def resolve(config: dict) -> dict:
    """
    Turn a device config dict into a render-ready scene. Unknown scene
    names fall back to standard, so a stale config never breaks LEDs.
    """
    name = (config or {}).get("ledScene", "standard")
    if name == "custom":
        listen = _hex_to_rgb(config.get("ledListenColor"), (0, 180, 0))
        think  = _hex_to_rgb(config.get("ledThinkColor"), (0, 200, 0))
        preset = {
            "listening":  _solid(*listen),
            "spin_head":  think,
            "spin_trail": tuple(c // 3 for c in think),
            "rotate":     False,
        }
    else:
        preset = _PRESETS.get(name, _PRESETS["standard"])

    listening_leds = _leds(preset["listening"])

    if preset["rotate"]:
        palette = preset["listening"]

        def spin_frame(pos: int) -> list:
            return _leds([palette[(i - pos) % NUM_LEDS] for i in range(NUM_LEDS)])
    else:
        head, trail = preset["spin_head"], preset["spin_trail"]

        def spin_frame(pos: int) -> list:
            frame = [(0, 0, 0)] * NUM_LEDS
            frame[pos % NUM_LEDS] = head
            frame[(pos - 1) % NUM_LEDS] = trail
            return _leds(frame)

    # Wire-ready led_anim specs for firmware that animates locally
    # (capability "led_anim"). The device renders these on its own ticker,
    # so spinner smoothness stops depending on controller/WiFi jitter.
    # ttlSec is a dead-man switch: if the controller dies mid-turn the
    # ring self-clears instead of spinning forever.
    # TTLs are bounded by what each phase can legitimately take, not by a
    # single generous constant. The old flat 180s meant a controller that
    # died mid-turn left the ring animating for three minutes, which reads
    # as broken long before it self-clears. Ceilings used: the mic stream's
    # 20s hard cap for listening, and observed worst-case HA think time of
    # 14.7s (2026-07-25, n=57) for the spinner — each with ~2-3x headroom.
    # meter is the exception: response length is unbounded, so its TTL is
    # set per playback from the actual audio duration (see METER_TTL_PAD).
    if preset["rotate"]:
        spin_anim = {
            "pattern":  "rotate",
            "colors":   [list(c) for c in preset["listening"]],
            "periodMs": 80,
            "ttlSec":   SPIN_TTL,
        }
    else:
        spin_anim = {
            "pattern":  "spin",
            "colors":   [list(preset["spin_head"]), list(preset["spin_trail"])],
            "periodMs": 80,
            "ttlSec":   SPIN_TTL,
        }
    listening_anim = {
        "pattern":   "solid",
        "colors":    [list(c) for c in preset["listening"]],
        "listening": True,
        "ttlSec":    30,
    }
    # Playback: the ring throbs with the live speaker level (device-side
    # RMS at the ALSA write). Solid scenes throb the spinner head colour;
    # pride throbs the whole rainbow.
    meter_palette = (preset["listening"] if preset["rotate"]
                     else [preset["spin_head"]])
    meter_anim = {
        "pattern": "meter",
        "colors":  [list(c) for c in meter_palette],
        # Placeholder — the caller overrides this per playback with
        # meter_ttl(audio_seconds). A long TTS must never self-clear its own
        # ring mid-response, which a fixed TTL would eventually do.
        "ttlSec":  METER_TTL_PAD,
        **_meter_curve(config or {}),
    }

    # A brief self-clearing cue played at turn end so outcomes are
    # distinguishable. Rhythm carries the meaning, not colour: adding a new
    # colour would collide with red (mute) / orange (link) / cyan (volume).
    outcome_colors = ([list(preset["spin_head"])] if not preset["rotate"]
                      else [list(c) for c in preset["listening"]])

    return {
        "name":           name,
        "listening":      listening_leds,
        "spin_frame":     spin_frame,
        "listening_anim": listening_anim,
        "spin_anim":      spin_anim,
        "meter_anim":     meter_anim,
        # One slow throb — "I was listening and heard nothing."
        "nospeech_anim":  {
            "pattern": "pulse", "colors": outcome_colors,
            "periodMs": 900, "ttlSec": 1,
        },
        # Fast agitated blinks — "something went wrong."
        "error_anim":     {
            "pattern": "pulse", "colors": outcome_colors,
            "periodMs": 220, "ttlSec": 1,
        },
        # A steady hold — "heard you, nothing more to do."
        #
        # Solid rather than a pulse on purpose: the two cues above are
        # OUTCOMES and their rhythm says something went wrong or unheard.
        # This one is an acknowledgement, and holding the listening colour
        # steady reads as a continuation of the listening ring rather than a
        # new signal to decode.
        #
        # It exists because a turn can now end in milliseconds. Home
        # Assistant's satellite setup intercepts the wake word and ends the
        # run immediately, so the ring lit and cleared inside ~100ms and read
        # as a glitch — the device looked broken at exactly the moment it was
        # working. Before the turn was released promptly it stayed lit only
        # because the turn was hung, which is feedback by accident.
        #
        # 1s is the TTL floor the device supports and comfortably shorter
        # than the gap between setup's two wake word prompts (measured at
        # 3.0s and 5.6s on hardware), so it cannot bleed into the next one.
        "ack_anim":       {
            "pattern": "solid", "colors": outcome_colors,
            "ttlSec": 1,
        },
        # Two orange throbs — "heard you, but there is nothing to talk to."
        #
        # The one cue that does NOT take its colour from the scene, for the
        # reason the mute ring is always red: it reports a fault in the chain
        # above the device rather than an outcome of the turn, and orange
        # already carries exactly that meaning on this hardware — it is what
        # the device pulses on its own while it cannot find a controller
        # (pulseOrange, device/cmd/server.go). Home Assistant not being
        # connected is the same condition one hop further up, so it reads as
        # "the link upstream is down" without a legend, which no rhythm in the
        # scene colour can say.
        #
        # It follows a brief hold of the listening ring (NO_HA_HOLD_S, in
        # em_controller): the wake word WAS heard and the ack has to land
        # before the fault, or the two read as one confusing signal.
        #
        # 500ms against the device's 1s TTL floor gives exactly two throbs
        # and then a self-clear — runPulse starts and ends dim, so the count
        # is the deliberate part.
        "no_ha_anim":     {
            "pattern": "pulse", "colors": [list(LINK_ORANGE)],
            "periodMs": 500, "ttlSec": 1,
        },
    }


def meter_ttl(audio_seconds: float) -> int:
    """
    Dead-man TTL for a playback meter ring, sized to the response actually
    being played. Bounded below so a very short clip still gets a sane
    window, and padded so a slow link (delivery has been observed at ~2x
    the audio duration) cannot trip it mid-response.
    """
    return int(max(30.0, audio_seconds * 2.0 + METER_TTL_PAD))
