"""
LiveKit WakeWord v2 models, controller-side (kind → engine → gate).

A livekit_v2 .onnx streams its own state: its ONNX graph carries a dozen
input/output tensor pairs between frames, so openwakeword cannot load one,
and neither can the Echo's firmware (which feeds openwakeword embedding
windows and nothing else). The controller must therefore

  1. tell the two kinds apart from the file itself (em_oww_check.classify),
  2. score livekit_v2 with the `livekit-wakeword` client (em_wake_engine),
  3. never offer such a file to a device (wanted_models, desired_assets),
  4. force owwOnDevice off while one is selected (em_shadow.forced_mode).

tests/fixtures holds the two real trained models for one phrase, in each
format: they are the proof of the discriminator, not a stand-in for it.
The pure rules run everywhere; loading and scoring needs onnxruntime (and
the livekit package for the engine), which CI installs — these fail under
CI without them rather than skipping, because a skipped check of a real
model is not a pass.
"""

import os
from pathlib import Path

import pytest

import em_oww_assets
import em_oww_check
import em_oww_models as models
import em_shadow as shadow

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OWW_SAMPLE = FIXTURES / "hey_jarvo.onnx"
LK_SAMPLE = FIXTURES / "hey_jarvo_livekit.onnx"

try:
    import numpy as np
    import onnx                       # noqa: F401  (synthetic graphs below)
    import onnxruntime                # noqa: F401
    HAVE_ORT = True
except ImportError:
    if os.environ.get("CI"):
        raise RuntimeError("onnx and onnxruntime are required under CI: these "
                           "tests classify and score real models")
    HAVE_ORT = False

needs_ort = pytest.mark.skipif(not HAVE_ORT, reason="needs onnxruntime")

try:
    from livekit.wakeword import WakeWordModel
    HAVE_LK = True
except ImportError:
    if os.environ.get("CI"):
        raise RuntimeError("livekit-wakeword is required under CI: it is in "
                           "controller/requirements.txt and the CI job installs it")
    HAVE_LK = False

needs_livekit = pytest.mark.skipif(not HAVE_LK, reason="needs livekit-wakeword")

F = "tensor(float)"
LK_IN = [("chunk", ["N", 1280], F),
         ("c0", [1, 64], F), ("h0", [1, 64], F)]
LK_OUT = [("score", ["N"], F),
          ("c0_out", [1, 64], F), ("h0_out", [1, 64], F)]


# ─── classify_signature: the discriminator, no file involved ─────────────────

@pytest.mark.parametrize("inputs", [
    [("chunk", [1, 1280], F)],
    [LK_IN[0]],                                   # dynamic batch: same rule
    LK_IN,                                        # with state inputs
])
def test_a_chunk_input_names_livekit(inputs):
    assert models.classify_signature(inputs, {}) == models.KIND_LIVEKIT


def test_the_trainer_metadata_names_livekit_alone():
    # The chunk rule is what the livekit loader itself insists on; the
    # stamped `format` is the belt — either identifies v2.
    assert models.classify_signature(
        [("whatever", [1, 1280], F)],
        {"format": models.LIVEKIT_FORMAT}) == models.KIND_LIVEKIT


def test_the_embedding_window_names_oww():
    assert models.classify_signature(
        [("input", ["batch", 16, 96], F)], {}) == models.KIND_OWW


def test_the_two_rules_cannot_both_match_a_real_file():
    # The livekit audio input is 2-D raw samples and the openwakeword input
    # is 3-D embeddings, so no model satisfies both orders of the check —
    # and livekit is checked first anyway.
    both = [("chunk", ["N", 1280], F), ("input", ["batch", 16, 96], F)]
    assert models.classify_signature(both, {}) == models.KIND_LIVEKIT


@pytest.mark.parametrize("inputs", [
    [("input", ["batch", 16, 64], F)],     # not the 96-wide embedding
    [("audio", ["N", 1280], "tensor(int16)")],  # not float, not named chunk
    [],
])
def test_a_model_neither_engine_can_run_has_no_kind(inputs):
    assert models.classify_signature(inputs, {}) is None


# ─── livekit_shape_problem: what the streaming client does, checked first ────

def test_a_streaming_graph_that_carries_its_state_is_accepted():
    assert em_oww_check.livekit_shape_problem(LK_IN, LK_OUT) is None


