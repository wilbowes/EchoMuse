"""tools/coverage_floor.py: floors are checked per module and only ever rise."""

import json
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import coverage_floor  # noqa: E402


def _report(tmp_path, **percent):
    files = {name.replace("_py", ".py"): {"summary": {"percent_covered": p}}
             for name, p in percent.items()}
    files["tests/test_x.py"] = {"summary": {"percent_covered": 1.0}}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"files": files}))
    return path


def test_a_module_under_its_floor_or_without_one_fails(tmp_path, monkeypatch):
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps({"em_a.py": 50.0}))
    monkeypatch.setattr(coverage_floor, "FLOORS", floors)
    problems = coverage_floor.check(_report(tmp_path, em_a_py=49.9, em_b_py=80.0))
    assert len(problems) == 2
    assert "under its floor" in problems[0] and "no floor" in problems[1]
    assert coverage_floor.check(_report(tmp_path, em_a_py=50.0)) == []


def test_raising_never_lowers_a_floor(tmp_path, monkeypatch):
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps({"em_a.py": 60.0}))
    monkeypatch.setattr(coverage_floor, "FLOORS", floors)
    got = coverage_floor.raise_floors(_report(tmp_path, em_a_py=40.0, em_b_py=72.6))
    assert got == {"em_a.py": 60.0, "em_b.py": 71.0}


def test_every_module_has_a_floor():
    floors = json.loads(coverage_floor.FLOORS.read_text())
    modules = {p.name for p in CONTROLLER.glob("em_*.py")}
    assert modules == set(floors), (
        "missing: %s; stale: %s — run the suite with --cov-report=json and "
        "`python tools/coverage_floor.py raise coverage.json`"
        % (sorted(modules - set(floors)), sorted(set(floors) - modules)))
