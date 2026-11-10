"""Measures for the audio bench. Pure numpy; the wake scorer is passed in.

A capture is what `capture_mics` writes: raw interleaved S24_3LE, 9 channels
at 16 kHz. Channels 0-6 are the microphones (6 is the centre one), 7 and 8
the playback loopback. The front end's output is 16 kHz mono S16.
"""

from __future__ import annotations

import numpy as np

RATE = 16000
CHANNELS = 9
MICS = 7
FRAME_BYTES = CHANNELS * 3
CHUNK = 1280            # 80 ms, the wake word's frame
FULL, BARGE = 0.5, 0.25
HOLD_S = 1.96           # wakeword.ScoreSpan: the low bar outlasts playback
REFRACT_S = 2.0
PHASES = 8              # positions of the 80 ms frame against the audio


def load_raw(path) -> np.ndarray:
    """[9, n] float in -1..1."""
    raw = np.fromfile(path, dtype=np.uint8)
    n = len(raw) // FRAME_BYTES
    raw = raw[:n * FRAME_BYTES].reshape(n, CHANNELS, 3).astype(np.int32)
    v = raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)
    v = np.where(v & 0x800000, v - 0x1000000, v)
    return (v / 8388608.0).T


def _db(power: float) -> float:
    return float(10 * np.log10(max(power, 1e-12)))


def mic_levels(x: np.ndarray) -> list[dict]:
    """Per microphone: level, the quietest and loudest 80 ms, and samples at
    full scale. `floor` is the 10th percentile of frame levels, `loud` the
    99th, so one click does not set either."""
    out = []
    for ch in range(MICS):
        c = x[ch]
        n = len(c) // CHUNK
        frames = (c[:n * CHUNK].reshape(n, CHUNK) ** 2).mean(axis=1) if n else np.zeros(1)
        fdb = 10 * np.log10(np.maximum(frames, 1e-12))
        out.append({
            "mic": ch,
            "rms_db": round(_db(float((c ** 2).mean()) if len(c) else 0.0), 2),
            "floor_db": round(float(np.percentile(fdb, 10)), 2),
            "loud_db": round(float(np.percentile(fdb, 99)), 2),
            "clipped": int((np.abs(c) >= 0.9999).sum()),
        })
    return out


def playing_mask(ref: np.ndarray, n_frames: int) -> tuple[np.ndarray, np.ndarray]:
    """Per 80 ms frame: (playback present, playback present or within the hold)."""
    active = np.array([np.abs(ref[i * CHUNK:(i + 1) * CHUNK]).max(initial=0) > 0
                       for i in range(n_frames)], dtype=bool)
    hold = int(round(HOLD_S / 0.08))
    low = active.copy()
    last = -10 ** 9
    for i, a in enumerate(active):
        if a:
            last = i
        elif i - last <= hold:
            low[i] = True
    return active, low


def detect(scores: np.ndarray, lowbar: np.ndarray) -> list[tuple[float, float, bool]]:
    """The device's rules: one frame at FULL, or two running at BARGE while
    playback is audible; then a refractory. (seconds, score, over playback)."""
    hits, prev_above, last = [], False, -10 ** 9
    for i, v in enumerate(scores):
        low = bool(lowbar[i]) if i < len(lowbar) else False
        above = v >= (BARGE if low else FULL)
        ok = above and (not low or prev_above) and (i - last) * 0.08 >= REFRACT_S
        prev_above = above
        if ok:
            hits.append((i * 0.08, float(v), low))
            last = i
    return hits


