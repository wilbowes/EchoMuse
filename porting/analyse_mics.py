#!/usr/bin/env python3
"""Summarise a multi-channel mic capture from probe.sh. Standard library only.

    analyse_mics.py <file.raw> <channels> <format> <rate> [clap_s ...]

format is s16_le, s24_le, s24_3le or s32_le. Each clap_s is a time the
operator was told to clap; the analysis looks at the 1.5s after it.

What it answers, per channel:
  - silent: bit-exact zero throughout. On biscuit that is an unconnected
    input, or a playback loopback while nothing plays (Ch7/Ch8 — they carry
    audio only when the speaker does, so a capture with the speaker quiet
    cannot tell those two apart).
  - quiet RMS and DC: the noise floor, and whether a channel is sitting on an
    offset rather than carrying audio.
  - duplicates: two channels correlating above 0.999 are one signal twice.
  - clap arrival order: the mic nearest the clap crosses first. One sample
    at 16kHz is ~21mm of sound, so on a 72mm array this is an ordering, not
    a position, and needs a clap at two opposite points to mean anything.
"""
import math
import struct
import sys

WIDTH = {"s16_le": 2, "s24_le": 4, "s24_3le": 3, "s32_le": 4}
# 16 samples at 16kHz is ~34cm of sound: several times any Echo's mic array.
MAX_SPREAD = 16
FULL = {"s16_le": 32768.0, "s24_le": 8388608.0, "s24_3le": 8388608.0, "s32_le": 2147483648.0}


def decode(data, channels, fmt):
    w = WIDTH[fmt]
    frame = w * channels
    n = len(data) // frame
    chans = [[0] * n for _ in range(channels)]
    for i in range(n):
        base = i * frame
        for c in range(channels):
            o = base + c * w
            if fmt == "s16_le":
                v = struct.unpack_from("<h", data, o)[0]
            elif fmt == "s24_3le":
                v = data[o] | (data[o + 1] << 8) | (data[o + 2] << 16)
                if v & 0x800000:
                    v -= 1 << 24
            else:  # s24_le sits in the low 3 bytes of 4; s32_le uses all 4
                v = struct.unpack_from("<i", data, o)[0]
                if fmt == "s24_le":
                    v = ((v & 0xFFFFFF) ^ 0x800000) - 0x800000
            chans[c][i] = v
    return chans


def dbfs(x, full):
    return -200.0 if x <= 0 else 20 * math.log10(x / full)


def rms(xs):
    return math.sqrt(sum(v * v for v in xs) / len(xs)) if xs else 0.0


def corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = saa = sbb = 0.0
    for x, y in zip(a, b):
        dx, dy = x - ma, y - mb
        sab += dx * dy
        saa += dx * dx
        sbb += dy * dy
    return sab / math.sqrt(saa * sbb) if saa and sbb else 0.0


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(2)
    path, channels, fmt, rate = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    claps = [float(t) for t in sys.argv[5:]]
    full = FULL[fmt]
    chans = decode(open(path, "rb").read(), channels, fmt)
    n = len(chans[0])
    print(f"{n} frames, {n / rate:.2f}s, {channels} channels, {fmt}, {rate}Hz")

    quiet_end = int(min(claps) * rate) if claps else n
    print("\nch  silent  quiet_rms_dBFS  dc_dBFS  peak_dBFS")
    silent = set()
    for c, xs in enumerate(chans):
        if not any(xs):
            silent.add(c)
            print(f"{c:>2}  yes")
            continue
        q = xs[:quiet_end]
        dc = sum(q) / len(q)
        print(f"{c:>2}  no      {dbfs(rms(q), full):>14.1f}  {dbfs(abs(dc), full):>7.1f}  "
              f"{dbfs(max(abs(v) for v in xs), full):>9.1f}")

    live = [c for c in range(channels) if c not in silent]
    step = max(1, n // 32000)  # correlation on a decimated copy keeps this quick
    dup = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            r = corr(chans[a][::step], chans[b][::step])
            if r > 0.999:
                dup.append((a, b, r))
    print("\nduplicates (r > 0.999):", ", ".join(f"ch{a}=ch{b} ({r:.4f})" for a, b, r in dup) or "none")

    for t in claps:
        s, e = int(t * rate), min(n, int((t + 1.5) * rate))
        if s >= n:
            print(f"\nclap at {t}s: beyond the end of the capture")
            continue
        arrivals = []
        for c in live:
            seg = chans[c][s:e]
            peak = max(abs(v) for v in seg)
            floor = rms(chans[c][:quiet_end]) or 1.0
            if peak < 8 * floor:
                arrivals.append((None, c, peak))
                continue
            first = next(i for i, v in enumerate(seg) if abs(v) >= peak / 2)
            arrivals.append((first, c, peak))
        heard = sorted(a for a in arrivals if a[0] is not None)
        print(f"\nclap at {t}s: arrival order (samples after the first)")
        if not heard:
            print("  no channel rose clearly above its noise floor — was there a clap?")
            continue
        t0 = heard[0][0]
        for first, c, peak in heard:
            print(f"  ch{c}: +{first - t0}  peak {dbfs(peak, full):.1f} dBFS")
        missed = [c for f, c, _ in arrivals if f is None]
        if missed:
            print("  did not hear it:", ", ".join(f"ch{c}" for c in missed))
        # Sound crosses a small array in a few samples, so a clap reaches every
        # mic almost at once. A wide spread, or mics that missed it, means this
        # window caught ordinary room noise, and the order above means nothing.
        spread = heard[-1][0] - t0
        if missed or spread > MAX_SPREAD:
            print(f"  NOT A CLEAN CLAP (spread {spread} samples, {len(missed)} missed) — ignore this order")


if __name__ == "__main__":
    main()
