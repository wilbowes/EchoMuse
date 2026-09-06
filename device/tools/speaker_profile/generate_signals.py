#!/usr/bin/env python3
"""
generate_signals.py — write the test signals profile.sh plays.

Generated rather than committed: they are 4MB of WAV that a dozen lines
reproduce exactly, and a binary in git is a binary nobody can review or
adjust. profile.sh runs this automatically when the files are missing.

All are 48kHz stereo S16, matching the speaker path (card 0, device 23).

    left_only / right_only  440Hz on one channel, digital silence on the
                            other. The internal driver plays the RIGHT
                            channel only and discards the left (measured
                            2026-08-29, 55dB apart), and these are what
                            re-establish that on another board.

    sweep                   40Hz->7.5kHz log sine over 10s, with raised-cosine
                            fades and a second of silence after it.

Three details in the sweep are load-bearing:

  * LOG, not linear. A log sweep spends equal time per octave, so the low
    end - where this driver is weakest and the measurement noisiest - gets
    the dwell time it needs instead of a few milliseconds.

  * 7.5kHz top. The capture is 16kHz, so Nyquist is 8kHz; sweeping closer
    would alias into the band being measured.

  * Amplitude 12000 of 32767. Comfortably below the level where the DAC adds
    distortion of its own at unity gain (1.5% THD at index 127, 65% at 153 -
    see Volume in device/CLAUDE.md), because a measurement rig must not be
    the loudest source of the thing it is measuring.
"""

import math
import struct
import sys
from pathlib import Path

RATE = 48000


def write_wav(path: Path, frames) -> None:
    with open(path, "wb") as f:
        n = len(frames) * 2
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + n))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16))
        f.write(b"data")
        f.write(struct.pack("<I", n))
        for v in frames:
            f.write(struct.pack("<h", max(-32768, min(32767, int(v)))))


def tone(seconds: float = 6.0, hz: float = 440.0, amp: int = 20000,
         left: bool = True, right: bool = True):
    out = []
    for i in range(int(RATE * seconds)):
        v = amp * math.sin(2 * math.pi * hz * i / RATE)
        out.append(v if left else 0)
        out.append(v if right else 0)
    return out


def log_sweep(f0: float = 40.0, f1: float = 7500.0, seconds: float = 10.0,
              amp: int = 12000, fade: float = 0.05, tail: float = 1.0):
    n = int(RATE * seconds)
    k = seconds * f0 / math.log(f1 / f0)
    rate_of_change = math.log(f1 / f0) / seconds
    out = []
    for i in range(n):
        t = i / RATE
        # A step at either end smears the whole deconvolution, so fade in
        # and out over 50ms.
        env = 1.0
        if t < fade:
            env = 0.5 * (1 - math.cos(math.pi * t / fade))
        elif t > seconds - fade:
            env = 0.5 * (1 - math.cos(math.pi * (seconds - t) / fade))
        v = amp * env * math.sin(
            2 * math.pi * k * (math.exp(rate_of_change * t) - 1))
        out.extend([v, v])
    # Silence so the response tail is captured rather than wrapping round.
    out.extend([0, 0] * int(RATE * tail))
    return out


def main() -> int:
    here = Path(__file__).resolve().parent
    write_wav(here / "left_only.wav", tone(right=False))
    write_wav(here / "right_only.wav", tone(left=False))
    write_wav(here / "sweep.wav", log_sweep())
    print("wrote left_only.wav, right_only.wav, sweep.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
