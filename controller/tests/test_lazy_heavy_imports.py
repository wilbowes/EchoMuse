"""
The controller starts without openwakeword, scikit-learn or scipy.

On a controller whose Echoes all detect their own wake word and run their
own output chain, none of the three is used, and together they were most of
its memory: openwakeword's package __init__ imports a training helper that
pulls in scikit-learn and scipy (~120 MB, ~2 s), and the output chain
imported scipy at module level (~90 MB). They are now imported where a
feature needs them: openwakeword in the worker thread that builds a
streaming Echo's model, scipy when an Echo without its own output chain
registers (em_eq.preload, also off the event loop).

Run in a fresh interpreter: another test may already have imported them.
"""

import subprocess
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
HEAVY = ("openwakeword", "sklearn", "scipy")

PROBE = f"""
import sys
sys.path.insert(0, {str(CONTROLLER)!r})
import em_api, em_controller, em_esphome, em_player, em_speechgate, em_oww_assets
loaded = sorted({{m.split('.')[0] for m in sys.modules}} & set({HEAVY!r}))
print(','.join(loaded))
"""


def test_importing_the_controller_loads_none_of_the_heavy_packages():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                         text=True, cwd=CONTROLLER, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", (
        f"imported at startup: {out.stdout.strip()}; import them where the "
        "feature that needs them runs")


def test_the_output_chain_still_filters_once_scipy_is_loaded():
    import numpy as np
    import em_eq
    em_eq.preload()
    x = (np.sin(np.linspace(0, 200, 4800)) * 8000).astype(np.int16).tobytes()
    y = em_eq.apply(x, 48000, [6, 0, 0, 0, 0, 0, 0, 0], False)
    assert len(y) == len(x) and y != x


def test_the_models_directory_is_found_without_running_the_package(tmp_path, monkeypatch):
    """The speech gate and the asset sync need only the directory; running
    openwakeword's __init__ is the ~120 MB this avoids. Here running it
    raises, so finding the directory proves it was not run."""
    import em_oww_models
    pkg = tmp_path / "openwakeword"
    (pkg / "resources" / "models").mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('package was imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "openwakeword", raising=False)
    assert em_oww_models.resources_dir() == pkg / "resources" / "models"
    assert "openwakeword" not in sys.modules


def test_no_models_directory_when_openwakeword_is_absent(monkeypatch):
    import importlib.util
    import em_oww_models
    monkeypatch.delitem(sys.modules, "openwakeword", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert em_oww_models.resources_dir() is None


def test_freed_memory_is_returned_without_raising_anywhere(monkeypatch):
    """malloc_trim is glibc's; where libc lacks it, or there is no libc to
    find, releasing models must still work."""
    import ctypes.util
    import em_process_stats
    em_process_stats.release_freed_memory()                 # this host
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    em_process_stats.release_freed_memory()                 # no libc found
    import ctypes
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libc.so.6")

    def unloadable(name):
        raise OSError(name)
    monkeypatch.setattr(ctypes, "CDLL", unloadable)
    em_process_stats.release_freed_memory()                 # libc will not load
    monkeypatch.setattr(ctypes, "CDLL", lambda name: object())
    em_process_stats.release_freed_memory()                 # libc without malloc_trim


def test_the_models_directory_of_an_already_imported_package(tmp_path, monkeypatch):
    import types
    import em_oww_models
    (tmp_path / "resources" / "models").mkdir(parents=True)
    mod = types.ModuleType("openwakeword")
    mod.__file__ = str(tmp_path / "__init__.py")
    monkeypatch.setitem(sys.modules, "openwakeword", mod)
    assert em_oww_models.resources_dir() == tmp_path / "resources" / "models"
    (tmp_path / "resources" / "models").rmdir()
    assert em_oww_models.resources_dir() is None            # installed, no models


def test_no_models_directory_when_the_package_cannot_be_located(monkeypatch):
    import importlib.util
    import types
    import em_oww_models
    monkeypatch.delitem(sys.modules, "openwakeword", raising=False)

    def broken(name):
        raise ValueError(name)
    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert em_oww_models.resources_dir() is None
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: types.SimpleNamespace(submodule_search_locations=None))
    assert em_oww_models.resources_dir() is None            # a module, not a package


def test_the_model_class_is_imported_on_first_use(monkeypatch):
    import types
    import em_oww_models

    class Model:
        pass
    sub = types.ModuleType("openwakeword.model")
    sub.Model = Model
    monkeypatch.setitem(sys.modules, "openwakeword", types.ModuleType("openwakeword"))
    monkeypatch.setitem(sys.modules, "openwakeword.model", sub)
    assert em_oww_models.model_class() is Model
