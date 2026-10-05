#!/usr/bin/env python3
"""
Reference vectors for device/internal/outchain, generated from the
controller's own output chain (em_eq.StreamingEQ + em_mbc.BassGuard +
em_limiter.Limiter). The Go port is held to these sample for sample.

    cd controller && python ../device/internal/outchain/testdata/gen_vectors.py

Processing runs in 2048-sample chunks, the device's period, and every
parameter change lands on a chunk boundary, as it does on the device. The
limiter and guard are always INSTANCES with an enabled flag, never None:
that is how the device holds them, and None would skip the guard's allpass
and the limiter's delay.

controller/tests/test_outchain_vectors.py re-renders every case and checks
the committed files still match, so a change to the Python that is not
carried to the Go fails there rather than drifting silently.
"""

import gzip
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "controller"))

import em_eq       # noqa: E402
import em_limiter  # noqa: E402
import em_mbc      # noqa: E402

FS = 48000
CHUNK = 2048

DEFAULTS = {
    "bands": [0.0] * 8, "loudness": False,
    "guardEnabled": True, "guardDb": -30.0,
    "limiterEnabled": True, "limiterThreshold": -1.0, "limiterRelease": 150.0,
}


def _signal(kind: str, chunks: int, seed: int) -> np.ndarray:
    n = chunks * CHUNK
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed)
    if kind == "speechlike":
        # Bass-heavy harmonic series under a syllable-rate envelope, plus
        # noise: peaks near full scale, and real content below 115Hz.
        f0 = 110 + 30 * np.sin(2 * np.pi * 3 * t)
        phase = 2 * np.pi * np.cumsum(f0) / FS
        x = sum(np.sin(k * phase) / k for k in range(1, 12))
        x += 0.8 * np.sin(2 * np.pi * 55 * t)
        env = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 4 * t))
        x = x * env + 0.05 * rng.standard_normal(n)
        x = x / np.max(np.abs(x)) * 32000
    elif kind == "sweep":
        f = np.geomspace(30, 16000, n)
        x = 30000 * np.sin(2 * np.pi * np.cumsum(f) / FS)
    elif kind == "square":
        # Full-scale square wave: every stage's edge case at once.
        x = np.where(np.sin(2 * np.pi * 220 * t) >= 0, 32767.0, -32768.0)
    elif kind == "bursts":
        # Loud bursts separated by exact digital silence, ending in silence.
        x = 32000 * np.sin(2 * np.pi * 440 * t) * (np.floor(t * 8) % 2 == 0)
        x[-3 * CHUNK:] = 0.0
    else:
        raise ValueError(kind)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


