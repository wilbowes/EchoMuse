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

    return {"name": name, "listening": listening_leds, "spin_frame": spin_frame}
