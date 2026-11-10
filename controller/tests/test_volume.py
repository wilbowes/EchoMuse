"""Volume scale conversion.

The scale existed as `/ 175` copy-pasted into three modules with no test at
all, which is how it went four releases with a ceiling that put the codec's
DAC into positive digital gain. These pin the ceiling and the round trip.
"""
import json
import os
import re
from pathlib import Path

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
    # Every level HA can set reports back as a value that sets it again.
    for step in range(em_volume.VOLUME_STEPS + 1):
        level = em_volume.VOLUME_FLOOR + step * em_volume.VOLUME_STEP
        ha = em_volume.device_level_to_ha(level)
        assert em_volume.ha_volume_to_device(ha) == level
    assert em_volume.ha_volume_to_device(em_volume.device_level_to_ha(0)) == 0


def test_one_percent_is_the_floor_and_zero_is_silent():
    assert em_volume.ha_volume_to_device(0.0) == 0
    assert em_volume.ha_volume_to_device(0.01) == em_volume.VOLUME_FLOOR
    assert em_volume.ha_volume_to_device(1.0) == em_volume.DEVICE_VOLUME_MAX
    # Audible but under the floor (a stored level from before) must not read
    # as 0, which HA takes for muted.
    assert em_volume.device_level_to_ha(1) == 0.01
    assert em_volume.device_level_to_ha(em_volume.VOLUME_FLOOR) == 0.01


def test_every_percentage_lands_on_a_button_step():
    # So HA's X% shows the same ring as the presses that reach it.
    prev = 0
    for pct in range(1, 101):
        level = em_volume.ha_volume_to_device(pct / 100)
        assert (level - em_volume.VOLUME_FLOOR) % em_volume.VOLUME_STEP == 0, pct
        assert level >= prev
        prev = level


def _go_const(name):
    src = (Path(__file__).resolve().parents[2]
           / "device/internal/server/volume.go").read_text()
    m = re.search(rf"^\s*{name}\s*=\s*(\d+)", src, re.M)
    assert m, f"{name} not found in volume.go"
    return int(m.group(1))


def test_the_band_matches_the_firmwares():
    assert em_volume.VOLUME_FLOOR == _go_const("volumeButtonFloor")
    assert em_volume.VOLUME_STEP == _go_const("volumeStep")
    assert em_volume.DEVICE_VOLUME_MAX == _go_const("volumeMax")


VECTORS = (Path(__file__).resolve().parents[2]
           / "device/internal/server/testdata/volume_law.json")


def _vectors():
    return {"percent_to_level": [em_volume.ha_volume_to_device(p / 100) for p in range(101)],
            "level_to_percent": [round(em_volume.device_level_to_ha(l) * 100, 6)
                                 for l in range(128)]}


def test_the_law_matches_the_firmwares():
    # The device's PercentToLevel/LevelToPercent (Sendspin) read this file in
    # volume_test.go. Regenerate: EM_WRITE_VECTORS=1 pytest tests/test_volume.py
    if os.environ.get("EM_WRITE_VECTORS") == "1":
        VECTORS.parent.mkdir(exist_ok=True)
        VECTORS.write_text(json.dumps(_vectors(), indent=1) + "\n")
    assert json.loads(VECTORS.read_text()) == _vectors()


def test_device_level_above_the_cap_reports_as_full():
    # A device still sitting at a pre-cap level must not report >1.0 to HA.
    assert em_volume.device_level_to_ha(175) == 1.0
    assert em_volume.device_level_to_ha(140) == 1.0


def test_bad_input_does_not_raise():
    assert em_volume.device_level_to_ha(None) == 0.0
    assert em_volume.device_level_to_ha("nonsense") == 0.0
