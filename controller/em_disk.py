"""Disk-space guard for optional controller writes (#995).

Thresholds are module constants so the edge behaviour is testable.
"""
import shutil
from pathlib import Path

DISK_WARN_MB = 500
DISK_REFUSE_MB = 100


def free_mb(path=".") -> float:
    try:
        return shutil.disk_usage(str(path)).free / (1024 * 1024)
    except OSError:
        return float("inf")


def disk_state(path=".") -> str:
    free = free_mb(path)
    if free < DISK_REFUSE_MB:
        return "refuse"
    if free < DISK_WARN_MB:
        return "warn"
    return "ok"


def optional_writes_allowed(path=".") -> bool:
    """Below DISK_WARN_MB, skip optional writes (utterances, wake clips,
    model uploads, firmware cache)."""
    return disk_state(path) != "warn" and disk_state(path) != "refuse"


def new_writes_allowed(path=".") -> bool:
    """Below DISK_REFUSE_MB, refuse anything new except small DB writes."""
    return disk_state(path) != "refuse"
