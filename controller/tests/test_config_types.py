"""
em_config_types: the type each config value must have.

The table is only useful while it matches the two readers it protects, so the
first tests hold it against them: the device's ConfigMessage (parsed from
config.go), and every key the controller converts when a device registers.
"""

import ast
import math
import re
from pathlib import Path

import pytest

import em_config_types as T
import em_db

REPO = Path(__file__).resolve().parents[2]
CONTROLLER = REPO / "controller"

# Keys the table deliberately leaves out, and why.
UNLISTED = {
    "consolePassword",      # pointer on the wire; validated by em_console_pw
    "consoleTimeoutMin",    # pointer on the wire; _validate_console_timeout
    "controllerEndpoints",  # fleet-only, validated by em_endpoints
    "ledScene", "ledListenColor", "ledThinkColor",  # em_scenes falls back
    "meterAttack", "meterDecay", "meterFloor",       # em_scenes falls back
    "meterGamma", "meterRef", "meterCurve",
}

# ConfigMessage fields that are not config values.
NOT_CONFIG = {"type", "hasBeamforming", "listeningAnim",
              "consolePassword", "consoleTimeoutMin"}

GO_KIND = {"int": T.INT, "float64": T.FLOAT, "bool": T.BOOL,
           "string": T.STR, "[]float64": T.FLOAT_LIST}


def _go_fields() -> dict[str, str]:
    src = (REPO / "device/internal/config/config.go").read_text()
    body = src.split("type ConfigMessage struct {", 1)[1].split("\n}", 1)[0]
    out = {}
    for m in re.finditer(r'^\s*\w+\s+(\*?[\w\[\].]+)\s+`json:"(\w+)', body, re.M):
        go_type, key = m.group(1).lstrip("*"), m.group(2)
        out[key] = go_type
    return out


def test_config_go_was_parsed():
    fields = _go_fields()
    assert len(fields) > 25 and fields["micGainDb"] == "int"


def test_every_device_field_is_listed_with_its_go_type():
    for key, go_type in _go_fields().items():
        if key in NOT_CONFIG:
            continue
        assert key in T.KINDS, f"{key} ({go_type}) is decoded by the device but not listed"
        assert T.KINDS[key] == GO_KIND[go_type], (
            f"{key}: listed as {T.KINDS[key]!r}, the device decodes {go_type}")


def test_every_default_key_is_listed_or_named_as_unlisted():
    unclassified = set(em_db.DEFAULT_DEVICE_CONFIG) - set(T.KINDS) - UNLISTED
    assert not unclassified, (
        f"add {sorted(unclassified)} to em_config_types.KINDS, or to UNLISTED "
        f"here with the reason")


def test_every_default_fits_its_kind():
    for key, kind in T.KINDS.items():
        if key in em_db.DEFAULT_DEVICE_CONFIG:
            assert T.fits(kind, em_db.DEFAULT_DEVICE_CONFIG[key]), key


def test_every_registration_conversion_is_covered():
    # handle_control converts these with float()/int()/bool(); each must be
    # listed, or a stored bad value reaches the conversion.
    src = (CONTROLLER / "em_controller.py").read_text()
    body = src.split("async def handle_control", 1)[1].split("\nasync def ", 1)[0]
    converted = set(re.findall(
        r'(?:float|int|bool)\(\s*config\.get\(\s*["\'](\w+)["\']', body))
    assert converted, "no conversions found; has handle_control changed shape?"
    assert converted <= set(T.KINDS), sorted(converted - set(T.KINDS))


@pytest.mark.parametrize("kind,good,bad", [
    (T.INT, [0, -1, 42], [30.0, 1.5, "3", True, None]),
    (T.FLOAT, [0, 0.5, -30, 1e-3], ["0.5", math.nan, math.inf, False, None]),
    (T.BOOL, [True, False], ["false", 0, 1, None]),
    (T.STR, ["", "hey_jarvis_v0.1"], [1, None, ["x"]]),
    (T.FLOAT_LIST, [[], [0.0] * 8, [1, -2.5]], [[1, None], [math.nan], "0,0", None]),
])
def test_fits_from_the_edges(kind, good, bad):
    for v in good:
        assert T.fits(kind, v), (kind, v)
    for v in bad:
        assert not T.fits(kind, v), (kind, v)


def test_problems_names_each_bad_changed_key():
    msgs = T.problems({"owwThreshold": "0.5", "saveUtterances": "false",
                       "micGainDb": 24, "ledScene": 7}, stored={})
    assert len(msgs) == 2
    assert any(m.startswith("owwThreshold must be a number") for m in msgs)
    assert any(m.startswith("saveUtterances must be true or false") for m in msgs)


def test_problems_lets_an_unchanged_stored_value_through():
    # A read-modify-write sends back what is stored; a value stored before the
    # check existed must not make every later save fail.
    stored = {"micGainDb": 30.0}
    assert T.problems({"micGainDb": 30.0}, stored) == []
    assert T.problems({"micGainDb": 31.0}, stored) != []
    # Same value, different type is a change: 1 == True in Python.
    assert T.problems({"saveUtterances": 1}, {"saveUtterances": True}) != []


def test_drop_invalid_removes_only_bad_listed_keys():
    cfg = {"owwThreshold": "abc", "micGainDb": 24, "ledScene": 7, "x": None}
    out, bad = T.drop_invalid(cfg)
    assert bad == ["owwThreshold"]
    assert out == {"micGainDb": 24, "ledScene": 7, "x": None}
    assert cfg["owwThreshold"] == "abc"   # input not mutated


def test_drop_invalid_returns_a_clean_config_unchanged():
    cfg = dict(em_db.DEFAULT_DEVICE_CONFIG)
    out, bad = T.drop_invalid(cfg)
    assert bad == [] and out is cfg


# ── Every full-config push is filtered ──────────────────────────────────────

def _config_pushes(tree):
    """(enclosing function, splatted expression) for send_control({"type": "config", **x})."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "send_control" and node.args
                    and isinstance(node.args[0], ast.Dict)):
                continue
            d = node.args[0]
            is_config = any(isinstance(k, ast.Constant) and k.value == "type"
                            and isinstance(v, ast.Constant) and v.value == "config"
                            for k, v in zip(d.keys, d.values))
            for k, v in zip(d.keys, d.values):
                if is_config and k is None:   # a ** splat: a full config
                    out.append((fn, v))
    return out


def _calls(fn, name):
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == name
               for n in ast.walk(fn))


def test_every_em_api_config_push_is_filtered():
    tree = ast.parse((CONTROLLER / "em_api.py").read_text())
    pushes = [(fn, v) for fn, v in _config_pushes(tree) if fn.name != "push_config"]
    assert len(pushes) >= 3
    for fn, v in pushes:
        direct = isinstance(v, ast.Call) and getattr(v.func, "id", None) == "_well_typed"
        assert direct or _calls(fn, "_well_typed"), (
            f"{fn.name} pushes a full config without _well_typed")


def test_registration_filters_before_it_pushes():
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_control")
    pushes = [v for f, v in _config_pushes(ast.Module(body=[fn], type_ignores=[]))
              if f is fn]
    assert len(pushes) == 1
    drops = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "drop_invalid"]
    assert drops, "handle_control no longer calls em_config_types.drop_invalid"
    assert drops[0].lineno < pushes[0].lineno, "drop_invalid must run before the push"
