"""
Token conformance for the dashboard.

The dashboard is inline-styled, so its colours cannot be restyled from a
stylesheet — they have to be indirected through CSS custom properties before
anything can theme them. That is what makes this a correctness test rather
than a matter of taste: a light/dark toggle is impossible while a value is
typed at the call site, and a `var()` with no definition renders as *nothing*
(transparent text, invisible borders) without erroring.

Deliberately NOT enforced here: how anything looks. No opinion about scales,
spacing or palette. The rules are only:

  1. every var() the JSX uses is defined
  2. every token is defined in BOTH themes, or neither
  3. the components that legitimately hold literal colours keep them, and
     nothing else acquires new ones (a ratchet, so existing call sites can be
     cleaned pane by pane instead of in one sweep)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
JSX = STATIC / "dashboard.jsx"
HTML = STATIC / "dashboard.html"
BUDGET = Path(__file__).resolve().parent / "design_budget.json"

# Components whose colours are DATA, not styling, with the reason each is
# exempt. These must keep literal hexes — theming them would be a bug.
#
#   LedRing, DeviceDiagram, DeviceDiagramMini
#       render the physical Echo Dot: its black plastic, its actual LED
#       colours. A device drawn in "dark mode" would be a different device.
#   Shell
#       an xterm.js theme. Terminal palettes are a 16-colour contract that
#       programs address by index; they are not ours to restyle.
#   DeviceConfigForm
#       the LED scene swatches are values sent to the hardware. `#00b400` is
#       what the ring will emit, not what the page will paint.
#   deviceState (module scope)
#       `dot` is the simulated LED colour and stays literal; `color` beside
#       it is chrome and is tokenised. Hence module scope is budgeted, not
#       exempt — the split has to hold.
EXEMPT = {"LedRing", "DeviceDiagram", "DeviceDiagramMini", "Shell", "DeviceConfigForm"}

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_COMPONENT = re.compile(r"^function ([A-Z]\w*)")
_VAR_USE = re.compile(r"var\((--[a-z0-9-]+)\)")
_VAR_DEF = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)


# Comments are stripped before tallying, because a GitHub issue reference is
# not a colour: `#235` matches the 3-digit hex form exactly, so citing an
# issue beside a component pushed its tally up and failed the ratchet. Left
# alone that trains people out of citing issues in this file, which is the
# opposite of what the project wants.
#
# Filtering by CONTENT does not work — the obvious "require a letter" rule
# lets `#286040` through, a real colour that happens to be all digits, and a
# ratchet with a false negative is worse than one with a false positive.
# Context is the reliable discriminator: colours live in code, issue numbers
# live in prose.
#
# Line comments are matched only when `//` is not preceded by a colon, so a
# URL inside a string is not mistaken for the start of a comment.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _strip_comments(text: str) -> str:
    """Blank out comments, preserving line numbering so owners still resolve."""
    def blank(m):
        return "".join(c if c == "\n" else " " for c in m.group(0))
    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, text))


def _owner_map(text: str):
    comps = [
        (i, m.group(1))
        for i, line in enumerate(text.split("\n"), 1)
        if (m := _COMPONENT.match(line))
    ]

    def owner(line_no: int) -> str:
        name = "(module)"
        for start, nm in comps:
            if start <= line_no:
                name = nm
            else:
                break
        return name

    return owner


def scan() -> dict[str, int]:
    """Literal hexes per component, excluding the exempt ones."""
    text = JSX.read_text()
    owner = _owner_map(text)
    tally: Counter = Counter()
    for i, line in enumerate(_strip_comments(text).split("\n"), 1):
        who = owner(i)
        if who in EXEMPT:
            continue
        n = len(_HEX.findall(line))
        if n:
            tally[who] += n
    return dict(sorted(tally.items()))


def _theme_blocks() -> dict[str, set[str]]:
    """Token names defined by each theme block in dashboard.html."""
    html = HTML.read_text()
    out: dict[str, set[str]] = {}
    for name, marker in (
        ("light", ':root, :root[data-theme="light"] {'),
        ("dark", ':root[data-theme="dark"] {'),
    ):
        start = html.index(marker) + len(marker)
        end = html.index("\n    }", start)
        out[name] = set(_VAR_DEF.findall(html[start:end]))
    return out


def test_every_var_is_defined():
    """A var() with no definition is invisible, not broken — it renders as
    nothing and reports nothing. This is the failure mode worth a test."""
    defined = set().union(*_theme_blocks().values())
    used = set(_VAR_USE.findall(JSX.read_text()))
    missing = used - defined
    assert not missing, (
        f"dashboard.jsx uses undefined tokens: {sorted(missing)}. "
        f"These render as transparent/absent, silently."
    )


def test_themes_define_the_same_tokens():
    """A token defined only in light falls back to the light value in dark —
    a single wrong-coloured element in an otherwise themed page, which is
    exactly the sort of thing that ships unnoticed."""
    blocks = _theme_blocks()
    only_light = blocks["light"] - blocks["dark"]
    only_dark = blocks["dark"] - blocks["light"]
    assert not (only_light or only_dark), (
        f"Themes disagree.\n  light only: {sorted(only_light)}"
        f"\n  dark only:  {sorted(only_dark)}"
    )


def test_no_new_literal_colours():
    """Ratchet. Existing call sites get cleaned a pane at a time; new ones
    must use tokens from the start."""
    current, budget = scan(), json.loads(BUDGET.read_text())
    over = [
        f"  {name}: {budget.get(name, 0)} → {n}"
        for name, n in current.items()
        if n > budget.get(name, 0)
    ]
    assert not over, (
        "Literal colours increased — use a token from dashboard.html's "
        ":root block so it can be themed:\n" + "\n".join(sorted(over))
        + "\n\nIf the colour is genuinely data (a real LED value, a terminal "
          "palette), add the component to EXEMPT with the reason."
    )


def test_budget_matches_reality():
    """A budget left above the real count is drift already paid for, which
    can then be spent again without the ratchet noticing."""
    current, budget = scan(), json.loads(BUDGET.read_text())
    stale = [
        f"  {name}: budget {was}, actual {current.get(name, 0)}"
        for name, was in budget.items()
        if current.get(name, 0) < was
    ]
    assert not stale, (
        "Budget is looser than reality — re-record it to lock the cleanup "
        "in:\n" + "\n".join(sorted(stale))
        + "\n\n  python tests/test_design_tokens.py --update"
    )


if __name__ == "__main__":  # pragma: no cover - maintenance helper
    import sys

    if "--update" in sys.argv:
        BUDGET.write_text(json.dumps(scan(), indent=2, sort_keys=True) + "\n")
        print(f"Recorded: {sum(scan().values())} literal colours outstanding.")
    else:
        for name, n in scan().items():
            print(f"{name:24} {n}")
