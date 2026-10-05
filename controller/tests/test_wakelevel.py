"""em_wakelevel: the wake level both halves compute. VECTOR is repeated in
device/internal/client/wakelevel_test.go; a change to one is a change to both."""

import re
from pathlib import Path

import em_wakelevel as w

# 25 frames: quiet room, a word rising to 0.05 RMS and falling away.
VECTOR = [0.0004] * 15 + [0.002, 0.01, 0.03, 0.05, 0.04, 0.02, 0.008, 0.003, 0.001, 0.0005]
GAIN_DB = 24.0
EXPECTED = (-60.5, -50.0)


def test_shared_vector():
    assert w.measure(VECTOR, GAIN_DB) == EXPECTED


def test_go_uses_the_same_vector():
    go = (Path(__file__).parents[2] / "device/internal/client/wakelevel_test.go").read_text()
    assert "wakeLevelFrames = 25" in (Path(__file__).parents[2]
                                      / "device/internal/client/wakelevel.go").read_text()
    assert w.WINDOW_FRAMES == 25
    assert re.search(r"want := \[2\]float64\{-60\.5, -50\.0\}", go)
    assert "gainDb = 24.0" in go


def test_gain_is_removed():
    lvl, peak = w.measure([0.1], 20.0)
    assert (lvl, peak) == (-40.0, -40.0)


def test_silence_is_the_floor_not_minus_infinity():
    assert w.measure([0.0, 0.0], 24.0) == (w.FLOOR_DB, w.FLOOR_DB)


def test_empty_is_none():
    assert w.measure([], 24.0) is None
    assert w.LevelRing().measure(24.0) is None


def test_ring_keeps_only_the_window():
    r = w.LevelRing()
    for _ in range(100):
        r.push(0.9)          # loud, long before the wake
    for v in VECTOR:
        r.push(v)
    assert r.measure(GAIN_DB) == EXPECTED
    r.clear()
    assert r.measure(GAIN_DB) is None


def test_log_line_carries_capture_time_to_the_millisecond():
    line = w.log_line(-60.5, -50.0, 0.0004, 24.0, 1_790_000_000.123, "device")
    assert "Wake level -60.5 dBFS (peak -50.0, floor -92.0)" in line
    assert re.search(r"heard \d\d:\d\d:\d\d\.123, measured by device$", line)


def test_log_line_without_a_floor():
    assert "floor" not in w.log_line(-60.5, -50.0, 0.0, 24.0, 0.5, "controller")
