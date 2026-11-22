#!/usr/bin/env python3
"""Replay an Echo's wake scorer inputs through any wake models, offline.

A bench build records what each scorer was given beside a raw capture
(`rawcap-<t>.s` the wake stream, `.t0`... the reply taps; `.ts` the arrival
time of every 80 ms chunk). This scores those exact bytes with each model given,
so a model can be judged on the audio the device's own scorers heard, after the
device's own echo canceller.

    python3 barge_replay.py --models a.onnx b.onnx --bars 0.25/0.5 0.15/0.3 \
        --mel melspectrogram.onnx --emb embedding_model.onnx  rawcap-*.ts

--bars gives each model its barge rule, LOW/HIGH: two consecutive frames at
LOW, or one at HIGH (em_barge); several rules for one model, comma-separated,
are judged on one scoring pass. A fire is any stream meeting the rule; fires
closer than --refractory seconds count once. Prints one line per fire with its
capture and second, for checking by ear, then a total per model and per hour of
audio scored during replies (the tap streams run only while a reply plays).

A capture with nobody talking gives false barges; one with tries gives catches.
"""
import argparse
import collections
import os

import numpy as np
from openwakeword.model import Model

CHUNK = 1280            # 80 ms at 16 kHz, S16LE mono
GAP_NS = 0.5e9          # a tap restarts each reply: reset its model on a gap


def streams(ts_path):
    """{tag: (int16 samples, [arrival ns per chunk])} for the scorer streams."""
    base = ts_path[:-3]
    times = collections.defaultdict(list)
    for line in open(ts_path):
        k, ns = line.split()[:2]
        times[k].append(int(ns))
    out = {}
    for tag in sorted(times):
        if tag != "s" and not tag.startswith("t"):
            continue
        path = f"{base}.{tag}"
        if not os.path.exists(path):
            continue
        x = np.fromfile(path, dtype="<i2")
        # .ts lists each write; writes are whole chunks or halves of one
        n = len(x) // CHUNK
        t = np.array(times[tag], dtype=np.int64)
        per = max(1, round(len(t) / n)) if n else 1
        out[tag] = (x[:n * CHUNK], t[per - 1::per][:n])
    return out


def score(x, t, model_path, mel, emb):
    m = Model(wakeword_models=[model_path], inference_framework="onnx",
              melspec_model_path=mel, embedding_model_path=emb)
    s, prev = np.zeros(len(t)), None
    for i in range(len(t)):
        if prev is not None and t[i] - prev > GAP_NS:
            m.reset()
        prev = t[i]
        s[i] = list(m.predict(x[i * CHUNK:(i + 1) * CHUNK]).values())[0]
    return s


def fires(s, t, lo, hi):
    out = []
    for i in range(len(s)):
        two = i > 0 and s[i] >= lo and s[i - 1] >= lo and t[i] - t[i - 1] < GAP_NS
        if s[i] >= hi or two:
            out.append((t[i], s[i]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ts", nargs="+", help="rawcap-*.ts files")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--bars", nargs="+", required=True, help="LOW/HIGH[,LOW/HIGH...] per model")
    ap.add_argument("--mel", required=True)
    ap.add_argument("--emb", required=True)
    ap.add_argument("--refractory", type=float, default=2.0)
    a = ap.parse_args()
    if len(a.bars) != len(a.models):
        ap.error("one --bars entry per model")
    bars = [[tuple(float(v) for v in r.split("/")) for r in b.split(",")] for b in a.bars]
    total = {(m, r): 0 for m, rs in zip(a.models, bars) for r in rs}
    reply_s = 0.0
    for ts in sorted(a.ts):
        st = streams(ts)
        taps = [k for k in st if k.startswith("t")]
        if taps:
            reply_s += len(st[taps[0]][1]) * CHUNK / 16000
        t0 = min(v[1][0] for v in st.values() if len(v[1]))
        for mp, rules in zip(a.models, bars):
            scored = {tag: (score(x, t, mp, a.mel, a.emb), t) for tag, (x, t) in st.items()}
            for lo, hi in rules:
                ev = []
                for tag, (s, t) in scored.items():
                    ev += [(tt, sc, tag) for tt, sc in fires(s, t, lo, hi)]
                ev.sort()
                last = None
                for tt, sc, tag in ev:
                    if last is not None and tt - last < a.refractory * 1e9:
                        continue
                    last = tt
                    total[(mp, (lo, hi))] += 1
                    print(f"{os.path.basename(mp)} {lo}/{hi}  {os.path.basename(ts)[:-3]}  +{(tt - t0) / 1e9:6.1f}s  {tag}  {sc:.2f}", flush=True)
    hours = reply_s / 3600
    print(f"\n{reply_s / 60:.1f} min of reply scored on the taps")
    for (mp, (lo, hi)), n in total.items():
        rate = f"{n / hours:.1f}/h" if hours else "-"
        print(f"  {os.path.basename(mp):28s} bars {lo}/{hi}: {n} fires ({rate})")


if __name__ == "__main__":
    main()
