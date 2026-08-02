"""
em_wake_capture.py — audio around on-device wake crossings that matched no turn

Shadow mode tells us the device crossed its wake threshold 26 times, over 155
device-hours, where no turn followed. That number is the whole gate on
`owwOnDevice=on`: on the shadow path an unmatched crossing is a log line, but
a device that TRIGGERS turns them into turns nobody asked for — ring, mic,
and an utterance sent to Home Assistant.

**"Unmatched" is not the same as "false accept", and the counters cannot tell
them apart.** Each one is either

  - a genuine wake the CONTROLLER missed, which argues *for* shipping this,
  - a crossing during a turn or TTS, when the controller was not scoring, or
  - a real false trigger, which argues against.

Those three want opposite decisions, so the only way through is to listen.
This module keeps a short rolling buffer of the wake stream per device and,
when a crossing turns out to have matched nothing, writes the seconds either
side of it to a WAV.

## Why the check is deferred, and why not by the obvious amount

A crossing is correlated with a turn at TURN-PERSIST time, not at detection —
the report can land after the wake it belongs to, and by turn end it has had
seconds to arrive (see em_shadow). So "is it still in the tracker's ring a
moment later" answers the wrong question: a turn can run for 30 seconds, and
every genuine wake would look unmatched.

The check instead compares the crossing against the controller's own most
recent wake instant (`Device.last_wake_mono`), which is set at DETECTION.
That is the same comparison em_shadow will make later, available immediately,
and it needs no coordination with turn lifetime.

## Privacy

This writes recognisable speech to disk, and worse than the utterance capture
does: an unmatched crossing is by definition audio nobody addressed to the
assistant. It is therefore **opt-in per device and off by default**
(`captureWakeMisses`), stored separately from `recordings/`, and capped hard
by file count. It is a diagnostic for answering one question, not a feature.
"""

from __future__ import annotations

import collections
import io
import os
import re
import time
import wave
from pathlib import Path

# Wire format of the mic stream: mono S16LE at 16kHz, in 80ms frames.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2

CAPTURES_SUBDIR = "wake_captures"

# Seconds of audio kept either side of the crossing. Enough before it to hear
# what was said INTO the trigger — a false accept usually has a cause a few
# syllables earlier — and enough after to hear whether a human carried on
# talking, which is what distinguishes "the controller missed a real wake"
# from "the television said something".
PRE_S = 3.0
POST_S = 2.0

# The rolling buffer must cover PRE_S plus the age of the report when it
# arrives plus the wait for the deferred check, with room to spare. 15s of
# 16kHz mono is ~480KB per device — bounded, and only allocated for devices
# with the capture switched on.
RING_S = 15.0

# Hard per-device cap. Small on purpose: this is meant to be reviewed and
# acted on within days, not to accumulate.
KEEP_PER_DEVICE = 30

# How long after the crossing to decide. MATCH_WINDOW_S (2.0s) is how far
# apart the two detectors may be and still be one utterance; the extra second
# covers the controller detecting slightly later than the device, which is the
# expected direction — the device scores the frame it just captured, the
# controller scores it after a network hop.
DECIDE_AFTER_S = 3.0

_NAME_RE = re.compile(r"^(?P<device>[A-Za-z0-9_.-]{1,64})_(?P<ts>\d{10,19})"
                      r"_(?P<score>\d{1,4})\.wav$")


def captures_dir(db_path: str | None = None) -> Path:
    """`wake_captures/` beside the SQLite DB. Absolute, so cwd cannot move it."""
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / CAPTURES_SUBDIR


def safe_device_id(device_id: str) -> str | None:
    if device_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device_id):
        return device_id
    return None


def filename(device_id: str, at_wall: float, score: float) -> str | None:
    """
    Name a capture. The score rides in the filename (×1000, integer) so a
    directory listing is triageable without opening anything.
    """
    safe = safe_device_id(device_id)
    if safe is None:
        return None
    score_i = max(0, min(9999, int(round(float(score) * 1000))))
    return f"{safe}_{int(at_wall * 1000)}_{score_i}.wav"


