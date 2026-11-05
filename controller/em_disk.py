"""Disk-space policy for the controller's data volume (#995).

The free space beside the database is shared — in the add-on `/data` is Home
Assistant's own partition — so it can collapse under the controller and its
sizes are not the cause. The thresholds are module constants, and every
predicate takes the database path so a test can point it at a nearly full
tmpfs.

An unreadable volume reads as unlimited: it is not evidence of a full disk,
and refusing on it would stop writes on any mount whose free space cannot be
measured.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

DISK_WARN_MB = 500
DISK_REFUSE_MB = 100

OK = "ok"
WARN = "warn"
REFUSE = "refuse"


def data_dir(db_path: str | None = None) -> Path:
    """The directory that holds the database and its sibling files."""
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent


def free_mb(db_path: str | None = None) -> float:
    """Free space beside the database, in MB. Unlimited when unreadable."""
    try:
        return shutil.disk_usage(str(data_dir(db_path))).free / 1048576.0
    except OSError:
        return float("inf")


def classify(free: float) -> str:
    """The state of a free-space reading."""
    if free < DISK_REFUSE_MB:
        return REFUSE
    if free < DISK_WARN_MB:
        return WARN
    return OK


def state(db_path: str | None = None) -> str:
    return classify(free_mb(db_path))


def optional_writes_allowed(db_path: str | None = None) -> bool:
    """Utterances, wake clips, model uploads and the firmware cache."""
    return state(db_path) == OK


def new_writes_allowed(db_path: str | None = None) -> bool:
    """Anything that creates a file. The database is never gated by this."""
    return state(db_path) != REFUSE
