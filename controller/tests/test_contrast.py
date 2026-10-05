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


# ── Surfaces that are dark in both themes ─────────────────────────────────────
# The LCD readouts, insets, console and update banner. Text inside them reads
# the dark theme's token values whatever the page theme is (the `.em-lcd, ...`
# rule in dashboard.html); in the light theme the page's own --warn and
# --muted measured 1.9-2.2:1 there.

JSX = HTML.parent / "dashboard.jsx"
_DARK_RULE = ".em-lcd, .em-inset, .em-console, .em-ctrl-update, .em-on-dark {"


def _dark_rule() -> dict[str, str]:
    html = HTML.read_text()
    start = html.index(_DARK_RULE)
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", html[start:html.index("}", start)]))


def _dark_surfaces(t: dict[str, str]) -> dict[str, tuple]:
    out = {k: _rgb(t[f"--{k}"]) for k in ("lcd-face", "lcd-bg", "lcd-deep")}
    for i, stop in enumerate(re.findall(r"#[0-9a-fA-F]{6}", t["--notice-bg"] if "--notice-bg" in t else "")):
        out[f"notice-bg stop {i}"] = _rgb(stop)
    return out


def test_the_dark_surface_rule_is_the_dark_theme():
    rule = _dark_rule()
    assert rule, "dashboard.html must keep the dark-surface token rule"
    drift = {k: (v, DARK[k]) for k, v in rule.items() if DARK[k].lower() != v.lower()}
    assert not drift, f"dark-surface values differ from the dark theme: {drift}"


def _notice(theme):
    html = HTML.read_text()
    sel = ':root, :root[data-theme="light"] {' if theme == "light" else ':root[data-theme="dark"] {'
    start = html.index(sel)
    block = html[start:html.index("}", start)]
    return dict(re.findall(r"(--notice-bg):\s*([^;]+);", block))


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", TEXT_TOKENS)
def test_text_tokens_meet_4_5_on_dark_surfaces(theme, token):
    t = {**THEMES[theme], **_notice(theme)}
    fg = _rgb(_dark_rule()[f"--{token}"])
    bad = {n: round(ratio(fg, bg), 2) for n, bg in _dark_surfaces(t).items() if ratio(fg, bg) < TEXT}
    assert not bad, f"--{token} inside a dark surface ({theme}) under {TEXT}:1 on {bad}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", ["lcd-green", "lcd-amber", "lcd-dim", "accent-lit"])
def test_lcd_text_colours_meet_4_5(theme, token):
    t = THEMES[theme]
    fg = _rgb(t[f"--{token}"])
    panels = {n: bg for n, bg in _dark_surfaces(t).items() if not n.startswith("notice")}
    panels.update({f"glow over {n}": _glow(bg, fg) for n, bg in list(panels.items())})
    bad = {n: round(ratio(fg, bg), 2) for n, bg in panels.items() if ratio(fg, bg) < TEXT}
    assert not bad, f"--{token} ({theme}) under {TEXT}:1 on {bad}"


def _glow(surface, fg, share=0.12):
    """An LCD readout's text glows (a text-shadow in its own colour), which
    lightens the panel directly behind the glyphs. axe-core measured it at
    about 12% of the way to the text colour, 2026-09-25."""
    return tuple(s + (f - s) * share for s, f in zip(surface, fg))


def test_device_state_names_are_legible_on_the_lcd():
    # deviceState's `dot` is the LED's simulated colour and stays exact; `lcd`
    # is what the state's NAME is written in on an LCD readout.
    states = re.findall(r"key: '(\w+)',[^}]*?lcd: 'var\((--[\w-]+)\)'", JSX.read_text())
    assert len(states) >= 7, "every deviceState entry needs an lcd colour token"
    for theme, t in THEMES.items():
        for key, token in states:
            lcd = t[token]
            fg = _rgb(lcd)
            panels = [bg for n, bg in _dark_surfaces(t).items() if not n.startswith("notice")]
            worst = min(ratio(fg, bg) for bg in panels + [_glow(bg, fg) for bg in panels])
            assert worst >= TEXT, f"{key} lcd {lcd} ({theme}) {worst:.2f}:1"


def test_uncoloured_text_uses_the_theme_text_colour():
    # Without it, text that sets no colour of its own falls back to black,
    # which is 1.2:1 on the dark theme.
    assert re.search(r"html, body \{[^}]*color: var\(--text\)", HTML.read_text())
