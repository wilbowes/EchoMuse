"""
The controller's own health: how long it has been up, its CPU over 1m/5m/1h,
its memory and data sizes, and the in-memory ring of its recent log lines
that support bundles carry. em_controller installs the ring at startup and
feeds the CPU sampler from its 1s ticker.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import em_recordings
import em_state
import em_support

# Disk space thresholds (MB)
FREE_SPACE_LOW_MB = 500
FREE_SPACE_CRITICAL_MB = 100

# Internal tracking of free space and warnings
_current_free_space_mb: float | None = None
_prev_free_space_mb: float | None = None
_low_space_logged = False
_critical_space_logged = False


def _update_free_space_tracking(free_mb: float) -> None:
    """
    Update internal free space tracking and log warnings when crossing thresholds.
    """
    global _current_free_space_mb, _prev_free_space_mb, _low_space_logged, _critical_space_logged
    _prev_free_space_mb = _current_free_space_mb
    _current_free_space_mb = free_mb

    logger = logging.getLogger(__name__)
    if _prev_free_space_mb is not None:
        # Low space threshold (500 MB)
        if _prev_free_space_mb >= FREE_SPACE_LOW_MB and free_mb < FREE_SPACE_LOW_MB:
            logger.warning("Free disk space dropped below %d MB (%d MB)", FREE_SPACE_LOW_MB, free_mb)
            _low_space_logged = True
        elif _prev_free_space_mb < FREE_SPACE_LOW_MB and free_mb >= FREE_SPACE_LOW_MB:
            logger.info("Free disk space recovered above %d MB (%d MB)", FREE_SPACE_LOW_MB, free_mb)
            _low_space_logged = False

        # Critical space threshold (100 MB)
        if _prev_free_space_mb >= FREE_SPACE_CRITICAL_MB and free_mb < FREE_SPACE_CRITICAL_MB:
            logger.warning("Free disk space dropped below %d MB (%d MB)", FREE_SPACE_CRITICAL_MB, free_mb)
            _critical_space_logged = True
        elif _prev_free_space_mb < FREE_SPACE_CRITICAL_MB and free_mb >= FREE_SPACE_CRITICAL_MB:
            logger.info("Free disk space recovered above %d MB (%d MB)", FREE_SPACE_CRITICAL_MB, free_mb)
            _critical_space_logged = False


# Import time, which is startup: em_controller imports this module before it
# serves anything. Close enough to process start for "how long has it been up",
# and it needs no procfs.
PROCESS_START = time.time()


# CPU over 1m/5m/1h, fed by the event-loop lag monitor (see sample_cpu). The
# ring is bounded by its longest window; nothing here runs per request.
cpu_history = em_support.CpuHistory()
CPU_SAMPLE_INTERVAL_S = em_support.CpuHistory.INTERVAL_S


# The controller's own recent log, kept in memory for support bundles. Every
# line that would have explained #62 goes to stdout, and stdout was not in
# the bundle at all.
log_ring = em_support.LogRing()


def install_log_ring(fmt: str) -> None:
    """
    Attach the in-memory log ring to the root logger.

    Called from em_controller once logging is configured, with the same
    format string, so a bundle reads exactly like the console does.
    """
    log_ring.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(log_ring)


def sample_cpu() -> None:
    """
    Take one CPU sample. Called from em_controller's existing 1s ticker, not
    on a task of its own — the cost is one os.times() every INTERVAL_S.
    """
    cpu_history.add(time.monotonic(), sum(os.times()[:2]))


def _read_first_line(path: str) -> str:
    with open(path) as fh:
        return fh.readline().strip()


def _proc_meminfo() -> dict[str, float]:
    """/proc/meminfo in MB. Empty on anything without procfs."""
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out[parts[0].rstrip(":")] = int(parts[1]) / 1024.0
    except OSError:
        pass
    return out


def controller_stats() -> dict:
    """
    The controller's own CPU, memory and storage.

    Read from /proc and the filesystem rather than psutil, which is not a
    dependency and is not worth becoming one for a handful of files. Every
    lookup degrades to an absent key: a bundle missing a stat is a nuisance,
    a bundle that 500s when the host is unusual is the failure that matters,
    since the bundle is what someone reaches for when things are wrong.

    Paths are deliberately never reported — on a bare-metal install the data
    directory carries the account name, which is the leak this same change
    fixes in the log tail.
    """
    stats: dict[str, Any] = {"loop_lag_peak_ms": round(em_state.loop_lag_peak_ms, 1)}

    stats["python"] = platform.python_version()
    stats["platform"] = f"{platform.system()} {platform.machine()}"
    stats["cpu_count"] = os.cpu_count()
    stats["container"] = os.path.exists("/.dockerenv")

    try:
        load = os.getloadavg()
        stats["load_1"], stats["load_5"], stats["load_15"] = [round(x, 2) for x in load]
    except OSError:
        pass

    mem = _proc_meminfo()
    if "MemTotal" in mem:
        stats["mem_total_mb"] = round(mem["MemTotal"], 1)
    if "MemAvailable" in mem:
        stats["mem_available_mb"] = round(mem["MemAvailable"], 1)

    # cgroup v2 then v1: in a container MemTotal is the HOST's memory, which
    # reads as plenty of headroom while the container is being OOM-killed.
    for limit_path in ("/sys/fs/cgroup/memory.max",
                       "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = _read_first_line(limit_path)
            if raw and raw != "max":
                val = int(raw) / 1048576.0
                # An unset v1 limit is a sentinel near 2^63, not a real cap.
                if val < 1024 * 1024:
                    stats["mem_limit_mb"] = round(val, 1)
            break
        except (OSError, ValueError):
            continue

    try:
        with open("/proc/self/status") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    stats["rss_mb"] = round(int(ln.split()[1]) / 1024.0, 1)
                    break
    except OSError:
        pass

    # Process CPU as a share of one core, averaged over the process lifetime.
    # A lifetime average, not an instant sample: a support bundle is taken
    # once, and a single 100ms sample of an asyncio process is noise.
    try:
        uptime = time.time() - PROCESS_START
        stats["uptime_s"] = int(uptime)
        cpu_time = sum(os.times()[:2])
        # Below a few seconds the ratio is startup cost divided by almost
        # nothing — it reads as 1147% and looks like a controller on fire.
        # Omitted rather than reported wrong; a bundle taken in the first
        # seconds of a run has no CPU history worth having anyway.
        # Percent of ONE core, as `top` reports it — over 100 means more than
        # a core's worth. The windowed figures are what point anywhere: a
        # lifetime average cannot tell a controller busy right now from one
        # that was busy for an hour this morning.
        stats.update(cpu_history.windows(time.monotonic(), cpu_time))
        if uptime >= 5.0:
            stats["cpu_pct_life"] = round(100.0 * cpu_time / uptime, 1)
    except Exception:
        pass

    # Same resolution as em_recordings, via its helper — the DB path lives in
    # one place and this must not become a second definition of it.
    db_path = Path(os.environ.get("DB_PATH", "echomuse.db")).resolve()
    try:
        usage = shutil.disk_usage(db_path.parent)
        stats["data_used_mb"] = round(usage.used / 1048576.0, 1)
        stats["data_free_mb"] = round(usage.free / 1048576.0, 1)
        # Update free space tracking and log warnings
        _update_free_space_tracking(stats["data_free_mb"])
    except OSError:
        pass
    try:
        # The database is the thing that grows without anyone watching it.
        stats["db_mb"] = round(db_path.stat().st_size / 1048576.0, 1)
    except OSError:
        pass
    try:
        rec_dir = em_recordings.recordings_dir()
        total = sum(f.stat().st_size for f in rec_dir.iterdir() if f.is_file())
        stats["recordings_mb"] = round(total / 1048576.0, 1)
    except OSError:
        pass

    return stats


def get_free_space_mb() -> float | None:
    """Return the current free disk space in MB, or None if unknown."""
    return _current_free_space_mb


def is_low_space() -> bool:
    """Return True if free disk space is below the low threshold (500 MB)."""
    if _current_free_space_mb is None:
        return False
    return _current_free_space_mb < FREE_SPACE_LOW_MB


def is_critical_space() -> bool:
    """Return True if free disk space is below the critical threshold (100 MB)."""
    if _current_free_space_mb is None:
        return False
    return _current_free_space_mb < FREE_SPACE_CRITICAL_MB


def should_allow_write(write_type: str) -> tuple[bool, str]:
    """
    Determine whether a write operation of the given type is allowed based on free space.

    Returns (allowed, reason). If allowed is True, reason is empty.
    """
    # Essential writes that are always allowed
    if write_type in ("settings", "turns", "logs", "database"):
        return True, ""

    # Optional writes that are skipped when low space
    if write_type in ("utterance", "wake_clip", "model_upload", "firmware_cache"):
        if is_low_space():
            return False, f"Free disk space below {FREE_SPACE_LOW_MB} MB"
        if is_critical_space():
            return False, f"Free disk space below {FREE_SPACE_CRITICAL_MB} MB"

    # Default deny for unknown types
    return False, f"Unknown write type: {write_type}"


def release_freed_memory() -> None:
    """Ask glibc to return freed heap to the OS (malloc_trim). Freed memory
    otherwise stays in the process: three wake models released after a
    switch to private listening left ~40 MB that this gives back (measured
    in the controller image, 2026-10-16). Blocking for a few ms, so call it
    from a worker thread. A no-op where libc has no malloc_trim (musl,
    macOS)."""
    name = ctypes.util.find_library("c")
    try:
        trim = getattr(ctypes.CDLL(name), "malloc_trim", None) if name else None
        if trim is not None:
            trim(0)
    except OSError:
        pass
