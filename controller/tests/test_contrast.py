"""
WCAG 2.2 AA contrast for the dashboard's colour tokens.

Where a standard exists we conform to it and prove it (CLAUDE.md), and for
colour that standard is WCAG 2.2 level AA:

  1.4.3  text:      4.5:1 (3:1 only for large text, which nothing here is)
  1.4.11 non-text:  3:1 for control edges, states and focus indicators

Ratios are computed with WCAG's own relative-luminance formula, from the
token values in dashboard.html, in BOTH themes. Every panel and button is a
gradient, so text is checked against both ends of the one it sits on, and
the translucent tints (the step list, a selected card) are composited over
the surface beneath before measuring. Those two are what a pairwise check of
flat tokens misses: axe-core found both on the wizard mockup, 2026-09-25.

What is not checked: literal colours in the exempt components (the Echo
drawing, the terminal, LED swatches). Those are the device's colours, see
test_design_tokens.EXEMPT.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

TEXT, NON_TEXT = 4.5, 3.0


def _theme(selector: str) -> dict[str, str]:
    html = HTML.read_text()
    start = html.index(selector)
    body = html[start:html.index("}", start)]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6}|rgba\([^)]*\))", body))


LIGHT = _theme(':root, :root[data-theme="light"] {')
DARK = {**LIGHT, **_theme(':root[data-theme="dark"] {')}
THEMES = {"light": LIGHT, "dark": DARK}


def _rgb(value: str, under: tuple[float, ...] | None = None) -> tuple[float, ...]:
    """0-255 RGB; an rgba() is composited over `under`, as the browser paints it."""
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b, a = (float(x) for x in re.findall(r"[\d.]+", value))
    assert under is not None, f"{value} is translucent; say what it sits on"
    return tuple(c * a + u * (1 - a) for c, u in zip((r, g, b), under))


def _luminance(rgb) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _surfaces(t: dict[str, str]) -> dict[str, tuple]:
    """Every background text can sit on, including the tints over panels."""
    flat = {k: _rgb(t[f"--{k}"]) for k in ("raised", "card", "surface", "bg")}
    tinted = {f"hairline over {k}": _rgb(t["--hairline"], flat[k]) for k in ("raised", "card")}
    return {**flat, **tinted}


# Tokens used as text, on any surface.
TEXT_TOKENS = ["text", "text2", "muted", "accent", "ok", "warn", "error"]

# Text on a filled control: (label, [every stop of the fill it can sit on]).
LABELS = [
    ("accent-tint", ["accent-hi", "accent"]),               # .em-pill--accent, .em-iconbtn--accent (hover too)
    ("on-error", ["error", "error-deep"]),                  # .em-pill--danger
    ("text", ["surface", "border-soft"]),                   # .em-pill
]

# Non-text: control edges (--field-line on inputs, --faint on .em-pill), the
# selected state (--accent) and the focus outline (--accent-hi), on panels.
NON_TEXT_TOKENS = ["field-line", "accent", "accent-hi", "faint"]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", TEXT_TOKENS)
def test_text_tokens_meet_4_5_on_every_surface(theme, token):
    t = THEMES[theme]
    fg = _rgb(t[f"--{token}"])
    bad = {name: round(ratio(fg, bg), 2) for name, bg in _surfaces(t).items() if ratio(fg, bg) < TEXT}
    assert not bad, f"--{token} ({theme}) under {TEXT}:1 on {bad}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("label,fills", LABELS)
def test_button_labels_meet_4_5_at_both_ends_of_their_gradient(theme, label, fills):
    t = THEMES[theme]
    fg = _rgb(t[f"--{label}"])
    bad = {f: round(ratio(fg, _rgb(t[f"--{f}"])), 2) for f in fills if ratio(fg, _rgb(t[f"--{f}"])) < TEXT}
    assert not bad, f"--{label} ({theme}) under {TEXT}:1 on {bad}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", NON_TEXT_TOKENS)
def test_control_edges_and_focus_meet_3_on_panels(theme, token):
    t = THEMES[theme]
    fg = _rgb(t[f"--{token}"])
    panels = {k: v for k, v in _surfaces(t).items() if k != "bg"}
    bad = {name: round(ratio(fg, bg), 2) for name, bg in panels.items() if ratio(fg, bg) < NON_TEXT}
    assert not bad, f"--{token} ({theme}) under {NON_TEXT}:1 on {bad}"


def test_the_ratio_matches_wcags_own_examples():
    # Black on white is the 21:1 ceiling; #767676 on white is the classic
    # smallest grey that passes 4.5:1 (4.54).
    assert round(ratio((0, 0, 0), (255, 255, 255)), 2) == 21.0
    assert round(ratio(_rgb("#767676"), (255, 255, 255)), 2) == 4.54
    assert ratio(_rgb("#777777"), (255, 255, 255)) < 4.5
