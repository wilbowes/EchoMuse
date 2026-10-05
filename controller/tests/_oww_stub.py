"""
Opt-in stub for openwakeword, so `em_controller` can be imported.

em_controller imports `openwakeword.model.Model` at module level, and the
real package (onnxruntime, scikit-learn) is not installed for tests.

Opt-in per test, not in conftest: four tests `importorskip("openwakeword")`
and a global stub would make them stop skipping and fail.
"""

import sys
import types


def install() -> None:
    """Put a minimal openwakeword in sys.modules so em_controller imports.

    Idempotent, and a no-op if the real package is importable — a developer who
    has openwakeword installed gets the real thing rather than a fake, so the
    same test means the same thing locally and in CI.
    """
    if "openwakeword" in sys.modules:
        return
    try:
        import openwakeword  # noqa: F401
        return
    except ImportError:
        pass

    oww = types.ModuleType("openwakeword")
    model = types.ModuleType("openwakeword.model")

    class Model:
        """
        Constructible and inert. `predict` raises: a fabricated score would let a
        test pass on a number nothing measured. Scoring is checked against the
        device's golden fixtures.
        """

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def predict(self, *args, **kwargs):
            raise AssertionError(
                "the stubbed openwakeword Model.predict was called. The suite "
                "does not score audio; a fabricated score would pass on a value "
                "nothing measured. Use internal/wakeword/testdata for that."
            )

        def close(self):
            pass

    model.Model = Model
    oww.model = model
    oww.Model = Model
    oww.__stub__ = True
    model.__stub__ = True
    sys.modules["openwakeword"] = oww
    sys.modules["openwakeword.model"] = model


def uninstall() -> None:
    """Remove the stub, so a later test sees the environment as it was.

    Needed because sys.modules is global and four tests in the suite
    `importorskip` openwakeword. Without this, one em_controller test would
    silently disable their skip and fail four unrelated tests.
    """
    if isinstance(sys.modules.get("openwakeword"), types.ModuleType):
        if getattr(sys.modules["openwakeword"], "__stub__", False):
            del sys.modules["openwakeword"]
    if isinstance(sys.modules.get("openwakeword.model"), types.ModuleType):
        if getattr(sys.modules["openwakeword.model"], "__stub__", False):
            del sys.modules["openwakeword.model"]