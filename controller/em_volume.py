"""Volume scale conversion between the device's native level and HA's float.

Split out as pure logic for the reason em_linkauth.py was: the three call
sites (em_controller, em_esphome, em_api) each had their OWN copy of
`level / 175`, so the scale lived in three places and none of them were
covered by a test. Changing the ceiling meant finding all three.

The device level is the raw tinymix ctl 61 index — the tlv320aic32x4 DAC
digital volume, 0.5dB per step with 0dB (unity) at index 127. The scale is
therefore dB-linear, which is roughly perceptually linear, so the mapping to
HA's 0.0–1.0 is a plain proportion and needs no extra taper.

DEVICE_VOLUME_MAX is 127 and NOT the control's own maximum of 175. Indexes
above 127 apply positive digital gain to near-full-scale PCM and saturate
inside the DAC — measured at 65% THD by index 153 (see
device/internal/server/volume.go for the full measurement and why stock
FireOS never touches this control).
"""

# Codec unity gain. The mixer control accepts up to 175; everything above
# this clips. See the module docstring.
DEVICE_VOLUME_MAX = 127

# 0.5dB per index step, 0dB at DEVICE_VOLUME_MAX.
DB_PER_STEP = 0.5


def level_to_db(level: int) -> float:
    """Device level as dB relative to unity. Index 127 -> 0.0, index 0 -> -63.5."""
    return (level - DEVICE_VOLUME_MAX) * DB_PER_STEP


# Radar's own volume law, read out of its stock firmware. Alexa's volume is
# a 0-100 value per step (VolumeCurves.xml), and the stock mixer daemon
# (/system/bin/mixer, Mixer_AlgoRampGain) turns each value into a level in
# exactly this module's law — 0.5dB per step, 127 = 0dB — through this table:
# value + 27 from 11 up, a steeper run below. On Radar the HA slider's percent
# IS that value, so HA 54% plays at the level a stock Echo plays Alexa
# volume 5 at (-23dB), where a plain proportion put it at -29dB. em_eq reads
# the same table to pick the stock EQ file for a volume.
STOCK_MIXER_LEVELS = (
    0, 3, 7, 11, 17, 20, 27, 30, 32, 35, 36,
    *range(38, 128),            # values 11..100: value + 27
)
assert len(STOCK_MIXER_LEVELS) == 101


def _uses_stock_law(board_id) -> bool:
    return board_id == "radar"


def stock_value_for_level(level: int) -> int:
    """Stock's 0-100 volume value for a device level: the highest value whose
    mixer level is at or below it."""
    value = 0
    for v, lv in enumerate(STOCK_MIXER_LEVELS):
        if lv <= level:
            value = v
    return value


def device_level_to_ha(level: int, board_id: str | None = None) -> float:
    """Convert a device volume level to an HA float (0.0–1.0).

    On Radar the float is stock's volume value / 100 (see
    STOCK_MIXER_LEVELS); every other board keeps the plain proportion."""
    try:
        lv = float(level)
        if _uses_stock_law(board_id):
            lv = max(0, min(DEVICE_VOLUME_MAX, int(round(lv))))
            return stock_value_for_level(lv) / 100.0
        return max(0.0, min(1.0, lv / DEVICE_VOLUME_MAX))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def ha_volume_to_device(volume: float, board_id: str | None = None) -> int:
    """Convert an HA volume float (0.0–1.0) to a device volume level.

    Clamped to the codec's unity gain, so HA asking for full volume can never
    put the DAC into positive digital gain. On Radar the float is read as
    stock's volume value and goes through its mixer table.
    """
    if _uses_stock_law(board_id):
        value = max(0, min(100, round(float(volume) * 100)))
        return STOCK_MIXER_LEVELS[value]
    return max(0, min(DEVICE_VOLUME_MAX, round(float(volume) * DEVICE_VOLUME_MAX)))
