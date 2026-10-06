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

`tilt` is the word's tone over the same window: the energy of the
sample-to-sample difference over the energy of the samples, in dB. A voice
loses its high frequencies with distance and round a corner, and the
difference weights them up, so a nearer voice reads higher. It is a ratio
inside one Echo, so no gain setting or microphone sensitivity is in it. Each
80ms frame contributes the sums over its samples 1..n-1, starting afresh, so
both halves compute it from the same bytes whatever came before. Logged only
(2026-10-06): nothing decides on it until there is data to judge it by.

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


def tilt_sums(samples) -> tuple[float, float]:
    """One frame's contribution to the tilt, from its int16 samples: the sum
    of squares of samples 1..n-1, and of their differences from the sample
    before."""
    n = len(samples)
    if n < 2:
        return 0.0, 0.0
    sq = dsq = 0.0
    prev = samples[0] / 32768.0
    for i in range(1, n):
        f = samples[i] / 32768.0
        sq += f * f
        dsq += (f - prev) * (f - prev)
        prev = f
    return sq, dsq


def tilt(sums) -> float | None:
    """The tilt in dB of a window of tilt_sums pairs, or None when there is
    nothing to take a ratio of (silence, or frames kept without samples)."""
    sq = sum(s[0] for s in sums)
    dsq = sum(s[1] for s in sums)
    if sq <= 0 or dsq <= 0:
        return None
    return round(10.0 * math.log10(dsq / sq), 1)


class LevelRing:
    """The last WINDOW_FRAMES frames of one Echo's wake stream: each frame's
    RMS, and its tilt sums when the caller had the samples."""

    def __init__(self) -> None:
        self._rms: deque[float] = deque(maxlen=WINDOW_FRAMES)
        self._sums: deque[tuple[float, float]] = deque(maxlen=WINDOW_FRAMES)

    def push(self, rms: float, sums: tuple[float, float] = (0.0, 0.0)) -> None:
        self._rms.append(rms)
        self._sums.append(sums)

    def clear(self) -> None:
        self._rms.clear()
        self._sums.clear()

    def measure(self, gain_db: float) -> tuple[float, float] | None:
        return measure(self._rms, gain_db)

    def tilt(self) -> float | None:
        return tilt(self._sums)


def log_line(level: float, peak: float, floor_rms: float | None, gain_db: float,
             heard_wall: float, source: str, tilt: float | None = None) -> str:
    """The device_logs line. Capture time to the millisecond is what pairs one
    utterance's wakes across Echos."""
    floor = f", floor {dbfs(floor_rms) - gain_db:.1f}" if floor_rms else ""
    if tilt is not None:
        floor += f", tilt {tilt:.1f}"
    total_ms = round(heard_wall * 1000)
    stamp = time.strftime("%H:%M:%S", time.localtime(total_ms // 1000))
    ms = total_ms % 1000
    return (f"Wake level {level:.1f} dBFS (peak {peak:.1f}{floor}), "
            f"heard {stamp}.{ms:03d}, measured by {source}")
