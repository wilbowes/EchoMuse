"""
Every forge module the image runs is copied into it.

The Dockerfile names its Python files one by one, and a module added without
a matching COPY builds fine and dies at import in the container: piper_voices
(#348), then forge_progress (#547, found in #612 — the web UI would not start
from any local build after it). Follows the imports of the shipped modules,
so a new local module is caught the moment anything shipped imports it.
"""

import ast
import re
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]


def _shipped() -> set[str]:
    text = (FORGE / "Dockerfile").read_text()
    names = set()
    for line in re.findall(r"^COPY (.+) /opt/forge/\s*$", text, re.M):
        names.update(n for n in line.split() if n.endswith(".py"))
    return names


def _local_imports(path: Path) -> set[str]:
    local = {p.stem for p in FORGE.glob("*.py")}
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return {f"{m}.py" for m in found & local}


def test_the_entry_point_is_shipped():
    assert "forge.py" in _shipped()


def test_every_module_a_shipped_module_imports_is_shipped():
    shipped = _shipped()
    missing = {}
    for name in sorted(shipped):
        gaps = _local_imports(FORGE / name) - shipped
        if gaps:
            missing[name] = sorted(gaps)
    assert not missing, f"imported but not copied into the image: {missing}"
