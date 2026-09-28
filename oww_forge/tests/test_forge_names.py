"""Wake word names: what `new` calls a phrase, what is allowed, and rename.

A name is a directory, the clips directory inside it, and the .onnx stem
openWakeWord keys its predictions by, so rename has to move all of them and
the two config lines that record it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forge  # noqa: E402


def test_default_name_is_the_first_spelling():
    # The web UI slugified the whole field, commas and all, and named a
    # word that did not exist.
    assert forge.default_name("hello hanako, hello hanakko") == "hello_hanako"
    assert forge.default_name("  , Hey O'Brien ") == "hey_o_brien"


@pytest.mark.parametrize("ok", ["Verona", "Hey Verona", "hey o'brien", "Ünïcode ok", "x" * forge.NAME_MAX])
def test_names_allowed(ok):
    assert forge.check_name(ok) == ok


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "..", ".hidden", "tab\there", "nul\x00", "x" * (forge.NAME_MAX + 1)])
def test_names_refused(bad):
    with pytest.raises(ValueError):
        forge.check_name(bad)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    ww, models = tmp_path / "wakewords", tmp_path / "models"
    monkeypatch.setattr(forge, "WAKEWORDS", ww)
    monkeypatch.setattr(forge, "MODELS", models)
    d = ww / "hello_hanako"
    (d / "hello_hanako" / "positive_train").mkdir(parents=True)
    (d / "hello_hanako" / "positive_train" / "clip.wav").write_bytes(b"w")
    (d / "hello_hanako.onnx").write_bytes(b"inner")
    (d / "config.yml").write_text(
        "# the name\nmodel_name: \"hello_hanako\"\ntarget_phrase:\n  - \"hello hanako\"\n"
        f"# where\noutput_dir: \"{d}\"\nsteps: 50000\n")
    models.mkdir()
    (models / "hello_hanako.onnx").write_bytes(b"published")
    return ww, models


def test_rename_moves_everything(tree):
    ww, models = tree
    forge.rename_wakeword("hello_hanako", "Hanako")
    d = ww / "Hanako"
    assert not (ww / "hello_hanako").exists()
    assert (d / "Hanako" / "positive_train" / "clip.wav").read_bytes() == b"w"
    assert (d / "Hanako.onnx").read_bytes() == b"inner"
    assert (models / "Hanako.onnx").read_bytes() == b"published"
    assert not (models / "hello_hanako.onnx").exists()
    cfg = (d / "config.yml").read_text()
    assert 'model_name: "Hanako"' in cfg and f'output_dir: "{d}"' in cfg
    assert "# the name" in cfg and "# where" in cfg and "steps: 50000" in cfg  # comments and the rest kept
    import yaml
    loaded = yaml.safe_load(cfg)
    assert loaded["model_name"] == "Hanako" and Path(loaded["output_dir"]) / loaded["model_name"] == d / "Hanako"


def test_rename_refuses_an_existing_name(tree):
    ww, models = tree
    (ww / "Taken").mkdir()
    with pytest.raises(FileExistsError):
        forge.rename_wakeword("hello_hanako", "Taken")
    (models / "Other.onnx").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        forge.rename_wakeword("hello_hanako", "Other")
    assert (ww / "hello_hanako" / "config.yml").exists()  # untouched


def test_rename_refuses_a_bad_name_before_touching_anything(tree):
    ww, _ = tree
    with pytest.raises(ValueError):
        forge.rename_wakeword("hello_hanako", "../escape")
    assert (ww / "hello_hanako" / "hello_hanako").is_dir()


def test_rename_before_training(tree):
    # A word created but never built has no clips dir and no model yet.
    ww, models = tree
    import shutil
    shutil.rmtree(ww / "hello_hanako" / "hello_hanako")
    (ww / "hello_hanako" / "hello_hanako.onnx").unlink()
    (models / "hello_hanako.onnx").unlink()
    forge.rename_wakeword("hello_hanako", "Hanako")
    assert (ww / "Hanako" / "config.yml").exists()
