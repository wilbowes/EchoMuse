"""
The output chain moves to the device when both halves agree (#TBD): the
device announces `output_chain`, the controller announces it back on the
ack, and from then on the controller sends audio untouched.

The failure this guards is the chain running TWICE — a doubled EQ curve and
a second limiter, which sounds like a bad EQ setting rather than a bug — or
running NOWHERE. Both come from one playback path forgetting the gate, and
there are three of them.
"""

import ast
import re
from pathlib import Path

import numpy as np

import em_db
import em_eq

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
CHAIN_GO = ROOT / "device" / "internal" / "outchain" / "chain.go"


def test_passthrough_leaves_audio_untouched():
    pcm = (np.arange(-2000, 2000, dtype=np.int16) * 7).tobytes()
    p = em_eq.Passthrough()
    assert p.process(pcm) is pcm
    assert p.flush() == b""
    assert p.update(bands=[12.0] * 8, loudness=True, limiter_enabled=True) is False
    assert em_eq.describe_activity(p.limiter, p.guard) == \
        "guard_reduction=n/a limiter_reduction=n/a"


def test_passthrough_has_every_method_the_playback_paths_call():
    for name in ("process", "flush", "update", "limiter", "guard"):
        assert hasattr(em_eq.Passthrough, name)
        assert hasattr(em_eq.StreamingEQ, name)


def _functions(path: Path):
    """Module functions and methods — not closures, which are judged with the
    function around them (a nested helper reads the gate through its
    enclosing scope, as _prepare_pcm does)."""
    fns = (ast.FunctionDef, ast.AsyncFunctionDef)
    tree = ast.parse(path.read_text())
    out = [n for n in tree.body if isinstance(n, fns)]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        out += [n for n in cls.body if isinstance(n, fns)]
    return out


def _builds_a_chain(fn) -> bool:
    """Calls em_eq.StreamingEQ(...) or em_eq.apply(...) — in code, not prose."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("StreamingEQ", "apply")
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "em_eq"):
            return True
    return False


def _reads_the_gate(fn) -> bool:
    # An Attribute node, so a comment or docstring naming it cannot pass.
    return any(isinstance(n, ast.Attribute) and n.attr == "output_chain_on_device"
               for n in ast.walk(fn))


def test_every_playback_path_that_builds_a_chain_checks_the_gate():
    builders = []
    for mod in ("em_controller.py", "em_player.py", "em_esphome.py", "em_api.py"):
        for fn in _functions(CONTROLLER / mod):
            if _builds_a_chain(fn):
                builders.append((mod, fn))
    names = sorted(f"{m}:{f.name}" for m, f in builders)
    # Three today. A fourth is fine, provided it is gated too.
    assert len(builders) >= 3, names
    ungated = [f"{m}:{f.name}" for m, f in builders if not _reads_the_gate(f)]
    assert not ungated, f"builds the chain without checking the device: {ungated}"


def test_the_gate_is_the_capability_the_device_announces():
    src = (CONTROLLER / "em_controller.py").read_text()
    m = re.search(r"def output_chain_on_device\(self\).*?return (.+?)\n", src, re.S)
    assert m and '"output_chain" in (self.capabilities' in m.group(1)


def test_device_defaults_match_the_controllers():
    """
    Until its first config push the device plays with its own defaults, and
    they must be what the controller would have applied — otherwise a device
    sounds different for the seconds after every reconnect.
    """
    src = CHAIN_GO.read_text()
    body = re.search(r"func DefaultParams\(\) Params \{(.*?)\n\}", src, re.S).group(1)
    go = dict(re.findall(r"(\w+):\s*([-\w.]+),", body))
    d = em_db.DEFAULT_DEVICE_CONFIG
    assert go["GuardEnabled"] == str(d["bassGuardEnabled"]).lower()
    assert float(go["GuardDb"]) == d["bassGuardDb"]
    assert go["LimiterEnabled"] == str(d["limiterEnabled"]).lower()
    assert float(go["LimiterThresholdDb"]) == d["limiterThreshold"]
    assert float(go["LimiterReleaseMs"]) == d["limiterRelease"]
    # Bands and loudness take Go's zero values: flat and off.
    assert "Bands" not in go and "Loudness" not in go
    assert d["eqBands"] == [0.0] * 8 and d["eqLoudness"] is False
