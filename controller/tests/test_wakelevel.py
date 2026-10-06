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


# ── Tilt: the word's tone, a ratio inside one Echo ──────────────────────────

# One 80ms frame: this pattern 160 times. Repeated in wakelevel_test.go.
TILT_PATTERN = [0, 1000, 3000, 2000, -1000, -4000, -2000, 500]
TILT_FRAME = TILT_PATTERN * 160
TILT_EXPECTED = -0.1


def test_tilt_shared_vector():
    r = w.LevelRing()
    for _ in range(w.WINDOW_FRAMES):
        r.push(0.05, w.tilt_sums(TILT_FRAME))
    assert r.tilt() == TILT_EXPECTED


def test_go_uses_the_same_tilt_vector():
    go = (Path(__file__).parents[2] / "device/internal/client/wakelevel_test.go").read_text()
    assert "{0, 1000, 3000, 2000, -1000, -4000, -2000, 500}" in go
    assert f"tiltWant = {TILT_EXPECTED}" in go


def test_tilt_does_not_move_with_level():
    """A ratio: the same word ten times louder has the same tone."""
    quiet = w.tilt([w.tilt_sums(TILT_FRAME)])
    loud = w.tilt([w.tilt_sums([v * 5 for v in TILT_FRAME])])
    assert quiet == loud == TILT_EXPECTED


def test_tilt_follows_the_high_frequencies():
    """Alternating samples are all difference; a slow ramp is almost none."""
    bright = w.tilt([w.tilt_sums([1000, -1000] * 640)])
    dull = w.tilt([w.tilt_sums(list(range(-640, 640)))])
    assert bright == 6.0
    assert dull < -40


def test_tilt_needs_signal():
    assert w.tilt([]) is None
    assert w.tilt([w.tilt_sums([0] * 1280)]) is None
    assert w.tilt_sums([5]) == (0.0, 0.0)
    r = w.LevelRing()
    r.push(0.01)                      # a frame kept without its samples
    assert r.tilt() is None
    r.push(0.01, w.tilt_sums(TILT_FRAME))
    r.clear()
    assert r.tilt() is None


def test_log_line_carries_the_tilt_when_there_is_one():
    line = w.log_line(-60.5, -50.0, 0.0004, 24.0, 1_790_000_000.123, "device", -7.3)
    assert "(peak -50.0, floor -92.0, tilt -7.3), heard" in line
    assert "tilt" not in w.log_line(-60.5, -50.0, 0.0004, 24.0, 0.5, "device")


def test_the_controllers_fast_path_agrees_with_the_definition():
    """em_controller sums with numpy; it has to be the same two numbers."""
    import numpy as np
    src = (Path(__file__).parents[1] / "em_controller.py").read_text()
    body = src[src.index("def _tilt_sums("):src.index("async def _claim_wake(")]
    ns = {"np": np}
    exec(body, ns)
    fast = ns["_tilt_sums"](np.array(TILT_FRAME, dtype=np.int16))
    slow = w.tilt_sums(TILT_FRAME)
    assert abs(fast[0] - slow[0]) < 1e-9 and abs(fast[1] - slow[1]) < 1e-9
    assert ns["_tilt_sums"](np.array([5], dtype=np.int16)) == (0.0, 0.0)
