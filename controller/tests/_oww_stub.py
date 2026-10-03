"""
Opt-in stub for openwakeword, so `em_controller` can be imported at all.

em_controller does `from openwakeword.model import Model as OWWModel` at module
level (em_controller.py:71). openwakeword is not installed in the test
environment — it pulls onnxruntime and scikit-learn, minutes of install for
nothing here — so importing em_controller raises ModuleNotFoundError, which is
why its 2,181 statements measured 0.0% and its only coverage was guards reading
the source.

Installing the real package is not the answer: CI's install line would go from
seconds to minutes on every push to run a suite that scores no audio, and the
model files it loads are on the device side, not here.

This is opt-in per test rather than done in conftest.py, deliberately. Four
existing tests call `pytest.importorskip("openwakeword")` so they skip when the
real package is absent; a conftest-level stub satisfies that check and they stop
skipping, then fail on the missing `MODELS` table. `importorskip` asks whether
the package is importable, and after a global stub the honest answer would be
"yes, at something that is not openwakeword".
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
        Stand-in for openwakeword.model.Model. Constructible and inert.

        `predict` refuses rather than returning a number. A test that reaches a
        scoring path and gets a fabricated score would pass on a value nothing
        measured, which is the same class of wrong as a metric that reads clean
        because it is broken. Real scoring is validated tensor-for-tensor
        against the device's golden fixtures (internal/wakeword/testdata), which
        is where a score belongs.
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