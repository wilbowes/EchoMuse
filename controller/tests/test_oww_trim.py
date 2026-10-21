"""The openWakeWord raw-buffer trim, exec'd from source (no em_controller import).

Same extraction approach as test_oww_metadata: the suite imports em_controller
only through the openwakeword stub, and this function is self-contained
(collections + the module logger), so lift it out with ast instead.
"""
import ast
import collections
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "em_controller.py"


def _load(log=None):
    """Exec `_trim_oww_raw_buffer` and its keep length from em_controller.py."""
    tree = ast.parse(SOURCE.read_text())
    wanted = [
        node for node in tree.body
        if (isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "_OWW_RAW_BUFFER_KEEP"
            for target in node.targets))
        or (isinstance(node, ast.FunctionDef) and node.name == "_trim_oww_raw_buffer")
    ]
    assert len(wanted) == 2, "the trim function or its keep length moved"
    scope = {"collections": collections, "log": log or logging.getLogger(__name__)}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope["_trim_oww_raw_buffer"], scope["_OWW_RAW_BUFFER_KEEP"]


def test_a_fresh_ten_second_buffer_is_trimmed_to_the_recent_samples():
    log = Mock()
    trim, keep = _load(log)
    before = collections.deque(range(160000), maxlen=160000)  # 10 s at 16 kHz
    model = SimpleNamespace(preprocessor=SimpleNamespace(raw_data_buffer=before))
    assert trim(model) is model
    buf = model.preprocessor.raw_data_buffer
    assert buf is not before
    assert buf.maxlen == keep
    assert list(buf) == list(range(160000 - keep, 160000))
    assert not log.warning.called


def test_an_already_trimmed_buffer_is_left_alone():
    log = Mock()
    trim, keep = _load(log)
    before = collections.deque(range(keep), maxlen=keep)
    model = SimpleNamespace(preprocessor=SimpleNamespace(raw_data_buffer=before))
    assert trim(model) is model
    assert model.preprocessor.raw_data_buffer is before
    assert not log.warning.called


def test_a_model_without_a_preprocessor_is_left_alone_and_logged():
    log = Mock()
    trim, _ = _load(log)
    model = SimpleNamespace()
    assert trim(model) is model
    assert not hasattr(model, "preprocessor")
    assert log.warning.called
    assert "buffer" in log.warning.call_args.args[0]
    assert "predict" in log.warning.call_args.args[0]  # names the cost that stays


def test_a_preprocessor_without_the_buffer_is_left_alone_and_logged():
    log = Mock()
    trim, _ = _load(log)
    model = SimpleNamespace(preprocessor=SimpleNamespace())
    assert trim(model) is model
    assert not hasattr(model.preprocessor, "raw_data_buffer")
    assert log.warning.called