def wake(pcm: np.ndarray, ref: np.ndarray, score) -> dict:
    """Detections averaged over PHASES frame positions. One pass is not a
    measurement: a wake word's peak moved 0.96 -> 0.30 for a 2-4 ms shift
    (2026-09-22). `score(pcm_int16) -> per-frame scores`."""
    counts, peaks = [], []
    for k in range(PHASES):
        pad = np.zeros(k * CHUNK // PHASES, np.int16)
        p, r = np.concatenate([pad, pcm]), np.concatenate([pad, ref])
        s = np.asarray(score(p))
        _, low = playing_mask(r, len(s))
        hits = detect(s, low)
        counts.append(len(hits))
        peaks.append(float(s.max()) if len(s) else 0.0)
    return {
        "detections": round(float(np.mean(counts)), 3),
        "detections_min": int(min(counts)),
        "detections_max": int(max(counts)),
        "peak": round(float(np.mean(peaks)), 3),
    }


def echo_removed(mic: np.ndarray, out: np.ndarray, ref: np.ndarray) -> dict | None:
    """dB removed between the front end's input and output, over the seconds
    in which playback was present. None when nothing played."""
    n = min(len(mic), len(out), len(ref))
    active, _ = playing_mask(ref[:n], n // CHUNK)
    rows = []
    for t in range(n // RATE):
        fr = slice(t * RATE // CHUNK, (t + 1) * RATE // CHUNK)
        if not active[fr].any():
            continue
        a = mic[t * RATE:(t + 1) * RATE].astype(np.float64)
        b = out[t * RATE:(t + 1) * RATE].astype(np.float64)
        rows.append(_db(float((a ** 2).mean())) - _db(float((b ** 2).mean())))
    if not rows:
        return None
    return {"seconds": len(rows), "median_db": round(float(np.median(rows)), 2),
            "worst_db": round(float(np.min(rows)), 2),
            "first_second_db": round(float(rows[0]), 2)}


# ── what a stage costs on an Echo ────────────────────────────────────────────

def parse_probe(text: str) -> dict:
    """Read the cost lines a device probe prints after a paced run
    (`device/tools/oww_probe`, phase 2):

        frames     750 over 60.0s (0 overran their 80ms slot)
        latency    p50 28.9ms  p95 31.2ms  max 40.1ms  (budget 80ms)
        CPU        38.0% of one core  (22.80s of CPU in 60.0s wall)
        RSS        41234 KB peak

    Raises ValueError when the CPU line is absent: a report with no figure
    must not be stored as a measurement."""
    import re
    out: dict = {}
    m = re.search(r"CPU\s+([\d.]+)% of one core", text)
    if not m:
        raise ValueError("no 'CPU ...% of one core' line in the probe output")
    out["pct_of_core"] = float(m.group(1))
    m = re.search(r"latency\s+p50 ([\d.]+)ms\s+p95 ([\d.]+)ms\s+max ([\d.]+)ms", text)
    if m:
        out["p50_ms"], out["p95_ms"], out["max_ms"] = (float(v) for v in m.groups())
    m = re.search(r"frames\s+(\d+) over ([\d.]+)s \((\d+) overran", text)
    if m:
        out["frames"], out["seconds"], out["late_frames"] = int(m.group(1)), float(m.group(2)), int(m.group(3))
    m = re.search(r"RSS\s+(\d+) KB", text)
    if m:
        out["rss_kb"] = int(m.group(1))
    return out


# ── comparing two runs ───────────────────────────────────────────────────────

# (path into a capture's result, direction that is WORSE, tolerance)
CHECKS = [
    (("wake", "recall"), "down", 0.05),
    (("wake", "detections"), "down", 0.25),
    (("wake", "false_per_hour"), "up", 0.5),
    (("echo", "median_db"), "down", 1.0),
    (("echo", "first_second_db"), "down", 2.0),
    (("levels", "centre_floor_db"), "either", 1.5),
    (("levels", "clipped"), "up", 0),
    (("cpu", "pct_of_core"), "up", 2.0),
    (("cpu", "p95_ms"), "up", 3.0),
    (("cpu", "late_frames"), "up", 0),
]


def _get(d: dict, path: tuple[str, ...]):
    for k in path:
        if not isinstance(d, dict) or d.get(k) is None:
            return None
        d = d[k]
    return d


def compare(base: dict, new: dict) -> list[dict]:
    """One row per measure that both runs hold for a capture. `verdict` is
    worse, better or same; a capture or measure in only one run is `missing`,
    never a pass."""
    rows = []
    for cid in sorted(set(base) | set(new)):
        if cid not in base or cid not in new:
            rows.append({"capture": cid, "measure": "*", "base": None, "new": None,
                         "verdict": "missing"})
            continue
        for path, worse, tol in CHECKS:
            a, b = _get(base[cid], path), _get(new[cid], path)
            if a is None and b is None:
                continue
            if a is None or b is None:
                verdict = "missing"
            else:
                d = b - a
                if abs(d) <= tol:
                    verdict = "same"
                elif worse == "either":
                    verdict = "worse"
                else:
                    verdict = "worse" if (d < 0) == (worse == "down") else "better"
            rows.append({"capture": cid, "measure": ".".join(path), "base": a, "new": b,
                         "verdict": verdict})
    return rows
