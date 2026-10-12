import json
from pathlib import Path

import pytest

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


# ─── scalar_score ────────────────────────────────────────────────────────────
# #869: a classifier exported with a [batch, 1, 1] output leaves openwakeword
# holding a one-element array, and the listener crashed formatting it.

@pytest.mark.parametrize("shape", [(), (1,), (1, 1), (1, 1, 1)])
def test_scalar_score_takes_any_output_shape(shape):
    np = pytest.importorskip("numpy")
    score = owm.scalar_score(np.full(shape, 0.25, dtype=np.float32))
    assert type(score) is float and score == 0.25
    assert f"{score:.3f}" == "0.250"
    assert json.dumps({"score": score}) == '{"score": 0.25}'


def test_scalar_score_passes_plain_numbers_through():
    # openwakeword returns a Python 0.0 for its first five frames, and
    # dict.get's default is one too.
    assert owm.scalar_score(0.0) == 0.0 and type(owm.scalar_score(0)) is float
    np = pytest.importorskip("numpy")
    assert owm.scalar_score(np.float32(0.5)) == 0.5


def test_scalar_score_reads_the_first_element_as_the_firmware_does():
    np = pytest.importorskip("numpy")
    assert owm.scalar_score(np.array([[0.75, 0.1]], dtype=np.float32)) == 0.75


def test_every_score_the_controller_reads_is_made_scalar():
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    reads = [l.strip() for l in src.splitlines() if "prediction.get(" in l]
    assert len(reads) >= 2
    assert all(l.startswith("score = em_oww_models.scalar_score(prediction.get(")
               for l in reads), reads


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


# ── display_name ──────────────────────────────────────────────────────────

def test_display_name_strips_the_version_suffix():
    assert owm.display_name("hey_jarvis_v0.1") == "hey jarvis"
    assert owm.display_name("alexa_v0.1") == "alexa"


def test_display_name_handles_custom_model_paths():
    """
    Custom models arrive as a path (owwModel stores one), and carry no
    version suffix — oww_forge names the file after the phrase, which is
    the only source of the name that exists for them: the ONNX files have
    no metadata at all (metadata_props={}, empty doc strings, raw torch_jit
    exports) and custom models are not in openwakeword.MODELS either.
    """
    assert owm.display_name(
        "/data/oww_models/hey_clarra.onnx") == "hey clarra"
    assert owm.display_name("hey_clarra") == "hey clarra"


def test_display_name_leaves_a_version_inside_the_name_alone():
    """Only a TRAILING _v<n> is a packaging suffix."""
    assert owm.display_name("hey_v2_robot") == "hey v2 robot"


def test_display_name_matches_openwakeword_own_names():
    """
    The version suffix is a filename convention, so trimming it is a guess
    unless it is checked against the package that invented the convention.
    openwakeword.MODELS is keyed by the clean name and values point at the
    versioned file, which makes it the authority for every stock model.

    Skipped where openwakeword is absent — CI installs pytest/numpy/scipy/
    pyyaml only, and em_oww_models stays dependency-free so it can be unit
    tested at all. Where the package IS present this is exact.
    """
    openwakeword = pytest.importorskip("openwakeword")

    for name, spec in openwakeword.MODELS.items():
        stem = Path(spec["model_path"]).stem
        assert owm.display_name(stem) == name.replace("_", " "), (
            f"display_name({stem!r}) disagrees with openwakeword's own name "
            f"for the model, {name!r}")
