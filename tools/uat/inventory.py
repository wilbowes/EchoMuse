"""
Every dashboard control, read from controller/static/dashboard.jsx.

Release UAT (Wil, 2026-09-26) checks that every control does what it says.
The list is extracted from the source rather than written by hand, so a
control added later is either checked or shows up as uncovered; nothing can
be quietly left out.

A control is a <Toggle>, <Slider>, <Select> or <NumberField> element. Its
label is the `label` prop (a string, or the first string inside a JSX label),
and its config key is the first set('<key>' / setConf('<key>' / onChange
naming a key inside the element.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2] / "controller/static/dashboard.jsx"
KINDS = ("Toggle", "Slider", "Select", "NumberField")


def _element(src: str, start: int) -> str:
    """The source of one JSX element from its '<', to its closing '/>' or '>'."""
    depth, i = 0, start
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0 and src.startswith("/>", i):
            return src[start:i + 2]
        elif depth == 0 and c == ">" and i > start:
            return src[start:i + 1]
        i += 1
    return src[start:]


def _enclosing_function(src: str, pos: int) -> str:
    m = None
    for m in re.finditer(r"^function (\w+)", src[:pos], re.M):
        pass
    return m.group(1) if m else "?"


def controls(src: str | None = None) -> list[dict]:
    src = src if src is not None else DASHBOARD.read_text()
    out = []
    for m in re.finditer(r"<(%s)\b" % "|".join(KINDS), src):
        el = _element(src, m.start())
        label = re.search(r'label="([^"]+)"', el) or re.search(r"label=\{[^}]*?'([^']+)'", el)
        key = re.search(r"""\bset(?:Conf|Sys)?\(\s*['"](\w+)['"]""", el)
        out.append({
            "kind": m.group(1),
            "label": label.group(1) if label else None,
            "key": key.group(1) if key else None,
            "line": src.count("\n", 0, m.start()) + 1,
            "in": _enclosing_function(src, m.start()),
        })
    return out


def actions(src: str | None = None) -> list[dict]:
    """
    Every state-changing API call the dashboard makes (buttons and widgets
    the control list cannot see: approve, pair, OTA, EQ bands, uploads...),
    as method + path with ids collapsed. Each is a row in the report, so an
    action nobody checked is visible as unchecked.
    """
    src = src if src is not None else DASHBOARD.read_text()
    seen = {}
    pat = re.compile(r"API\.(post|patch|put|del|delete)\(\s*([`'])([^`']*)\2")
    for m in pat.finditer(src):
        path = re.sub(r"\$\{[^}]*\}", "{id}", m.group(3))
        method = {"del": "DELETE"}.get(m.group(1), m.group(1).upper())
        seen.setdefault((method, path), src.count("\n", 0, m.start()) + 1)
    return [{"method": k[0], "path": k[1], "line": v} for k, v in sorted(seen.items())]


if __name__ == "__main__":
    if "--json" in sys.argv:
        json.dump({"controls": controls(), "actions": actions()}, sys.stdout, indent=1)
    else:
        for r in controls():
            print(f"{r['line']:>6} {r['in']:<24} {r['kind']:<11} {str(r['key']):<22} {r['label']}")
        for a in actions():
            print(f"{a['line']:>6} {'action':<24} {a['method']:<11} {a['path']}")
