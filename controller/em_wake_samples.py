"""Bounded wake-word candidate audio capture.

The controller already receives continuous 16 kHz mono mic frames for wake
scoring. Keep only a short in-memory pre-roll, and write WAVs when a score
candidate or real trigger asks us to retain a clip.
"""
from __future__ import annotations

import collections
import io
import json
import os
import re
import shutil
import time
import uuid
import wave
from pathlib import Path

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
PRE_ROLL_BYTES = int(1.5 * SAMPLE_RATE * SAMPLE_WIDTH)
# A candidate keeps the long tail: there is no sharp "this is where it ended" moment
# for a score that never crossed the real threshold, so extra context helps a reviewer.
# A trigger is a precise, known-timestamp event and gets none added (post-roll 0) plus
# a fixed trim off the end instead - the pre-roll snapshot is taken at the moment the
# trigger fires, and any lag between the wake word actually ending and the score
# crossing threshold is already baked into that snapshot, not into anything a post-roll
# duration could remove.
POST_ROLL_SECONDS = 1.25
TRIGGER_POST_ROLL_SECONDS = 0
TRIGGER_TAIL_TRIM_SECONDS = 0.2
TRIGGER_TAIL_TRIM_BYTES = int(TRIGGER_TAIL_TRIM_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH)
MAX_CLIP_BYTES = 5 * SAMPLE_RATE * SAMPLE_WIDTH
KEEP_PER_DEVICE = 50
SUBDIR = "wake_samples"
# Labeling a sample also copies it here, permanently and uncapped - the 50-per-device
# cap above still applies to the reviewable copy, so a label is what survives it.
ARCHIVE_SUBDIR = "wake_samples_archive"
ARCHIVE_LABELS = ("wake", "not_wake", "uncertain")
_NAME_RE = re.compile(r"^(?P<device>[A-Za-z0-9_.-]{1,64})_(?P<token>[a-f0-9]{32})\.wav$")


def samples_dir(db_path: str | None = None) -> Path:
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / "recordings" / SUBDIR


def archive_dir(label: str, db_path: str | None = None) -> Path:
    """Where a labeled sample's permanent, uncapped copy lives, one directory per label."""
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / "recordings" / ARCHIVE_SUBDIR / label


def safe_device_id(device_id: str) -> bool:
    return bool(device_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device_id))


def filename(device_id: str) -> str | None:
    return f"{device_id}_{uuid.uuid4().hex}.wav" if safe_device_id(device_id) else None


def parse_filename(name: str) -> str | None:
    match = _NAME_RE.fullmatch(name or "")
    return match.group("device") if match else None


def encode_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def save(device_id: str, name: str, pcm: bytes, db_path: str | None = None) -> bool:
    if parse_filename(name) != device_id or not pcm:
        return False
    directory = samples_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_suffix(".wav.part")
    tmp.write_bytes(encode_wav(pcm))
    tmp.replace(path)
    return True


def resolve(device_id: str, name: str, db_path: str | None = None) -> Path | None:
    if parse_filename(name) != device_id:
        return None
    path = samples_dir(db_path) / name
    return path if path.is_file() else None


def archive(device_id: str, name: str, label: str, meta: dict, db_path: str | None = None) -> bool:
    """Copy a labeled sample plus its metadata into the permanent archive.
    Best-effort: a False return should not fail the label call itself."""
    if parse_filename(name) != device_id or label not in ARCHIVE_LABELS:
        return False
    src = samples_dir(db_path) / name
    if not src.is_file():
        return False
    try:
        directory = archive_dir(label, db_path)
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, directory / name)
        (directory / (name[:-len(".wav")] + ".json")).write_text(json.dumps(meta, indent=2))
        return True
    except OSError:
        return False


def unlink(device_id: str, name: str, db_path: str | None = None) -> bool:
    """Remove one owned sample file; report errors so its DB row can remain."""
    if parse_filename(name) != device_id:
        return False
    try:
        (samples_dir(db_path) / name).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def remove(device_id: str, names, db_path: str | None = None) -> None:
    """Best-effort cleanup for files whose database insert did not succeed."""
    for name in names:
        unlink(device_id, name, db_path)