@pytest.mark.parametrize("inputs, outputs, says", [
    ([("audio", ["N", 1280], F)], [("score", ["N"], F)], "named 'chunk'"),
    ([("chunk", ["N", 1279], F)], [("score", ["N"], F)], "must be"),
    ([("chunk", ["N", 1280], "tensor(int16)")], [("score", ["N"], F)], "must be"),
    ([("chunk", ["N"], F)], [("score", ["N"], F)], "must be"),
    ([("chunk", ["N", 1280], F)], [], "no output"),
    ([("chunk", ["N", 1280], F)],
     [("score", ["N"], "tensor(int64)")], "score output"),
    # _Stream zero-initialises state by declared shape; a dynamic state
    # size has no first frame. (State OUTPUTS may stay dynamic — only
    # inputs need sizing.)
    ([("chunk", ["N", 1280], F), ("c0", ["N", 64], F)],
     [("score", ["N"], F), ("c0_out", ["N", 64], F)], "cannot size this one"),
    # The pairing: _Stream re-injects outputs[1:] by stripping "_out"; a
    # state input with no such output fails mid-stream, never at load.
    (LK_IN, LK_OUT[:2], "does not carry its state"),
])
def test_a_graph_the_stream_cannot_drive_is_refused_with_the_reason(inputs,
                                                                    outputs, says):
    why = em_oww_check.livekit_shape_problem(inputs, outputs)
    assert why and says in why, why


# ─── the two real models: the discriminator against the real thing ───────────

def lk_model(value=0.25, state="pair", broken=False) -> bytes:
    """
    A minimal streaming graph in the livekit_v2 shape: `chunk` (N, 1280)
    float in, one float score out, optionally a c0/c0_out state pair. Answers
    `value` per frame; `broken` reshapes to a size the graph cannot produce,
    so it loads and then fails on its first frame — the failure that would
    otherwise surface mid-stream in the wake listener. `state` may be "in"
    for a state input with no matching _out output.
    """
    import onnx
    from onnx import TensorProto as T, helper

    nodes = [helper.make_node("ReduceMean", ["chunk"], ["m"], axes=[1], keepdims=1),
             helper.make_node("Mul", ["m", "zero"], ["z"]),
             helper.make_node("Add", ["z", "value"], ["a"]),
             helper.make_node("Reshape", ["a", "shape"], ["score"])]
    init = [helper.make_tensor("zero", T.FLOAT, [], [0.0]),
            helper.make_tensor("value", T.FLOAT, [], [value]),
            helper.make_tensor("shape", T.INT64, [1], [3 if broken else 1])]
    ins = [helper.make_tensor_value_info("chunk", T.FLOAT, ["N", 1280])]
    outs = [helper.make_tensor_value_info("score", T.FLOAT, ["N"])]
    if state in ("pair", "in"):
        ins.append(helper.make_tensor_value_info("c0", T.FLOAT, [1, 64]))
    if state == "pair":
        nodes.append(helper.make_node("Identity", ["c0"], ["c0_out"]))
        outs.append(helper.make_tensor_value_info("c0_out", T.FLOAT, [1, 64]))
    g = helper.make_graph(nodes, "streaming", ins, outs, init)
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8)
    onnx.checker.check_model(m)
    return m.SerializeToString()


@needs_ort
def test_a_well_formed_synthetic_stream_is_accepted():
    assert em_oww_check.classify(lk_model()) == (models.KIND_LIVEKIT, None)


@needs_ort
@pytest.mark.parametrize("kw, says", [
    ({"value": 3.0}, "between 0 and 1"),          # logits, no sigmoid
    ({"value": -0.5}, "between 0 and 1"),
    ({"broken": True}, "first frame"),            # loads, then cannot run
    ({"state": "in"}, "does not carry its state"),
])
def test_a_livekit_shaped_graph_that_cannot_score_is_refused_with_the_reason(kw, says):
    kind, why = em_oww_check.classify(lk_model(**kw))
    assert kind == models.KIND_LIVEKIT and says in why, why

@needs_ort
def test_the_openwakeword_sample_classifies_as_oww():
    assert models.model_kind(str(OWW_SAMPLE)) == models.KIND_OWW


@needs_ort
def test_the_livekit_sample_classifies_as_livekit_v2():
    assert models.model_kind(str(LK_SAMPLE)) == models.KIND_LIVEKIT


@needs_ort
def test_both_samples_pass_their_own_upload_check():
    import em_oww_check
    assert em_oww_check.classify(OWW_SAMPLE.read_bytes()) == (models.KIND_OWW, None)
    assert em_oww_check.classify(LK_SAMPLE.read_bytes()) == (models.KIND_LIVEKIT, None)


