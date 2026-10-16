"""Volume scale conversion.

The scale existed as `/ 175` copy-pasted into three modules with no test at
all, which is how it went four releases with a ceiling that put the codec's
DAC into positive digital gain. These pin the ceiling and the round trip.
"""
import pytest

import em_volume


def test_ceiling_is_codec_unity_not_the_controls_maximum():
    # tinymix ctl 61 accepts 0..175. 127 is 0dB; everything above it is
    # POSITIVE digital gain on near-full-scale PCM and clips inside the DAC
    # (measured 65% THD at index 153). If this ever reads 175 again, the
    # distortion above ~73% volume is back.
    assert em_volume.DEVICE_VOLUME_MAX == 127


def test_full_ha_volume_cannot_exceed_unity():
    assert em_volume.ha_volume_to_device(1.0) == 127
    # Out-of-range input must clamp, not scale past the ceiling.
    assert em_volume.ha_volume_to_device(1.5) == 127
    assert em_volume.ha_volume_to_device(-0.5) == 0


def test_level_to_db_anchors():
    assert em_volume.level_to_db(127) == pytest.approx(0.0)
    assert em_volume.level_to_db(0) == pytest.approx(-63.5)
    # The old ceiling was +24dB of digital gain.
    assert em_volume.level_to_db(175) == pytest.approx(24.0)


def test_ha_round_trip_is_stable():
    for level in range(0, 128):
        ha = em_volume.device_level_to_ha(level)
        assert em_volume.ha_volume_to_device(ha) == level


def test_device_level_above_the_cap_reports_as_full():
    # A device still sitting at a pre-cap level must not report >1.0 to HA.
    assert em_volume.device_level_to_ha(175) == 1.0
    assert em_volume.device_level_to_ha(140) == 1.0


def test_bad_input_does_not_raise():
    assert em_volume.device_level_to_ha(None) == 0.0
    assert em_volume.device_level_to_ha("nonsense") == 0.0


# ─── Radar: stock's own volume law ───────────────────────────────────────────

def test_radar_ha_percent_is_stocks_volume_value():
    """On Radar the HA float is stock's 0-100 volume value, through the
    mixer table read out of /system/bin/mixer: Alexa 5 (value 54) is -23dB,
    level 81, as on a stock Echo."""
    assert em_volume.ha_volume_to_device(0.54, "radar") == 81
    assert em_volume.ha_volume_to_device(1.0, "radar") == 127
    assert em_volume.ha_volume_to_device(0.0, "radar") == 0
    assert em_volume.ha_volume_to_device(0.01, "radar") == 3
    assert em_volume.ha_volume_to_device(1.5, "radar") == 127
    assert em_volume.ha_volume_to_device(-0.5, "radar") == 0


def test_radar_round_trips_every_ha_percent():
    for value in range(101):
        level = em_volume.ha_volume_to_device(value / 100, "radar")
        assert em_volume.device_level_to_ha(level, "radar") == value / 100


def test_radar_level_between_values_reads_as_the_one_below():
    # Level 50 sits between values 22 (49) and 23 (50)... exactly on 23.
    assert em_volume.device_level_to_ha(50, "radar") == 0.23
    # Level 5 sits between values 1 (3) and 2 (7): reads as 1.
    assert em_volume.device_level_to_ha(5, "radar") == 0.01


def test_other_boards_keep_the_proportion():
    for board in (None, "biscuit"):
        assert em_volume.ha_volume_to_device(0.5, board) == 64
        assert em_volume.device_level_to_ha(127, board) == 1.0
