"""
The dashboard's type sizes, font families, corner radii and spacing come from the
scale tokens in dashboard.html (#820), the way its colours come from the
colour tokens (test_design_tokens.py).

A value typed at the call site cannot be changed from one place: before the
scale existed, a type-size change meant 249 edits over 16 values. This is a
ratchet in the same shape as the colour one: literals are counted per
component, the count can only fall, and a new component starts at zero.

What is counted, in the JSX with comments stripped:
  fontSize: <number>          -> var(--fs-*)
  borderRadius: <number|'Npx ...'>  -> var(--radius-*)
  'DM Mono' / 'DM Sans' font stacks  -> var(--font-mono) / var(--font-sans)
  gap / padding / margin: <non-zero number | 'Npx ...'>  -> var(--space-N)

Exempt, with the reason:
  Shell              xterm.js options are numbers and font names handed to a
                     canvas renderer, not CSS; a var() there is not resolved.
  LedRing, DeviceDiagram, DeviceDiagramMini
                     drawings of the Echo, whose sizes are geometry.

Not counted: a radius of half an element's height is the shape of that
element (the Toggle's 10 on a 20px track), so a percentage or a pill radius
is left to the component. Add it to the budget rather than to the scale.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from test_design_tokens import HTML, JSX, _owner_map, _strip_comments

BUDGET = Path(__file__).resolve().parent / "design_scale_budget.json"
EXEMPT = {"Shell", "LedRing", "DeviceDiagram", "DeviceDiagramMini"}

_LITERALS = re.compile(
    r"fontSize:\s*\d"
    r"|borderRadius:\s*(?:\d|'\d)"
    r"|\b(?:gap|rowGap|columnGap|padding\w*|margin\w*):\s*(?:[1-9]|'[^']*[1-9]\d*px)"
    r"|'DM (?:Mono|Sans)',\s?(?:monospace|sans-serif)")
_SCALE_BLOCK = ":root {\n      --font-sans:"
_VAR_DEF = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)
_SCALE_USE = re.compile(r"var\((--(?:fs|radius|font|space)-[a-z0-9-]+)\)")


def scan() -> dict[str, int]:
    text = JSX.read_text()
    owner = _owner_map(text)
    tally: Counter = Counter()
    for i, line in enumerate(_strip_comments(text).split("\n"), 1):
        who = owner(i)
        if who in EXEMPT:
            continue
        n = len(_LITERALS.findall(line))
        if n:
            tally[who] += n
    return dict(sorted(tally.items()))


def scale_tokens() -> set[str]:
    html = HTML.read_text()
    start = html.index(_SCALE_BLOCK)
    return set(_VAR_DEF.findall(html[start:html.index("\n    }", start)]))


def test_the_scale_block_is_found():
    assert {"--fs-sm", "--radius-md", "--font-mono"} <= scale_tokens()


def test_every_scale_token_used_is_defined():
    """An undefined font-size var() falls back to the inherited size and an
    undefined radius to square corners: wrong, and silent."""
    html = HTML.read_text()
    used = set(_SCALE_USE.findall(JSX.read_text())) | set(_SCALE_USE.findall(html))
    missing = used - scale_tokens()
    assert not missing, f"undefined scale tokens: {sorted(missing)}"


def test_no_new_literal_sizes():
    current, budget = scan(), json.loads(BUDGET.read_text())
    over = [f"  {name}: {budget.get(name, 0)} -> {n}"
            for name, n in current.items() if n > budget.get(name, 0)]
    assert not over, (
        "Literal type sizes, radii or font stacks increased; use the tokens "
        "in dashboard.html's scale block:\n" + "\n".join(sorted(over)))


def test_budget_matches_reality():
    current, budget = scan(), json.loads(BUDGET.read_text())
    stale = [f"  {name}: budget {was}, actual {current.get(name, 0)}"
             for name, was in budget.items() if current.get(name, 0) < was]
    assert not stale, (
        "Budget is looser than reality; re-record it to keep the gain:\n"
        + "\n".join(sorted(stale))
        + "\n\n  python tests/test_design_scale.py --update")


if __name__ == "__main__":  # pragma: no cover - maintenance helper
    import sys

    if "--update" in sys.argv:
        BUDGET.write_text(json.dumps(scan(), indent=2, sort_keys=True) + "\n")
        print(f"Recorded: {sum(scan().values())} literal sizes outstanding.")
    else:
        for name, n in scan().items():
            print(f"{name:24} {n}")