@needs_ort
def test_garbage_gets_no_kind_and_a_reason():
    kind, why = em_oww_check.classify(b"not an onnx model")
    assert kind is None and "not an ONNX model" in why


def test_without_onnxruntime_nothing_can_be_said(monkeypatch):
    # A controller image built without onnxruntime: classify refuses nothing
    # it cannot inspect — the same nothing-to-say answer problem() has always
    # given, so such an install still accepts uploads rather than failing.
    import sys

    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    assert em_oww_check.classify(b"whatever") == (None, None)
    assert em_oww_check.problem(b"whatever") is None


def test_a_stock_name_and_a_missing_file_default_to_oww():
    # Stock names are openwakeword's by definition; a missing file keeps the
    # pre-#869 failure shape (the listener's load fails and reverts) rather
    # than inventing a new one.
    assert models.model_kind("hey_jarvis_v0.1") == models.KIND_OWW
    assert models.model_kind("/no/such/model.onnx") == models.KIND_OWW


# ─── the engine factory ───────────────────────────────────────────────────────

@needs_livekit
@needs_ort
def test_build_livekit_streams_the_real_model():
    """The engine contract the wake listener and the barge watcher use:
    predict(int16 chunk) -> {model stem: score}, plus reset()."""
    import em_wake_engine
    engine = em_wake_engine.build_livekit(str(LK_SAMPLE))
    assert isinstance(engine, WakeWordModel)
    peak, last = 0.0, {}
    for _ in range(40):                       # 3.2 s of stream
        last = engine.predict(np.zeros(1280, np.int16))
        peak = max(peak, max(last.values()))
    # Keyed by the file stem — the same key openwakeword uses
    # (em_oww_models.prediction_key), so the listener's read is unchanged.
    assert list(last) == [LK_SAMPLE.stem]
    # Trained on this phrase; on 3.2 s of silence it must not fire at any
    # threshold the slider offers (top notch: 0.975).
    assert 0.0 <= peak < 0.9
    engine.reset()
    assert max(engine.predict(np.zeros(1280, np.int16)).values()) < 0.9


@needs_livekit
@needs_ort
def test_the_factory_routes_by_kind():
    import em_wake_engine
    assert isinstance(em_wake_engine.build(str(LK_SAMPLE)), WakeWordModel)


def test_the_factory_routes_an_oww_name_to_openwakeword():
    import _oww_stub
    import em_wake_engine
    _oww_stub.install()
    try:
        engine = em_wake_engine.build("hey_jarvis_v0.1")
        assert engine.__class__.__name__ == "Model"
        assert engine.kwargs["wakeword_models"] == ["hey_jarvis_v0.1"]
    finally:
        _oww_stub.uninstall()


def test_the_speex_flag_reaches_the_openwakeword_constructor():
    # The livekit engine has no speex stage — the v2 model is trained on
    # raw audio — so owwSpeexNs keeps meaning exactly what it means today
    # for oww models and nothing for livekit ones.
    import _oww_stub
    import em_wake_engine
    _oww_stub.install()
    try:
        engine = em_wake_engine.build("hey_jarvis_v0.1", speex=True)
        assert engine.kwargs["enable_speex_noise_suppression"] is True
    finally:
        _oww_stub.uninstall()


# ─── the owwOnDevice gate ─────────────────────────────────────────────────────

@pytest.mark.parametrize("configured", ["on", "shadow", "off", "banana", None])
def test_a_livekit_model_forces_the_mode_off(configured):
    assert shadow.forced_mode(configured, models.KIND_LIVEKIT) == shadow.MODE_OFF


@pytest.mark.parametrize("configured", ["on", "shadow", "off", "banana", None])
def test_an_oww_model_changes_nothing(configured):
    # Unnormalised on purpose: effective_mode normalises at the point of
    # use, and echoing a stored oddity back as "off" would rewrite a choice
    # this rule says nothing about.
    assert shadow.forced_mode(configured, models.KIND_OWW) is configured


def test_forced_config_rewrites_only_a_livekit_selection():
    cfg = {"owwModel": str(LK_SAMPLE), "owwOnDevice": "on", "owwThreshold": 0.5}
    out = shadow.forced_config(cfg)
    assert out["owwOnDevice"] == shadow.MODE_OFF
    assert out["owwModel"] == cfg["owwModel"] and out["owwThreshold"] == 0.5
    assert cfg["owwOnDevice"] == "on", "the stored config is not rewritten"


