#!/usr/bin/env python3
"""
analyse_ref.py — characterise the echo reference channels in a 9-channel capture.

The 2026-08-29 probe established that Ch7 and Ch8 carry the device's own
playback at full digital scale (RMS matched the generated signal to five
significant figures) and read bit-exact zero when nothing is playing. This
answers what is left: are they a stereo pair, how far does the mic lag them,
and does the DAC volume control sit before or after the tap.

Numpy only — no scipy, so it runs on a laptop with nothing installed but numpy.

Usage:
    analyse_ref.py capture.raw --mode tone   # per-channel dominant frequency
    analyse_ref.py capture.raw --mode delay  # mic-vs-reference lag
    analyse_ref.py capture.raw               # both
"""

import argparse
import sys

import numpy as np

N_CHANNELS = 9
BYTES_PER_SAMPLE = 3
RATE = 16000
REF_CHANNELS = (7, 8)
MIC_CENTRE = 6


def decode_s24_3le(raw: bytes) -> np.ndarray:
    """Raw interleaved S24_3LE -> float array [channels, frames], scaled ±1."""
    frame = N_CHANNELS * BYTES_PER_SAMPLE
    n = len(raw) // frame
    b = np.frombuffer(raw[:n * frame], dtype=np.uint8).reshape(n, N_CHANNELS, 3)
    # little-endian 24-bit, sign-extended via the top byte
    v = (b[:, :, 0].astype(np.int32)
         | (b[:, :, 1].astype(np.int32) << 8)
         | (b[:, :, 2].astype(np.int8).astype(np.int32) << 16))
    return (v.astype(np.float32) / (1 << 23)).T


def dominant(sig: np.ndarray, top: int = 3):
    """The strongest spectral components, as (hz, relative amplitude)."""
    if not np.any(sig):
        return []
    w = sig * np.hanning(len(sig))
    mag = np.abs(np.fft.rfft(w))
    freqs = np.fft.rfftfreq(len(w), 1.0 / RATE)
    peak = mag.max()
    if peak <= 0:
        return []
    idx = np.argsort(mag)[::-1][:top]
    return [(freqs[i], mag[i] / peak) for i in idx]


def lag_samples(a: np.ndarray, b: np.ndarray, max_ms: float = 250.0):
    """
    Lag of b behind a, by FFT cross-correlation. Positive = b arrives later.

    Both are mean-removed and energy-normalised so the returned peak is a
    correlation coefficient — a number that says how much to believe the lag.
    """
    n = min(len(a), len(b))
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return None, 0.0
    size = 1 << int(np.ceil(np.log2(2 * n)))
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    span = int(max_ms * RATE / 1000)
    # Positive lags at the head, negative at the tail — keep both.
    head, tail = corr[:span], corr[-span:]
    both = np.concatenate([tail, head])
    lags = np.concatenate([np.arange(-span, 0), np.arange(0, span)])
    i = int(np.argmax(np.abs(both)))
    return int(lags[i]), float(both[i] / (na * nb))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--mode", choices=("tone", "delay", "both"), default="both")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    ch = decode_s24_3le(open(args.capture, "rb").read())
    frames = ch.shape[1]
    print(f"\n{'=' * 62}")
    print(f"  {args.label or args.capture}   {frames} frames "
          f"({frames * 1000 // RATE}ms)")
    print(f"{'=' * 62}")

    rms = [float(np.sqrt(np.mean(ch[i] ** 2))) for i in range(N_CHANNELS)]
    print("\n  RMS per channel")
    for i in range(N_CHANNELS):
        db = 20 * np.log10(rms[i]) if rms[i] > 0 else -np.inf
        tag = ""
        if i in REF_CHANNELS:
            tag = "  <- reference candidate"
        elif i == MIC_CENTRE:
            tag = "  <- centre mic"
        db_s = "  -inf (bit-exact zero)" if rms[i] == 0 else f"{db:8.1f} dB"
        print(f"    ch{i}  {rms[i]:.6f}  {db_s}{tag}")

    if args.mode in ("tone", "both"):
        print("\n  Dominant frequencies")
        for i in (MIC_CENTRE, *REF_CHANNELS):
            peaks = dominant(ch[i])
            if not peaks:
                print(f"    ch{i}: silent")
                continue
            s = ", ".join(f"{f:7.1f}Hz ({a:.2f})" for f, a in peaks)
            print(f"    ch{i}: {s}")
        # The question this capture exists to answer.
        if all(np.any(ch[c]) for c in REF_CHANNELS):
            d = ch[REF_CHANNELS[0]] - ch[REF_CHANNELS[1]]
            same = not np.any(d)
            verdict = ("BIT-IDENTICAL — duplicates, not a stereo pair" if same
                       else "DIFFERENT — a genuine stereo pair")
            print(f"\n    ch7 and ch8 are {verdict}")
            if not same:
                print(f"      difference RMS {float(np.sqrt(np.mean(d ** 2))):.6f}")

    if args.mode in ("delay", "both"):
        print("\n  Mic lag behind the reference")
        for c in REF_CHANNELS:
            if not np.any(ch[c]):
                print(f"    ch{c}: silent, no lag to measure")
                continue
            lag, peak = lag_samples(ch[c], ch[MIC_CENTRE])
            if lag is None:
                print(f"    ch{c}: no signal")
                continue
            print(f"    ch{MIC_CENTRE} behind ch{c}: {lag:+d} samples "
                  f"= {lag * 1000.0 / RATE:+.2f} ms   (correlation {peak:+.3f})")
        print("\n    A correlation below ~0.1 means the lag is not to be "
              "trusted —\n    read it with a broadband signal (the noise "
              "capture), never a tone:\n    a tone's lag is only known modulo "
              "its period.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