def parse_filename(name: str) -> tuple[str, float, float] | None:
    """(device_id, wall seconds, score) — or None if it is not one of ours."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return m["device"], int(m["ts"]) / 1000.0, int(m["score"]) / 1000.0


class AudioRing:
    """
    A bounded rolling buffer of the wake stream, timestamped on arrival.

    Frames are appended on the data-plane hot path (~12/s per device), so this
    does exactly one deque append and one running-total update per frame — no
    allocation, no scan, no lock. The deque is trimmed from the left by total
    DURATION rather than frame count, because a frame is only nominally 80ms
    and a count-based bound would drift.
    """

    def __init__(self, seconds: float = RING_S):
        self._frames: collections.deque = collections.deque()
        self._bytes = 0
        self._max_bytes = int(seconds * SAMPLE_RATE * SAMPLE_WIDTH)

    def append(self, at: float, payload: bytes) -> None:
        if not payload:
            return
        self._frames.append((at, payload))
        self._bytes += len(payload)
        while self._bytes > self._max_bytes and len(self._frames) > 1:
            self._bytes -= len(self._frames.popleft()[1])

    def clear(self) -> None:
        self._frames.clear()
        self._bytes = 0

    @property
    def seconds(self) -> float:
        return self._bytes / (SAMPLE_RATE * SAMPLE_WIDTH)

    def extract(self, centre: float, pre: float = PRE_S,
                post: float = POST_S) -> bytes:
        """
        PCM spanning [centre-pre, centre+post], by frame arrival time.

        Frames are taken whole: a frame is 80ms, and trimming to the exact
        sample would imply a precision the arrival timestamps do not have.
        Returns b"" when the window has already aged out of the ring, which
        is a real outcome rather than an error — a capture is not worth
        holding the hot path to guarantee.
        """
        lo, hi = centre - pre, centre + post
        return b"".join(p for (at, p) in self._frames if lo <= at <= hi)


def save(device_id: str, pcm: bytes, at_wall: float, score: float,
         db_path: str | None = None, keep: int = KEEP_PER_DEVICE) -> str | None:
    """
    Write one capture and prune the device back to `keep` files.

    Returns the filename, or None if there was nothing to write. Blocking —
    call it in an executor.
    """
    name = filename(device_id, at_wall, score)
    if name is None or not pcm:
        return None
    directory = captures_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # Write-then-rename: a partially written WAV that the API then serves is
    # worse than no capture at all.
    tmp = path.with_suffix(".wav.part")
    tmp.write_bytes(encode_wav(pcm))
    tmp.replace(path)
    prune(device_id, db_path=db_path, keep=keep)
    return name


def encode_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm_len: int) -> int:
    return int(pcm_len / (SAMPLE_RATE * SAMPLE_WIDTH) * 1000)


def list_for(device_id: str, db_path: str | None = None) -> list[dict]:
    """This device's captures, newest first, with their metadata."""
    safe = safe_device_id(device_id)
    if safe is None:
        return []
    directory = captures_dir(db_path)
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        parsed = parse_filename(p.name)
        if not parsed or parsed[0] != safe or not p.is_file():
            continue
        out.append({
            "name": p.name,
            "ts": parsed[1],
            "score": parsed[2],
            "bytes": p.stat().st_size,
            "duration_ms": duration_ms(max(p.stat().st_size - 44, 0)),
        })
    out.sort(key=lambda d: d["ts"], reverse=True)
    return out


def prune(device_id: str, db_path: str | None = None,
          keep: int = KEEP_PER_DEVICE) -> list[str]:
    """Delete all but the newest `keep`. Returns what was removed."""
    removed = []
    for entry in list_for(device_id, db_path)[keep:]:
        path = captures_dir(db_path) / entry["name"]
        try:
            path.unlink()
            removed.append(entry["name"])
        except OSError:
            pass
    return removed


def resolve(device_id: str, name: str, db_path: str | None = None) -> Path | None:
    """
    The path for a capture, or None.

    Re-checks that the file belongs to the device in the URL — the endpoint
    takes both from the path, and one must not be able to name the other's
    audio. A missing file is an ordinary None, not an error: retention is a
    hard count, so an older reference is a claim to check rather than trust.
    """
    parsed = parse_filename(name)
    safe = safe_device_id(device_id)
    if parsed is None or safe is None or parsed[0] != safe:
        return None
    path = captures_dir(db_path) / name
    return path if path.is_file() else None


def delete_device(device_id: str, db_path: str | None = None) -> int:
    """Remove every capture for a device. Nothing cascades to the filesystem."""
    n = 0
    for entry in list_for(device_id, db_path):
        try:
            (captures_dir(db_path) / entry["name"]).unlink()
            n += 1
        except OSError:
            pass
    return n


def is_matched(cross_mono: float, last_wake_mono: float | None,
               window_s: float) -> bool:
    """
    Whether a crossing lines up with a controller wake.

    Deliberately the same comparison em_shadow.match makes, and deliberately
    NOT "did the tracker consume it": consumption happens at turn-persist
    time, which can be 30 seconds after a wake, so asking early would report
    every genuine wake as unmatched. `last_wake_mono` is set at detection.
    """
    if not last_wake_mono:
        return False
    return abs(cross_mono - last_wake_mono) <= window_s