def test_forced_config_passes_an_oww_config_through_by_identity():
    cfg = {"owwModel": "hey_jarvis_v0.1", "owwOnDevice": "on"}
    assert shadow.forced_config(cfg) is cfg


@needs_ort
def test_forced_config_fires_on_the_real_livekit_model():
    cfg = {"owwModel": str(LK_SAMPLE), "owwOnDevice": "shadow"}
    assert shadow.forced_config(cfg)["owwOnDevice"] == shadow.MODE_OFF


# ─── a device is never offered a livekit model ───────────────────────────────

def _sync_db(monkeypatch, oww_model):
    import em_oww_sync
    monkeypatch.setattr(em_oww_sync.db, "get_effective_device_config",
                        lambda device_id: {"owwModel": oww_model})
    return em_oww_sync


def test_wanted_models_offers_a_livekit_file_to_no_device(monkeypatch):
    sync = _sync_db(monkeypatch, str(LK_SAMPLE))
    assert sync.wanted_models("dev1") == []


def test_wanted_models_still_pins_an_oww_model(monkeypatch):
    sync = _sync_db(monkeypatch, "hey_jarvis_v0.1")
    assert sync.wanted_models("dev1") == ["hey_jarvis_v0.1"]


@needs_ort
def test_hold_back_never_waits_for_an_install_that_cannot_happen(monkeypatch):
    import types

    sync = _sync_db(monkeypatch, "x")            # unused; the gate is on the new model
    device = types.SimpleNamespace(oww_model="hey_jarvis_v0.1",
                                   oww_shadow_capable=True)
    effective = {"owwModel": str(LK_SAMPLE), "owwOnDevice": "off"}
    out, pending = sync.hold_back_model(device, effective)
    assert out is effective and pending is None, \
        "a livekit switch must apply immediately — there is nothing to install"


@needs_ort
def test_the_asset_plan_skips_the_livekit_classifier():
    # The provisioning wizard and the field sync share this: skipping the
    # classifier here keeps both transports and plan_sync's eviction silent
    # about a file that is fine — the controller just scores it itself.
    assets, problems = em_oww_assets.desired_assets(
        [str(LK_SAMPLE)], runtime_dir=Path("/nonexistent"),
        models_dir=FIXTURES, include_stock=False)
    assert not [a for a in assets if a.kind == "classifier"]
    assert not any("jarvo" in p for p in problems), \
        "the skip is not a problem: the model runs, just not there"


# ─── the endpoints: kind on the list, kind on the upload receipt ─────────────

@needs_ort
def test_the_upload_endpoint_installs_a_livekit_model_and_says_so(tmp_path,
                                                                  monkeypatch):
    import asyncio

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import _oww_stub
    _oww_stub.install()
    try:
        import em_routes_oww
    finally:
        _oww_stub.uninstall()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    post_handler = em_routes_oww._post_oww_model_upload.__wrapped__
    get_handler = em_routes_oww._get_oww_models.__wrapped__

    async def run():
        # The transport cap must clear MAX_MODEL_BYTES, as em_api.create_app
        # guarantees for the real app; a 7.4 MB v2 model against aiohttp's
        # 1 MB default would fail at the socket, not at the check.
        app = web.Application(client_max_size=2 * models.MAX_MODEL_BYTES)
        app.router.add_post("/upload", post_handler)
        app.router.add_get("/models", get_handler)
        async with TestClient(TestServer(app)) as c:
            form = aiohttp.FormData()
            form.add_field("model", LK_SAMPLE.read_bytes(),
                           filename=LK_SAMPLE.name)
            good = await c.post("/upload", data=form)
            form = aiohttp.FormData()
            form.add_field("model", b"not an onnx model", filename="fake.onnx")
            junk = await c.post("/upload", data=form)
            listed = await c.get("/models")
            return (good.status, await good.json(),
                    junk.status, await junk.json(),
                    listed.status, await listed.json())

    g_status, g_body, j_status, j_body, l_status, l_body = asyncio.run(run())
    assert g_status == 201, g_body
    assert g_body["model"]["kind"] == models.KIND_LIVEKIT
    assert j_status == 400 and j_body["code"] == "invalid_model"
    assert l_status == 200
    assert [(m["file"], m["kind"]) for m in l_body["models"]] == [
        (LK_SAMPLE.name, models.KIND_LIVEKIT)]
