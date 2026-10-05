#!/usr/bin/env python3
"""Score aec_replay outputs: wake word detections over playback, and echo
removed over time. Every variant starts cold, as aec_replay runs it.

    score.py out/C2 [out/C3b ...]

For each directory, every <variant>.wav except ref.wav is scored with the
device's classifier (hey_jarvis_v0.1) through openwakeword, which the device
reproduces tensor-for-tensor (internal/wakeword golden fixture).

Each variant is scored at OFFSETS phases across the 80ms frame and the
detection counts averaged. One pass is not a measurement: a single wake
word's peak score moved 0.96 -> 0.30 for a 2-4ms shift (C2, 2026-09-22),
because it depends on where the frames fall against the speech, which on the
device is random.

Detection rules are the device's: the full bar is one frame at >= 0.5; the
barge-in bar is two consecutive frames at >= 0.25 while playback is audible
(ref active, held for wakeword.ScoreSpan after it stops). Refractory 2s.
"""
import sys
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

CHUNK = 1280            # 80ms, the device's frame
FULL, BARGE = 0.5, 0.25
HOLD_S = 1.96           # wakeword.ScoreSpan
REFRACT_S = 2.0
OFFSETS = 8             # phases across one 80ms frame


def load(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def playing_mask(ref, n_frames):
    """Per 80ms frame: is playback audible (ref non-silent, plus the hold)."""
    active = np.array([np.abs(ref[i * CHUNK:(i + 1) * CHUNK]).max(initial=0) > 0
                       for i in range(n_frames)])
    hold = int(round(HOLD_S / 0.08))
    out = active.copy()
    last = -10**9
    for i, a in enumerate(active):
        if a:
            last = i
        elif i - last <= hold:
            out[i] = True
    return active, out


def scores(model, pcm):
    model.reset()
    return np.array([model.predict(pcm[i * CHUNK:(i + 1) * CHUNK])["hey_jarvis_v0.1"]
                     for i in range(len(pcm) // CHUNK)])


def detect(s, lowbar):
    hits, prev_above, last = [], False, -10**9
    for i, v in enumerate(s):
        bar = BARGE if lowbar[i] else FULL
        above = v >= bar
        ok = above and (not lowbar[i] or prev_above) and (i - last) * 0.08 >= REFRACT_S
        prev_above = above
        if ok:
            hits.append((i * 0.08, float(v), bool(lowbar[i])))
            last = i
    return hits


def erle_by_second(mic, out, ref_active):
    rows = []
    for t in range(len(mic) // 16000):
        fr = slice(t * 200 // 16, (t + 1) * 200 // 16)  # frames in this second
        if not ref_active[fr].any():
            continue
        a, b = mic[t * 16000:(t + 1) * 16000].astype(float), out[t * 16000:(t + 1) * 16000].astype(float)
        rows.append((t, 10 * np.log10((a ** 2).mean() / max((b ** 2).mean(), 1e-9))))
    return rows


def main(dirs):
    model = Model(wakeword_models=["hey_jarvis_v0.1"], inference_framework="onnx")
    for d in map(Path, dirs):
        ref = load(d / "ref.wav")
        mic = load(d / "mic.wav")
        n = len(ref) // CHUNK
        ref_active, _ = playing_mask(ref, n)
        pb = np.flatnonzero(ref_active)
        span = f"{pb[0]*0.08:.1f}-{pb[-1]*0.08:.1f}s" if len(pb) else "none"
        print(f"\n== {d.name}  (playback {span}; detections over playback, mean of {OFFSETS} phases)")
        for wav in sorted(d.glob("*.wav")):
            if wav.name == "ref.wav":
                continue
            pcm = load(wav)
            counts, times = [], []
            for k in range(OFFSETS):
                sh = k * CHUNK // OFFSETS
                pad = np.zeros(sh, np.int16)
                p2, r2 = np.concatenate([pad, pcm]), np.concatenate([pad, ref])
                _, lowbar = playing_mask(r2, len(r2) // CHUNK)
                s = scores(model, p2)
                hits = [(t - sh / 16000, v, lb) for t, v, lb in detect(s, lowbar[:len(s)])]
                over = [h for h in hits if h[2]]
                counts.append(len(over))
                times += [round(t) for t, _, _ in over]
            hist = {t: times.count(t) for t in sorted(set(times))}
            print(f"  {wav.stem:12s} {np.mean(counts):4.1f}  per phase {counts}  "
                  + " ".join(f"{t}s:{c}/{OFFSETS}" for t, c in hist.items()))
            if wav.stem != "mic" and len(pb):
                e = erle_by_second(mic, pcm, ref_active)
                print(f"  {'':12s} echo removed/s: " + " ".join(f"{t}:{v:.0f}" for t, v in e))


if __name__ == "__main__":
    main(sys.argv[1:])
