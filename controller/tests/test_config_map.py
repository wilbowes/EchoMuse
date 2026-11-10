"""
docs/config-keys.md must match what tools/config_map.py builds from the code.

A failure means a config key was added, moved or dropped somewhere (the
device's ConfigMessage, a dashboard section, the defaults, a module that reads
it). Regenerate and commit the result with the change:

    python controller/tools/config_map.py
"""

import sys
from pathlib import Path

import _sources

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import config_map  # noqa: E402


def test_the_doc_is_current():
    assert config_map.OUT.read_text() == config_map.render(), (
        "docs/config-keys.md is stale: run python controller/tools/config_map.py")


def test_it_reads_every_controller_module():
    # The same set a source guard sees, so a new module is in the map the day
    # it is added.
    scanned = {p.stem for p in CONTROLLER.glob("em_*.py")}
    assert scanned == {p.stem for p in _sources.MODULES}


# Guards the guard: a parser that finds nothing renders a table that is
# trivially current.

def test_config_go_was_parsed():
    fields = config_map.go_fields()
    assert "owwThreshold" in fields and "listeningAnim" in fields


def test_each_column_finds_something():
    doc = config_map.render()
    assert "| `owwThreshold` | Wake word | `0.5` | a number | yes |" in doc
    # Read only by the controller, so the Echo ignores it.
    assert "| `wakeArbitrationMs` | Wake word |" in doc
    assert "| no | em_controller" in doc
    assert "| via `listeningAnim` | em_scenes |" in doc
    assert "(fleet only)" in doc and "| state |" in doc


def test_prose_does_not_count_as_a_reader():
    # em_config_sections' docstring names use_global_config; its code does not.
    lits = config_map.literals(CONTROLLER / "em_config_sections.py")
    assert "startupVolume" in lits
    assert not any("use_global_config" in s for s in lits)
