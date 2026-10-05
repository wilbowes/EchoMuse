"""The forge page's colours are the dashboard's.

controller/tests/test_contrast.py proves the dashboard's tokens meet WCAG 2.2
AA in both themes; the forge copies those tokens rather than importing them
(it is a separate image with no access to the controller's files at runtime),
so this holds the copy equal to the original. A token the forge defines must
exist in the dashboard with the same value, in the same theme. Pure text
parsing: no browser, no aiohttp.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORGE = (ROOT / "oww_forge" / "static" / "index.html").read_text()
DASH = (ROOT / "controller" / "static" / "dashboard.html").read_text()


def tokens(html: str, selector: str) -> dict:
    """--name: value pairs in the first rule whose selector list starts with `selector`."""
    m = re.search(re.escape(selector) + r"[^{]*\{(.*?)\}", html, re.S)
    assert m, f"no rule for {selector}"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: " ".join(v.split()) for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def test_light_tokens_match_dashboard():
    forge, dash = tokens(FORGE, ":root, :root[data-theme=\"light\"]"), tokens(DASH, ":root, :root[data-theme=\"light\"]")
    assert forge, "forge defines no light tokens"
    assert {k: v for k, v in forge.items() if dash.get(k) != v} == {}


def test_dark_tokens_match_dashboard():
    forge, dash = tokens(FORGE, ":root[data-theme=\"dark\"]"), tokens(DASH, ":root[data-theme=\"dark\"]")
    assert forge, "forge defines no dark tokens"
    assert {k: v for k, v in forge.items() if dash.get(k) != v} == {}


def test_themes_define_the_same_tokens():
    light = tokens(FORGE, ":root, :root[data-theme=\"light\"]")
    dark = tokens(FORGE, ":root[data-theme=\"dark\"]")
    assert set(light) == set(dark)


def test_every_var_is_defined():
    # An undefined var() renders as nothing: invisible text, no error.
    defined = set(tokens(FORGE, ":root, :root[data-theme=\"light\"]"))
    used = set(re.findall(r"var\((--[\w-]+)", FORGE))
    assert used - defined == set()


def test_dark_surface_text_matches_dark_theme():
    # The log console is dark in both themes, so its text takes the dark
    # theme's values whatever the page theme is (as the dashboard's does).
    on_dark, dark = tokens(FORGE, ".em-console"), tokens(FORGE, ":root[data-theme=\"dark\"]")
    assert on_dark and {k: v for k, v in on_dark.items() if dark.get(k) != v} == {}
