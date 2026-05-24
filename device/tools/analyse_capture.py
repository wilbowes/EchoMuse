#!/usr/bin/env python3
"""
analyse_capture.py — decode and analyse a raw 9-channel S24_3LE mic capture
from the biscuit mic array.

Usage:
    python3 analyse_capture.py capture.raw [--plot]

The script decodes all 9 channels, computes RMS per channel, and prints a
ranked table. Run once per capture position to build up the channel→mic mapping.

Test procedure:
    1. Place a tone source (phone speaker, ~440Hz) directly in front of the
       action button on the Echo Dot. Call this position 0°.
    2. Run capture_mics on device, pull capture.raw, run this script.
       Note the top 2-3 channels by RMS.
    3. Rotate the tone source 60° clockwise. Repeat capture + analysis.
    4. After 6 positions (full 360°) the channel→physical-mic mapping is clear.

Output format of capture.raw:
    Raw interleaved S24_3LE, 9 channels, 16kHz
    Each frame: 9 × 3 bytes = 27 bytes
    Frame rate: 16000/s
"""

import sys
import struct
import argparse
import numpy as np


N_CHANNELS = 9
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 3  # S24_3LE


def decode_s24_3le(raw: bytes, n_channels: int = N_CHANNELS) -> np.ndarray:
    """
    Decode raw interleaved S24_3LE bytes into float32 array [n_channels, n_frames].
    S24_3LE: 3 bytes per sample, little-endian, signed 24-bit.
    """
    total_bytes = len(raw)
    bytes_per_frame = n_channels * BYTES_PER_SAMPLE
    n_frames = total_bytes // bytes_per_frame

    if total_bytes % bytes_per_frame != 0:
        print(f"Warning: {total_bytes % bytes_per_frame} trailing bytes ignored")

    out = np.zeros((n_channels, n_frames), dtype=np.float32)

    for i in range(n_frames):
        for ch in range(n_channels):
            offset = i * bytes_per_frame + ch * BYTES_PER_SAMPLE
            b0, b1, b2 = raw[offset], raw[offset + 1], raw[offset + 2]
            # Sign-extend 24-bit to 32-bit
            val = b0 | (b1 << 8) | (b2 << 16)
            if val & 0x800000:
                val -= 0x1000000
            out[ch, i] = val / 8388608.0  # normalise to [-1.0, 1.0]

    return out


def rms(signal: np.ndarray) -> float:
    return float(np.sqrt(np.mean(signal ** 2)))


def rms_db(signal: np.ndarray) -> float:
    r = rms(signal)
    if r < 1e-10:
        return -100.0
    return 20.0 * np.log10(r)


def bar(value: float, max_value: float, width: int = 30) -> str:
    filled = int(round(value / max_value * width)) if max_value > 0 else 0
    return "█" * filled + "░" * (width - filled)


def analyse(raw: bytes, label: str = ""):
    channels = decode_s24_3le(raw)
    n_frames = channels.shape[1]
    duration_ms = n_frames * 1000 // SAMPLE_RATE

    print(f"\n{'='*60}")
    if label:
        print(f"  Capture: {label}")
    print(f"  Frames:  {n_frames}  ({duration_ms}ms)")
    print(f"  Bytes:   {len(raw)}")
    print(f"{'='*60}\n")

    rms_values = [(ch, rms(channels[ch]), rms_db(channels[ch]))
                  for ch in range(N_CHANNELS)]

    # Sort by RMS descending for the ranking
    ranked = sorted(rms_values, key=lambda x: -x[1])
    max_rms = ranked[0][1] if ranked[0][1] > 0 else 1.0

    print(f"  {'Ch':>3}  {'RMS':>8}  {'dBFS':>7}  {'Relative':>8}  Level")
    print(f"  {'-'*3}  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*30}")

    for ch, r, db in ranked:
        rel = r / max_rms
        label_str = " ← loudest" if ch == ranked[0][0] else ""
        print(f"  {ch:>3}  {r:>8.5f}  {db:>7.1f}  {rel:>8.3f}  "
              f"{bar(r, max_rms)}{label_str}")

    print(f"\n  Loudest channel: {ranked[0][0]}  "
          f"(RMS {ranked[0][1]:.5f}, {ranked[0][2]:.1f} dBFS)")

    # Also show original channel order for easy cross-referencing
    print(f"\n  Channel order (0–8):")
    orig = sorted(rms_values, key=lambda x: x[0])
    vals = [f"ch{ch}={db:.1f}dB" for ch, _, db in orig]
    print(f"  {', '.join(vals)}")

    return ranked


def plot_channels(raw: bytes, title: str = ""):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available — skipping plot")
        return

    channels = decode_s24_3le(raw)
    n_frames = channels.shape[1]
    t = np.arange(n_frames) / SAMPLE_RATE

    fig, axes = plt.subplots(N_CHANNELS, 1, figsize=(14, 12), sharex=True)
    if title:
        fig.suptitle(title, fontsize=12)

    rms_vals = [rms(channels[ch]) for ch in range(N_CHANNELS)]
    max_rms = max(rms_vals) if max(rms_vals) > 0 else 1.0

    for ch in range(N_CHANNELS):
        ax = axes[ch]
        ax.plot(t, channels[ch], linewidth=0.3, color='steelblue')
        ax.set_ylabel(f"Ch {ch}\n{rms_db(channels[ch]):.1f}dB",
                      fontsize=8, rotation=0, labelpad=40)
        ax.set_ylim(-1.0, 1.0)
        ax.axhline(0, color='grey', linewidth=0.3)
        # Highlight loudest channel
        if rms_vals[ch] == max_rms:
            ax.set_facecolor('#fff8e1')

    axes[-1].set_xlabel("Time (s)")
    plt.tight_layout()
    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="Analyse raw 9-channel S24_3LE mic capture from biscuit"
    )
    parser.add_argument("capture_file", help="Raw capture file (capture.raw)")
    parser.add_argument("--plot", action="store_true",
                        help="Plot all 9 channels (requires matplotlib)")
    parser.add_argument("--label", default="",
                        help="Label for this capture (e.g. '0deg_action_button')")
    args = parser.parse_args()

    try:
        with open(args.capture_file, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.capture_file}")
        sys.exit(1)

    if len(raw) == 0:
        print("Error: capture file is empty")
        sys.exit(1)

    label = args.label or args.capture_file
    ranked = analyse(raw, label)

    if args.plot:
        plot_channels(raw, title=label)


if __name__ == "__main__":
    main()
