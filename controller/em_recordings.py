"""
em_recordings.py — utterance audio capture for the Activity panel
==================================================================

Saves the mic audio of recent voice turns as WAV files so you can *listen*
to what the array actually captured, rather than inferring mic quality from
an STT transcript and a wake score. Asked for by users who wanted to judge
capture quality before spending an evening tuning micGainDb/AEC/NS.

Storage mirrors `em_oww_models`: files live in `recordings/` beside the
SQLite DB, so they sit inside the persisted Docker volume and survive image
upgrades. Retention is a hard per-device file count (KEEP_PER_DEVICE) —
utterances are the one artefact here that contains raw speech, so "a
bounded handful, then gone" is the point, not an optimisation. Pruning is
by turn id parsed out of the filename rather than mtime: ids are monotonic
rowids, so the order is exact even if the volume is restored from a backup
that flattened timestamps.

The audio written is exactly what the controller streamed to Home Assistant
for recognition — tapped BELOW noise suppression, so on a device with
`nsAsr` on the file is the denoised stream, not the raw mic. That is the
point: a recording that isn't what STT heard can tell you the room was
noisy but never why a transcript came back wrong. 16kHz mono S16_LE,
matching the ESPHome satellite wire format.

Pure path/filesystem logic (no aiohttp, no db import) so it can be unit
tested; em_esphome writes through it and em_api serves from it.
"""

from __future__ import annotations

import io
import logging
import os
import re
import wave
from pathlib import Path

import em_disk

log = logging.getLogger("echomuse.recordings")

RECORDINGS_SUBDIR = "recordings"

# How many utterances to keep per device. Ten is what the feature was asked
# for and is deliberately small — see the module docstring.
KEEP_PER_DEVICE = 10

# Wire format of the ASR-bound mic stream (em_esphome._stream_mic_audio).
SAMPLE_RATE  = 16000
SAMPLE_WIDTH = 2
CHANNELS     = 1

# Hard cap on one recording. A turn is endpointed by HA's VAD long before
# this, but the mic stream has no structural length limit (a stuck-open RMS
# gate in a noisy room is exactly the case that used to shovel audio at HA
# indefinitely), and an unbounded bytearray on the turn path is not
# something to leave to chance. 30s at 16kHz mono = 960 kB.
MAX_UTTERANCE_SECONDS = 30
MAX_UTTERANCE_BYTES   = MAX_UTTERANCE_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH

# device_id is ro.serialno (hex) and turn_id is a rowid, but this is the
# name that comes back off disk and out of the DB, so it gets validated
# like any other path component rather than trusted.
_NAME_RE = re.compile(r"^(?P<device>[A-Za-z0-9_.-]{1,64})_(?P<turn>\d{1,19})\.wav$")


def recordings_dir(db_path: str | None = None) -> Path:
    """
    Resolve the recordings directory: `recordings/` beside the SQLite DB
    (DB_PATH env, same default as em_controller). Absolute, so it stays
    valid regardless of the process cwd.
    """
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return (Path(db_path).resolve().parent / RECORDINGS_SUBDIR)


def safe_device_id(device_id: str) -> str | None:
    """The device id as a path component, or None if it isn't one."""
    if device_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device_id):
        return device_id
    return None


def filename(device_id: str, turn_id: int) -> str | None:
    """Canonical filename for a turn's utterance, or None if unnameable."""
    safe = safe_device_id(device_id)
    if safe is None or turn_id is None or int(turn_id) < 0:
        return None
    return f"{safe}_{int(turn_id)}.wav"


def parse_filename(name: str) -> tuple[str, int] | None:
    """(device_id, turn_id) for a recording filename, or None if malformed."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return m.group("device"), int(m.group("turn"))


def encode_wav(pcm: bytes) -> bytes:
    """PCM frames → a WAV container, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm_len: int) -> int:
    """Playing time of `pcm_len` bytes of the wire format, in ms."""
    return int(pcm_len / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS) * 1000)


def save(device_id: str, turn_id: int, pcm: bytes,
         db_path: str | None = None, keep: int = KEEP_PER_DEVICE) -> str | None:
    """
    Write one turn's utterance and prune the device back to `keep` files.

    Returns the filename to store on the turn row, or None if nothing was
    written. Blocking (runs in an executor at the call site).
    """
    name = filename(device_id, turn_id)
    if name is None or not pcm:
        return None
    if not em_disk.optional_writes_allowed(db_path):
        log.warning(f"[recordings] Not saving {name}: free space is "
                    f"{em_disk.free_mb(db_path):.0f} MB, below the "
                    f"{em_disk.DISK_WARN_MB} MB mark for optional writes")
        return None
    directory = recordings_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # Write-then-rename: a partially written WAV that the API then serves
    # is worse than no recording at all.
    tmp = path.with_suffix(".wav.part")
    tmp.write_bytes(encode_wav(pcm))
    tmp.replace(path)
    prune(device_id, db_path=db_path, keep=keep)
    return name


def list_for(device_id: str, db_path: str | None = None) -> list[str]:
    """This device's recording filenames, newest turn first."""
    safe = safe_device_id(device_id)
    if safe is None:
        return []
    directory = recordings_dir(db_path)
    if not directory.is_dir():
        return []
    entries: list[tuple[int, str]] = []
    for child in directory.iterdir():
        parsed = parse_filename(child.name)
        if parsed and parsed[0] == safe:
            entries.append((parsed[1], child.name))
    entries.sort(reverse=True)
    return [name for _, name in entries]


def prune(device_id: str, db_path: str | None = None,
          keep: int = KEEP_PER_DEVICE) -> list[str]:
    """
    Delete all but the `keep` newest recordings for a device. Returns the
    filenames removed. Never raises — a failed unlink costs disk, not a turn.
    """
    directory = recordings_dir(db_path)
    removed: list[str] = []
    for name in list_for(device_id, db_path)[max(keep, 0):]:
        try:
            (directory / name).unlink()
            removed.append(name)
        except OSError as e:
            log.warning(f"[recordings] Could not prune {name}: {e}")
    return removed


def resolve(device_id: str, name: str, db_path: str | None = None) -> Path | None:
    """
    Path of an existing recording, or None. The filename must parse AND
    belong to `device_id` — the API takes both from the URL, and without the
    ownership check one device's turn id would serve another's audio.
    """
    parsed = parse_filename(name)
    safe   = safe_device_id(device_id)
    if parsed is None or safe is None or parsed[0] != safe:
        return None
    path = recordings_dir(db_path) / name
    return path if path.is_file() else None


def delete_device(device_id: str, db_path: str | None = None) -> int:
    """Remove every recording for a device. Returns the count deleted."""
    return len(prune(device_id, db_path=db_path, keep=0))
