"""
Modules and functions may shrink and may not grow: see tools/size_ratchet.py.

A failure here means one of two things. Something grew: split it, or raise
its number in tests/size_baseline.json in the same change so a reviewer sees
the decision. Or something shrank by more than the slack: run
`python controller/tools/size_ratchet.py --update` so the gain is kept.
"""

import json
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import size_ratchet  # noqa: E402


def _baseline() -> dict:
    return json.loads(size_ratchet.BASELINE.read_text())


def test_the_measurement_finds_the_known_giants():
    # Guards the guard: a measure that finds nothing passes everything.
    now = size_ratchet.measure()
    assert "em_controller.py" in now["files"]
    assert "em_controller.handle_control" in now["functions"]


def test_nothing_grew():
    base, now = _baseline(), size_ratchet.measure()
    grew = []
    for kind, cap in (("files", size_ratchet.FILE_CAP), ("functions", size_ratchet.FN_CAP)):
        for name, size in now[kind].items():
            limit = base[kind].get(name)
            if limit is None:
                grew.append(f"{name} is {size} lines, over the {cap}-line cap")
            elif size > limit:
                grew.append(f"{name} grew from {limit} to {size} lines")
    assert not grew, "; ".join(grew)


def test_the_baseline_keeps_what_was_gained():
    base, now = _baseline(), size_ratchet.measure()
    stale = []
    for kind in ("files", "functions"):
        for name, limit in base[kind].items():
            size = now[kind].get(name)
            if size is None:
                stale.append(f"{name} is no longer over the cap")
            elif limit - size > size_ratchet.SLACK:
                stale.append(f"{name} shrank from {limit} to {size} lines")
    assert not stale, ("; ".join(stale) + " — run "
                       "`python controller/tools/size_ratchet.py --update`")
