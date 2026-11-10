#!/usr/bin/env python3
"""Audio bench: replay recorded captures through the listening pipeline and
score them the same way every time (#892).

    bench.py run CORPUS --replay BIN [--out results.json] [--baseline base.json]
                 [--cpu wake=probe.txt ...]
    bench.py compare base.json new.json

CORPUS is a folder holding `corpus.json` and the raw captures it names. Each
capture is run through the firmware's own front end (`device/tools/aec_replay`
built for this machine, see README) and the output is scored. With a baseline
the run exits 1 if any measure is worse or missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

import metrics as M


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class Scorer:
    """openWakeWord, one model, reset between passes."""

    def __init__(self, model: str):
        from openwakeword.model import Model
        self._m = Model(wakeword_models=[model], inference_framework="onnx")
        self._key = next(iter(self._m.models))

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        self._m.reset()
        return np.array([self._m.predict(pcm[i * M.CHUNK:(i + 1) * M.CHUNK])[self._key]
                         for i in range(len(pcm) // M.CHUNK)])


def front_end(replay: Path, raw: Path, cap: dict, work: Path) -> dict[str, np.ndarray]:
    cmd = [str(replay), "-in", str(raw), "-out", str(work), "-gain", str(cap.get("gain_db", 24))]
    if cap.get("mics"):
        cmd += ["-mics", cap["mics"]]
        if cap.get("per_mic", True):
            cmd.append("-per-mic")
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"aec_replay failed on {raw.name}: {done.stderr.strip()[-300:]}")
    return {n: load_wav(work / f"{n}.wav") for n in ("mic", "ref", "speex")}


def run_capture(corpus: Path, cap: dict, replay: Path, scorers: dict) -> dict:
    raw = corpus / cap["file"]
    x = M.load_raw(raw)
    levels = M.mic_levels(x)
    with tempfile.TemporaryDirectory() as tmp:
        fe = front_end(replay, raw, cap, Path(tmp))
    model = cap.get("model", "hey_jarvis_v0.1")
    if model not in scorers:
        scorers[model] = Scorer(model)
    w = M.wake(fe["speex"], fe["ref"], scorers[model])
    seconds = x.shape[1] / M.RATE
    if cap.get("speech_free"):
        w["false_per_hour"] = round(w["detections"] * 3600 / seconds, 2)
    elif cap.get("wakes"):
        w["expected"] = cap["wakes"]
        w["recall"] = round(min(w["detections"], cap["wakes"]) / cap["wakes"], 3)
        extra = max(0.0, w["detections"] - cap["wakes"])
        w["false_per_hour"] = round(extra * 3600 / seconds, 2)
    return {
        "scenario": cap.get("scenario", ""),
        "seconds": round(seconds, 1),
        "wake": w,
        "echo": M.echo_removed(fe["mic"], fe["speex"], fe["ref"]),
        "levels": {
            "centre_floor_db": levels[6]["floor_db"],
            "centre_loud_db": levels[6]["loud_db"],
            "spread_db": round(max(l["rms_db"] for l in levels) - min(l["rms_db"] for l in levels), 2),
            "clipped": sum(l["clipped"] for l in levels),
            "mics": levels,
        },
    }


def table(results: dict) -> str:
    results = {k: v for k, v in results.items() if "wake" in v}
    rows = ["capture                 scenario               wakes  recall  false/h  echo dB (first s)  floor  clip"]
    for cid, r in results.items():
        w, e, lv = r["wake"], r["echo"], r["levels"]
        wakes = f"{w['detections']:.1f}/{w['expected']}" if "expected" in w else f"{w['detections']:.1f}"
        rows.append(
            f"{cid:23.23s} {r['scenario']:22.22s} {wakes:>6s} "
            f"{w.get('recall', float('nan')):6.2f}  {w.get('false_per_hour', float('nan')):6.1f}  "
            + (f"{e['median_db']:5.1f} ({e['first_second_db']:5.1f})" if e else "      -        ").ljust(17)
            + f"  {lv['centre_floor_db']:6.1f} {lv['clipped']:5d}")
    return "\n".join(rows)


def show_compare(rows: list[dict]) -> bool:
    bad = [r for r in rows if r["verdict"] in ("worse", "missing")]
    for r in rows:
        if r["verdict"] != "same":
            print(f"  {r['verdict'].upper():7s} {r['capture']:24s} {r['measure']:24s} "
                  f"{r['base']} -> {r['new']}")
    same = sum(r["verdict"] == "same" for r in rows)
    print(f"{same} unchanged, {sum(r['verdict'] == 'better' for r in rows)} better, {len(bad)} worse or missing")
    return not bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("corpus", type=Path)
    r.add_argument("--replay", type=Path, required=True, help="aec_replay built for this machine")
    r.add_argument("--out", type=Path)
    r.add_argument("--baseline", type=Path)
    r.add_argument("--only", nargs="*", help="capture ids to run")
    r.add_argument("--cpu", nargs="*", default=[], metavar="STAGE=FILE",
                   help="output of a device probe run on an Echo, stored as cpu:STAGE")
    c = sub.add_parser("compare")
    c.add_argument("base", type=Path)
    c.add_argument("new", type=Path)
    a = ap.parse_args()

    if a.cmd == "compare":
        return 0 if show_compare(M.compare(json.loads(a.base.read_text())["captures"],
                                           json.loads(a.new.read_text())["captures"])) else 1

    manifest = json.loads((a.corpus / "corpus.json").read_text())
    scorers: dict = {}
    results = {}
    for cap in manifest["captures"]:
        if a.only and cap["id"] not in a.only:
            continue
        print(f"… {cap['id']}", file=sys.stderr, flush=True)
        results[cap["id"]] = run_capture(a.corpus, cap, a.replay, scorers)
    print(table(results))
    for item in a.cpu:
        stage, _, path = item.partition("=")
        cost = M.parse_probe(Path(path).read_text())
        results[f"cpu:{stage}"] = {"cpu": cost}
        print(f"cpu:{stage:19s} {cost['pct_of_core']:.1f}% of one core"
              + (f", p95 {cost['p95_ms']:.1f} ms" if "p95_ms" in cost else "")
              + (f", {cost['late_frames']} late frames" if "late_frames" in cost else ""))
    doc = {"corpus": manifest.get("name", a.corpus.name), "captures": results}
    if a.out:
        a.out.write_text(json.dumps(doc, indent=1))
    if a.baseline:
        print()
        return 0 if show_compare(M.compare(json.loads(a.baseline.read_text())["captures"], results)) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