class WakeCapture:
    """One device's in-memory rolling audio and a single coalesced candidate."""

    def __init__(self):
        self.history = collections.deque()
        self.history_bytes = 0
        self.active = None
        self.above_floor = False

    def reset(self):
        self.history.clear()
        self.history_bytes = 0
        self.active = None
        self.above_floor = False

    def _history_pcm(self) -> bytes:
        return b"".join(self.history)

    def feed_audio(self, pcm: bytes, now: float | None = None):
        """Add one wire frame. Return a completed candidate when post-roll ends."""
        if not pcm:
            return None
        now = time.monotonic() if now is None else now
        self.history.append(pcm)
        self.history_bytes += len(pcm)
        while self.history and self.history_bytes > PRE_ROLL_BYTES:
            self.history_bytes -= len(self.history.popleft())
        if self.active is None:
            return None
        self.active["pcm"].extend(pcm)
        if len(self.active["pcm"]) >= MAX_CLIP_BYTES or now >= self.active["until"]:
            return self._finish()
        return None

    def consider(self, *, enabled: bool, minimum: float, score: float,
                 threshold: float, model: str, device_score=None,
                 trigger_source: str | None = None, now: float | None = None):
        """Start/update a clip on a score candidate or any actual trigger."""
        now = time.monotonic() if now is None else now
        triggered = bool(trigger_source)
        if not enabled:
            self.above_floor = False
            return
        score = None if score is None else float(score)
        candidate = score is not None and score >= minimum
        rising_candidate = candidate and not self.above_floor
        self.above_floor = candidate
        if self.active is not None:
            if score is not None:
                prior = self.active["score"]
                self.active["score"] = score if prior is None else max(prior, score)
            if device_score is not None:
                prior = self.active["device_score"]
                self.active["device_score"] = (
                    float(device_score) if prior is None
                    else max(prior, float(device_score))
                )
        if not rising_candidate and not triggered and self.active is None:
            return
        if not candidate and not triggered:
            return
        if self.active is None:
            kind = "trigger" if triggered else "candidate"
            self.active = {
                "pcm": bytearray(self._history_pcm()), "started": now,
                "ts": time.time(),
                "until": now + (TRIGGER_POST_ROLL_SECONDS if kind == "trigger" else POST_ROLL_SECONDS),
                "kind": kind,
                "model": model, "score": score, "threshold": float(threshold),
                "device_score": device_score, "trigger_source": trigger_source,
            }
            return
        active = self.active
        if score is not None:
            active["score"] = score if active["score"] is None else max(active["score"], score)
        if device_score is not None:
            active["device_score"] = max(active["device_score"] or 0.0, float(device_score))
        if triggered:
            active["kind"] = "trigger"
            active["trigger_source"] = trigger_source
        # A trigger's re-arm always uses the short tail, never the candidate window.
        if now - active["started"] < MAX_CLIP_BYTES / (SAMPLE_RATE * SAMPLE_WIDTH):
            active["until"] = now + (TRIGGER_POST_ROLL_SECONDS if active["kind"] == "trigger" else POST_ROLL_SECONDS)

    def _finish(self):
        active, self.active = self.active, None
        if active is None:
            return None
        pcm = bytes(active["pcm"])
        # Only a trigger is trimmed, and only if long enough that it can't go negative.
        if active["kind"] == "trigger" and len(pcm) > TRIGGER_TAIL_TRIM_BYTES:
            pcm = pcm[:-TRIGGER_TAIL_TRIM_BYTES]
        return {
            "pcm": pcm, "kind": active["kind"],
            "model": active["model"], "score": active["score"],
            "threshold": active["threshold"], "device_score": active["device_score"],
            "trigger_source": active["trigger_source"], "ts": active["ts"],
        }
