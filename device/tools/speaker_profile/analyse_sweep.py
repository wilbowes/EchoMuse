#!/usr/bin/env python3
"""
analyse_sweep.py — speaker frequency response, measured against the hardware
echo reference.

The Ch7/Ch8 discovery (2026-08-29) makes this measurement much easier than the
controller-side rig that was planned. Both the stimulus and the response come
back in ONE capture, off ONE ADC clock: Ch7 carries the signal that was sent to
the DAC, Ch0-Ch6 carry what the microphones heard. So the transfer function is
a direct division with no alignment step, no timing assumptions, and nothing to
correlate — the two are sample-locked by construction.

WHAT THIS DOES AND DOES NOT MEASURE. The result is the response of the whole
loop: DAC, amplifier, driver, the room, and the on-board microphone. The mic is
not a calibrated measurement mic and its own response is baked into every
number here. That is fine for what we need it for — deciding EQ defaults for a
system whose output is judged through this same speaker — and it is NOT a
substitute for a measurement mic if an absolute driver response is ever wanted.
Read it as "what does this device sound like to itself", and prefer relative
comparisons (before/after an EQ change) over absolute dB figures.

Numpy only.

Usage:
    analyse_sweep.py sweep.raw
    analyse_sweep.py sweep_a.raw --compare sweep_b.raw   # repeatability first
"""

import argparse
import sys

import numpy as np

N_CHANNELS = 9
RATE = 16000
REF = 7          # left echo reference
MIC = 6          # centre mic — omnidirectional, the sane default
# Third-octave centres across the band a 1.6" driver can be asked about.
#
# Stops at 6300 deliberately. The next band up is centred 8000, and even 6300's
# upper edge sits at 7073 — a 7500Hz band would reach 8415, past both the end of
# the sweep and the 8kHz Nyquist of a 16kHz capture, so it integrates noise and
# reads as a large fictitious boost. It did exactly that in the self-test:
# +18.2dB in a synthetic capture with nothing planted above +6.
BANDS = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
         1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300]


def decode(raw: bytes) -> np.ndarray:
    frame = N_CHANNELS * 3
    n = len(raw) // frame
    b = np.frombuffer(raw[:n * frame], dtype=np.uint8).reshape(n, N_CHANNELS, 3)
    v = (b[:, :, 0].astype(np.int32)
         | (b[:, :, 1].astype(np.int32) << 8)
         | (b[:, :, 2].astype(np.int8).astype(np.int32) << 16))
    return (v.astype(np.float32) / (1 << 23)).T


def response(ch: np.ndarray):
    """
    Third-octave band response of MIC relative to REF.

    Band energy ratios rather than a raw FFT division: a sweep visits each
    frequency only briefly, so bin-by-bin division is dominated by whichever
    bins the sweep happened not to excite. Integrating over a band is what
    makes the answer stable enough to act on.
    """
    ref, mic = ch[REF], ch[MIC]
    n = min(len(ref), len(mic))
    ref, mic = ref[:n], mic[:n]
    if not np.any(ref):
        return None
    R = np.abs(np.fft.rfft(ref * np.hanning(n))) ** 2
    M = np.abs(np.fft.rfft(mic * np.hanning(n))) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    out = []
    for c in BANDS:
        lo, hi = c / 2 ** (1 / 6), c * 2 ** (1 / 6)
        sel = (freqs >= lo) & (freqs < hi)
        if not sel.any():
            continue
        r, m = R[sel].sum(), M[sel].sum()
        if r <= 0 or m <= 0:
            out.append((c, None))
            continue
        out.append((c, 10 * np.log10(m / r)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--compare", help="second capture; prints the difference")
    args = ap.parse_args()

    a = response(decode(open(args.capture, "rb").read()))
    if a is None:
        print("reference channel is silent — was anything playing?", file=sys.stderr)
        return 1
    b = None
    if args.compare:
        b = response(decode(open(args.compare, "rb").read()))

    # Normalise to the 1kHz band so the numbers read as a shape, not a level;
    # absolute gain here is meaningless (uncalibrated mic, unknown distance).
    def norm(resp):
        ref = dict(resp).get(1000)
        if ref is None:
            return resp
        return [(f, None if v is None else v - ref) for f, v in resp]

    a, b = norm(a), (norm(b) if b else None)

    print(f"\n  Third-octave response, relative to 1kHz")
    print(f"  mic ch{MIC} against reference ch{REF}\n")
    hdr = "    Hz     dB" + ("    dB(2)   delta" if b else "")
    print(hdr)
    print("  " + "-" * (len(hdr) + 20))
    deltas = []
    bmap = dict(b) if b else {}
    for f, v in a:
        if v is None:
            print(f"  {f:6d}    (no energy)")
            continue
        bar_n = int(max(0, min(30, (v + 30) * 30 / 40)))
        line = f"  {f:6d}  {v:+6.1f}"
        if b:
            v2 = bmap.get(f)
            if v2 is None:
                line += "       -       -"
            else:
                d = v2 - v
                deltas.append(d)
                line += f"  {v2:+6.1f}  {d:+6.2f}"
        line += "  " + "#" * bar_n
        print(line)

    if b and deltas:
        d = np.array(deltas)
        print(f"\n  Repeatability: mean |delta| {np.abs(d).mean():.2f} dB, "
              f"max {np.abs(d).max():.2f} dB")
        if np.abs(d).max() > 3.0:
            print("  >>> The two sweeps disagree by more than 3dB somewhere.")
            print("  >>> Nothing downstream of this measurement is worth")
            print("  >>> building until that is understood — check the device")
            print("  >>> has not moved, and that nothing else made noise.")
        else:
            print("  Stable enough to build EQ defaults on.")

    print("""
  Reading it: this is the whole loop — DAC, amp, driver, room, and the Echo's
  own microphone, whose response is in every number. Use it for SHAPE and for
  before/after comparisons, never as an absolute driver response.

  Known before measuring, from stock's own FIR (#247): below ~115Hz there is
  nothing to EQ, because stock removes it 20:1 and the driver cannot radiate
  it. Stock boosts 150-250Hz by ~25dB, where we boost nothing. If this
  measurement disagrees with either, suspect the measurement first.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
