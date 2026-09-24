"""
em_wakelevel.py — how loud a wake word was at the Echo that heard it
====================================================================

Logged with every arbitrated wake so a week of contested wakes can show
whether level would pick the nearer Echo better than capture time does
(decision 2026-09-24; nothing acts on it yet). The device measures its own
wakes and sends the result on `oww_wake`; the controller measures the wakes it
scores from the stream. Both must compute the SAME thing, so the definition is
here and in device/internal/client/wakelevel.go, tested against one vector:

- per 80ms frame RMS (full scale 1.0) of the wake stream: post-AEC, AGC off,
  the bytes the scorer sees;
- divided by the mic's digital gain (micGainDb), so Echos on different gains
  compare; the ADC's analogue gain is NOT removed;
- over the WINDOW_FRAMES frames ending with the one the score crossed on,
  which covers the classifier's whole view (ScoreSpan, 1.96s);
- `level` is the energy mean of that window, `peak` its loudest frame, both
  dBFS. The window includes whatever silence the word did not fill, so
  `peak` is the less word-length-dependent of the two.

Pure: tested without aiohttp or openwakeword (tests/test_wakelevel.py).
"""

from __future__ import annotations

import math
import time
from collections import deque

FRAME_MS = 80
# 25 × 80ms = 2.0s, the smallest whole-frame window covering ScoreSpan.
WINDOW_FRAMES = 25
# Reported for a window of digital silence, rather than -inf.
FLOOR_DB = -120.0


def dbfs(rms: float) -> float:
    return FLOOR_DB if rms <= 0 else max(FLOOR_DB, 20.0 * math.log10(rms))


def measure(rms_frames, gain_db: float) -> tuple[float, float] | None:
    """(level, peak) in dBFS of frame RMS values with `gain_db` removed."""
    frames = list(rms_frames)
    if not frames:
        return None
    energy = sum(r * r for r in frames) / len(frames)

    def db(rms: float) -> float:
        return round(max(FLOOR_DB, dbfs(rms) - gain_db), 1)
    return db(math.sqrt(energy)), db(max(frames))


class LevelRing:
    """The last WINDOW_FRAMES frame RMS values of one Echo's wake stream."""

    def __init__(self) -> None:
        self._rms: deque[float] = deque(maxlen=WINDOW_FRAMES)

    def push(self, rms: float) -> None:
        self._rms.append(rms)

    def clear(self) -> None:
        self._rms.clear()

    def measure(self, gain_db: float) -> tuple[float, float] | None:
        return measure(self._rms, gain_db)


def log_line(level: float, peak: float, floor_rms: float | None, gain_db: float,
             heard_wall: float, source: str) -> str:
    """The device_logs line. Capture time to the millisecond is what pairs one
    utterance's wakes across Echos."""
    floor = f", floor {dbfs(floor_rms) - gain_db:.1f}" if floor_rms else ""
    total_ms = round(heard_wall * 1000)
    stamp = time.strftime("%H:%M:%S", time.localtime(total_ms // 1000))
    ms = total_ms % 1000
    return (f"Wake level {level:.1f} dBFS (peak {peak:.1f}{floor}), "
            f"heard {stamp}.{ms:03d}, measured by {source}")
