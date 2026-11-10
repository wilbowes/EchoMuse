"""Metadata decisions and real refresh logic, without runtime dependencies."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import em_oww_metadata as metadata
import em_oww_models


@pytest.mark.parametrize("values", [{}, {"wake_word": ""}, {"wake_word": None}, {"wake_word": []}, {"language": "invalid code"}])
def test_stock_fallback(values):
    assert metadata.from_metadata("hey_jarvis_v0.1", values) == metadata.WakeWord("hey jarvis")


def test_custom_identity_does_not_change_prediction_key():
    name = "/models/not_the_phrase.onnx"
    assert metadata.from_metadata(name, {"wake_word": " Hallo Welt ", "language": "de_DE"}) == metadata.WakeWord("Hallo Welt", ("de-DE",))
    assert em_oww_models.prediction_key(name) == "not_the_phrase"


@pytest.mark.parametrize(
    "class_name", ["EchoMuseSatellite", "DeviceESPhomeServer"]
)
def test_esphome_constructors_require_resolved_model_metadata(class_name):
    source = Path(__file__).resolve().parents[1] / "em_esphome.py"
    tree = ast.parse(source.read_text())
    class_def = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    constructor = next(
        node
        for node in class_def.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    positional = constructor.args.posonlyargs + constructor.args.args
    required = positional[:len(positional) - len(constructor.args.defaults)]
    assert "oww_model_info" in {argument.arg for argument in required}

    assignments = [
        node for node in ast.walk(constructor)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "oww_model_info"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.Name)
    assert assignments[0].value.id == "oww_model_info"


def test_missing_and_unreadable_models_fall_back(tmp_path, monkeypatch):
    path = tmp_path / "hey_robot.onnx"
    assert metadata.resolve(str(path)).name == "hey robot"
    path.write_bytes(b"not an onnx model")
    def fail(*args):
        raise RuntimeError("unreadable metadata")
    monkeypatch.setattr(metadata, "_read", fail)
    assert metadata.resolve(str(path)).name == "hey robot"


def test_file_replacement_changes_reader_identity(tmp_path, monkeypatch):
    path = tmp_path / "robot.onnx"
    path.write_bytes(b"first")
    reader = Mock(return_value={"wake_word": "First"})
    monkeypatch.setattr(metadata, "_read", reader)
    assert metadata.resolve(str(path)).name == "First"
    old = reader.call_args
    path.write_bytes(b"a different model")
    reader.return_value = {"wake_word": "Second"}
    assert metadata.resolve(str(path)).name == "Second"
    assert reader.call_args != old


def test_same_id_metadata_change_refreshes_connection(monkeypatch):
    # Execute the production refresh function with its network boundary injected.
    source = Path(__file__).resolve().parents[1] / "em_esphome.py"
    tree = ast.parse(source.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_oww_model")
    satellite = SimpleNamespace(disconnect=Mock())
    server = SimpleNamespace(oww_model_id="robot", oww_model_info=metadata.WakeWord("Old"), get_satellite=lambda: satellite,
                             wake_word_enabled=True, ha_select_stale=False)
    current = metadata.WakeWord("Hallo", ("de",))
    monkeypatch.setattr(metadata, "resolve", lambda name: current)
    scope = {"asyncio": asyncio, "em_oww_models": em_oww_models, "em_oww_metadata": metadata,
             "get_server": lambda device: server, "log": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
    asyncio.run(scope["update_oww_model"]("device", "/models/robot.onnx"))
    assert server.oww_model_id == "robot"
    assert server.oww_model_info == current
    satellite.disconnect.assert_called_once()
    asyncio.run(scope["update_oww_model"]("device", "/models/robot.onnx"))
    satellite.disconnect.assert_called_once()


def test_newer_refresh_wins_when_reads_finish_out_of_order(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "em_esphome.py"
    function = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_oww_model")
    satellite = SimpleNamespace(disconnect=Mock())
    server = SimpleNamespace(oww_model_id="initial", oww_model_info=metadata.WakeWord("Initial"), get_satellite=lambda: satellite,
                             wake_word_enabled=True, ha_select_stale=False)
    async def run():
        first_started, release_first = asyncio.Event(), asyncio.Event()
        async def read(_function, model):
            if model == "first":
                first_started.set()
                await release_first.wait()
            return metadata.WakeWord(model)
        scope = {"asyncio": SimpleNamespace(to_thread=read), "em_oww_models": em_oww_models,
                 "em_oww_metadata": metadata, "get_server": lambda device: server,
                 "log": logging.getLogger(__name__)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
        first = asyncio.create_task(scope["update_oww_model"]("device", "first"))
        await first_started.wait()
        await scope["update_oww_model"]("device", "second")
        release_first.set()
        await first
    asyncio.run(run())
    assert server.oww_model_id == "second"
    assert server.oww_model_info.name == "second"
    satellite.disconnect.assert_called_once()
