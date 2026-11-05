"""
em_firmware.py — on-disk cache of downloaded device firmware.

`_fetch_binary` opened a fresh HTTP session and pulled the whole release asset
on every call, from both OTA paths. Updating a fleet therefore downloaded the
same ~10MB once per device, and the provisioning wizard downloaded it again for
every device it set up. On a slow or metered connection that is the slowest
part of a fleet update, and it is entirely avoidable: the artefact is immutable
— a published release tag never changes what it points at.

**md5 is the definition of a cache hit, not the file existing.** A truncated
download is the realistic failure here (an interrupted transfer leaves a file
of plausible size), and the OTA's own device-side verification cannot catch it:
that check confirms the device received what the controller SENT, so a corrupt
cache entry verifies perfectly all the way onto the device — and a corrupt
binary and a genuinely broken one produce the same observable, three fast exits
and a rollback. So the digest is written beside the payload and re-checked on
every read, which costs ~30ms against a 10MB file.

**A cache failure is never an update failure.** Every write path degrades to
"return the bytes anyway": the cache is an optimisation, and a full or
read-only disk must not stop a device being updated. That is the opposite of
the rule for the DB backup before a migration, and deliberately so — there,
refusing is the safe action; here, refusing costs the user the thing they asked
for and protects nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import em_disk

log = logging.getLogger("echomuse.firmware")

FIRMWARE_SUBDIR = "firmware"

# How many release binaries to keep. Two is enough for the case that matters —
# the release being rolled out and the one being rolled back to — and each is
# ~10MB. Pruning is by mtime, which the read path touches, so "recently used"
# needs no bookkeeping that could be lost across a restart.
KEEP_RELEASES = 2

# A tag off the GitHub API, used as a path component. Tags are `v2.11.0` in
# practice; this is validated rather than trusted because it arrives over the
# network.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")


def firmware_dir(db_path: str | None = None) -> Path:
    """
    `firmware/` beside the SQLite DB — the same convention as recordings/,
    oww_models/ and tls/, so one persisted volume carries everything.
    """
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return (Path(db_path).resolve().parent / FIRMWARE_SUBDIR)


def safe_version(version: str) -> str | None:
    """The release tag as a path component, or None if it isn't one."""
    if version and _TAG_RE.fullmatch(version):
        return version
    return None


def cache_path(version: str, db_path: str | None = None) -> Path | None:
    """Where this version's binary lives, or None if the tag is unusable."""
    safe = safe_version(version)
    if safe is None:
        return None
    return firmware_dir(db_path) / f"server-{safe}"


def _sidecar(path: Path, ext: str) -> Path:
    """
    A companion file next to `path`.

    NOT Path.with_suffix: a release tag contains dots, so pathlib reads
    "server-v2.11.0" as stem "server-v2.11" with suffix ".0" and
    with_suffix(".md5") silently produces "server-v2.11.md5". The digest then
    never lines up with the payload it belongs to — every read is a miss, the
    cache does nothing, and nothing says so.
    """
    return path.parent / (path.name + ext)


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read(version: str, db_path: str | None = None) -> bytes | None:
    """
    The cached binary for `version`, or None.

    None covers every reason equally — not cached, no digest recorded, digest
    mismatch, unreadable — because the caller does the same thing in all of
    them: download it. A mismatch is logged and the entry removed, so a
    corrupt file cannot be re-read forever.
    """
    path = cache_path(version, db_path)
    if path is None:
        return None
    digest_path = _sidecar(path, ".md5")
    try:
        if not (path.is_file() and digest_path.is_file()):
            return None
        want = digest_path.read_text(encoding="utf-8").strip()
        data = path.read_bytes()
    except OSError as e:
        log.warning(f"[firmware] cache read failed for {version}: {e}")
        return None

    if not want or md5_bytes(data) != want:
        log.warning(f"[firmware] cached {version} failed md5 — discarding")
        for p in (path, digest_path):
            try:
                p.unlink()
            except OSError:
                pass
        return None

    # mtime is the recency signal prune() reads, so a hit counts as use.
    try:
        os.utime(path, None)
    except OSError:
        pass
    log.info(f"[firmware] cache hit {version} ({len(data):,} bytes)")
    return data


def write(version: str, data: bytes, db_path: str | None = None) -> bool:
    """
    Cache `data` for `version`. False if it could not be cached — which is
    never a reason for the caller to fail.

    Payload lands at `.part` and is renamed only after its digest is written,
    so an interrupted write cannot leave a file that read() would trust.
    """
    path = cache_path(version, db_path)
    if path is None or not data:
        return False
    if not em_disk.optional_writes_allowed(db_path):
        log.warning(f"[firmware] could not cache {version}: free space is "
                    f"{em_disk.free_mb(db_path):.0f} MB, below the "
                    f"{em_disk.DISK_WARN_MB} MB mark for optional writes")
        return False
    part = _sidecar(path, ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(data)
        _sidecar(path, ".md5").write_text(md5_bytes(data), encoding="utf-8")
        part.replace(path)
    except OSError as e:
        log.warning(f"[firmware] could not cache {version}: {e}")
        try:
            part.unlink()
        except OSError:
            pass
        return False
    log.info(f"[firmware] cached {version} ({len(data):,} bytes)")
    prune(db_path)
    return True


def prune(db_path: str | None = None, keep: int = KEEP_RELEASES) -> list[str]:
    """
    Drop all but the `keep` most recently used binaries. Returns what went.

    Only files this module wrote are ever considered — the directory sits in
    the user's data volume beside their database, and a cache is not a licence
    to delete things it does not recognise.
    """
    d = firmware_dir(db_path)
    try:
        entries = [p for p in d.iterdir()
                   if p.is_file() and p.name.startswith("server-")
                   and not p.name.endswith((".md5", ".part"))]
    except OSError:
        return []

    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for p in entries[keep:]:
        try:
            p.unlink()
            _sidecar(p, ".md5").unlink(missing_ok=True)
            removed.append(p.name)
        except OSError as e:
            log.warning(f"[firmware] could not prune {p.name}: {e}")
    if removed:
        log.info(f"[firmware] pruned {len(removed)} cached binary(s): "
                 f"{', '.join(removed)}")
    return removed
