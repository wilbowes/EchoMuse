from pathlib import Path

import em_oww_models as owm


# ─── prediction_key ──────────────────────────────────────────────────────────
# Regression guard for the silent-zero bug: openwakeword keys its
# prediction dict by filename stem, so scoring a custom model by its raw
# config value (a path) reads 0.0 forever and the wake word never fires.

def test_prediction_key_stock_name_passthrough():
    assert owm.prediction_key("hey_jarvis_v0.1") == "hey_jarvis_v0.1"


def test_prediction_key_path_reduces_to_stem():
    assert owm.prediction_key("/app/data/oww_models/hey_clara.onnx") == "hey_clara"


def test_prediction_key_relative_path():
    assert owm.prediction_key("data/oww_models/hey_clara.onnx") == "hey_clara"


# ─── safe_model_filename ─────────────────────────────────────────────────────

def test_filename_accepts_simple_onnx():
    assert owm.safe_model_filename("hey_clara.onnx") == "hey_clara.onnx"
    assert owm.safe_model_filename("Hey-Clara_2.onnx") == "Hey-Clara_2.onnx"


def test_filename_uses_basename_of_client_path():
    assert owm.safe_model_filename("/tmp/upload/hey.onnx") == "hey.onnx"


def test_filename_rejects_traversal_and_junk():
    assert owm.safe_model_filename("../../etc/passwd") is None
    assert owm.safe_model_filename("model.tflite") is None
    assert owm.safe_model_filename(".hidden.onnx") is None
    assert owm.safe_model_filename("sp ace.onnx") is None
    assert owm.safe_model_filename("") is None
    assert owm.safe_model_filename(".onnx") is None


# ─── models_dir / scan ───────────────────────────────────────────────────────

def test_models_dir_sits_beside_db():
    d = owm.models_dir("/app/data/echomuse.db")
    assert d == Path("/app/data/oww_models")


def test_scan_missing_dir_is_empty(tmp_path):
    assert owm.scan(tmp_path / "nope") == []


def test_scan_lists_only_onnx_sorted(tmp_path):
    (tmp_path / "b_model.onnx").write_bytes(b"x" * 10)
    (tmp_path / "a_model.onnx").write_bytes(b"y" * 20)
    (tmp_path / "notes.txt").write_text("ignore me")
    out = owm.scan(tmp_path)
    assert [m["name"] for m in out] == ["a_model", "b_model"]
    assert out[0]["size"] == 20
    assert out[0]["path"] == str((tmp_path / "a_model.onnx").resolve())


# ─── in_use_by ───────────────────────────────────────────────────────────────

def test_in_use_by_matches_path_refs_only(tmp_path):
    model = tmp_path / "hey_clara.onnx"
    model.write_bytes(b"m")
    configs = {
        "global":   {"owwModel": "hey_jarvis_v0.1"},
        "device-a": {"owwModel": str(model)},
        "device-b": {"owwModel": str(tmp_path / "other.onnx")},
        "device-c": {},
        "device-d": None,
    }
    assert owm.in_use_by(str(model), configs) == ["device-a"]