CASES = [
    {"name": "defaults_speech", "signal": "speechlike", "chunks": 6, "seed": 1,
     "schedule": [[0, {}]]},
    {"name": "boost_all_limited", "signal": "speechlike", "chunks": 6, "seed": 2,
     "schedule": [[0, {"bands": [12.0] * 8, "loudness": True}]]},
    {"name": "all_bypassed", "signal": "sweep", "chunks": 6, "seed": 3,
     "schedule": [[0, {"guardEnabled": False, "limiterEnabled": False}]]},
    {"name": "mixed_curve", "signal": "sweep", "chunks": 6, "seed": 4,
     "schedule": [[0, {"bands": [6.0, -3.0, 0.0, 2.5, -12.0, 4.0, 0.0, -6.0],
                       "guardEnabled": False, "limiterThreshold": -6.0,
                       "limiterRelease": 40.0}]]},
    {"name": "square_bypassed_clips", "signal": "square", "chunks": 4, "seed": 5,
     "schedule": [[0, {"bands": [0, 0, 0, 6.0, 0, 0, 0, 0],
                       "limiterEnabled": False}]]},
    {"name": "bursts_silence", "signal": "bursts", "chunks": 8, "seed": 6,
     "schedule": [[0, {"bands": [3.0] * 8}]]},
    # Deep limiting, bypass, then re-enabled with headroom: the Python's
    # bypass leaves the gain at 0dB, so the re-enabled limiter starts from
    # unity. Carrying the old -12dB through instead would release slowly
    # from it, audible as a dip — and no other case can tell the two apart.
    {"name": "limiter_reenable", "signal": "speechlike", "chunks": 8, "seed": 8,
     "schedule": [
         [0, {"limiterThreshold": -12.0, "guardEnabled": False}],
         [3, {"limiterEnabled": False}],
         [4, {"limiterEnabled": True, "limiterThreshold": 0.0}],
     ]},
    # Every kind of mid-stream change, each on its own chunk boundary.
    {"name": "live_changes", "signal": "speechlike", "chunks": 20, "seed": 7,
     "schedule": [
         [0, {}],
         [3, {"bands": [9.0, 6.0, 0, 0, 0, 0, 3.0, 0]}],          # flat -> shaped
         [6, {"limiterEnabled": False}],                          # limiter bypass
         [8, {"limiterEnabled": True, "limiterThreshold": -8.0}],
         [10, {"guardDb": -10.0}],                                # guard depth
         [12, {"loudness": True}],                                # 8 -> 9 sections
         [14, {"guardEnabled": False, "limiterRelease": 600.0}],
         [16, {"bands": [0.0] * 8, "loudness": False}],           # back to flat
         [18, {"bands": [-6.0] * 8, "guardEnabled": True}],       # shaped again
     ]},
]


def _params_at(schedule, chunk):
    p = dict(DEFAULTS)
    for at, delta in schedule:
        if at <= chunk:
            p.update(delta)
    return p


def render(case):
    """(input int16, output int16, stats) for one case."""
    x = _signal(case["signal"], case["chunks"], case["seed"])
    p0 = _params_at(case["schedule"], 0)
    lim = em_limiter.Limiter(FS, threshold_db=p0["limiterThreshold"],
                             release_ms=p0["limiterRelease"],
                             enabled=p0["limiterEnabled"])
    guard = em_mbc.BassGuard(FS, bass_guard_db=p0["guardDb"],
                             enabled=p0["guardEnabled"])
    chain = em_eq.StreamingEQ(FS, p0["bands"], p0["loudness"],
                              limiter=lim, guard=guard)
    out = []
    for c in range(case["chunks"]):
        p = _params_at(case["schedule"], c)
        chain.update(bands=p["bands"], loudness=p["loudness"],
                     limiter_enabled=p["limiterEnabled"],
                     limiter_threshold=p["limiterThreshold"],
                     limiter_release=p["limiterRelease"],
                     guard_enabled=p["guardEnabled"], guard_db=p["guardDb"])
        out.append(chain.process(x[c * CHUNK:(c + 1) * CHUNK].tobytes()))
    y = np.frombuffer(b"".join(out), dtype=np.int16)
    stats = {
        "guardReductionDb": float(guard._bass.max_reduction_db),
        "limiterReductionDb": float(lim.max_reduction_db),
        "clipped": int(lim.clipped),
        "clippedBypassed": int(lim.clipped_bypassed),
    }
    return x, y, stats


def manifest_entry(case, stats):
    return {"name": case["name"], "chunk": CHUNK, "sampleRate": FS,
            "chunks": case["chunks"],
            "schedule": [[at, _params_at(case["schedule"], at)]
                         for at, _ in case["schedule"]],
            "stats": stats}


def main():
    manifest = []
    for case in CASES:
        x, y, stats = render(case)
        for suffix, data in (("in", x), ("out", y)):
            path = os.path.join(HERE, f"{case['name']}.{suffix}.s16.gz")
            # mtime=0 so regenerating identical audio is a no-op in git.
            with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb",
                                                      mtime=0) as gz:
                gz.write(data.astype("<i2").tobytes())
        manifest.append(manifest_entry(case, stats))
    with open(os.path.join(HERE, "vectors.json"), "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
