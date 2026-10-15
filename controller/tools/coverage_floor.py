"""
Per-module test coverage that may only rise.

CI runs the suite with coverage and calls `check` on the result: every
controller em_*.py must be covered at least as well as its floor in
tests/coverage_floor.json. A floor is a module's figure when it was last set,
less one point for the run-to-run noise of timing-dependent branches, and is
only ever raised. A module with no floor fails until one is set, so a new
module's coverage is a decision someone made.

    python -m pytest tests/ --cov --cov-report=json:coverage.json
    python tools/coverage_floor.py check coverage.json
    python tools/coverage_floor.py raise coverage.json   # after adding tests

Measured in CI's environment only: coverage depends on which optional
dependencies are installed, and a laptop is not CI.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
FLOORS = CONTROLLER / "tests" / "coverage_floor.json"
MARGIN = 1.0


def covered(report: Path) -> dict[str, float]:
    files = json.loads(report.read_text())["files"]
    return {name: data["summary"]["percent_covered"]
            for name, data in files.items()
            if name.startswith("em_") and "/" not in name}


def floor_for(percent: float) -> float:
    return max(0.0, math.floor(percent) - MARGIN)


def check(report: Path) -> list[str]:
    floors = json.loads(FLOORS.read_text())
    problems = []
    for name, percent in sorted(covered(report).items()):
        floor = floors.get(name)
        if floor is None:
            problems.append(f"{name}: {percent:.1f}% and no floor — run "
                            f"`python tools/coverage_floor.py raise` to set one")
        elif percent < floor:
            problems.append(f"{name}: {percent:.1f}% is under its floor of {floor:.0f}%")
    return problems


def raise_floors(report: Path) -> dict[str, float]:
    floors = json.loads(FLOORS.read_text()) if FLOORS.exists() else {}
    for name, percent in covered(report).items():
        floors[name] = max(floors.get(name, 0.0), floor_for(percent))
    FLOORS.write_text(json.dumps(floors, indent=1, sort_keys=True) + "\n")
    return floors


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "raise"):
        print(__doc__)
        return 2
    report = Path(sys.argv[2])
    if sys.argv[1] == "raise":
        raise_floors(report)
        print(f"wrote {FLOORS}")
        return 0
    problems = check(report)
    for line in problems:
        print(line)
    if not problems:
        print(f"coverage floors held for {len(covered(report))} modules")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
