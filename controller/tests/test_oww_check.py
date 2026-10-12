"""
The upload check for a custom wake word model (em_oww_check).

The shape and score rules run everywhere. The tests that load a model need
onnx and onnxruntime, which CI installs for this job; without them locally
they skip, and under CI they fail, because a skipped check of what is
refused is not a pass.
"""

import math
import os
from pathlib import Path

import pytest

import em_oww_check as chk

F = "tensor(float)"
GOOD_IN = [([None, 16, 96], F)]
GOOD_OUT = [(["batch", 1], F)]


# ─── shape_problem: the rules, from what each consumer reads ─────────────────

@pytest.mark.parametrize("out", [
    [None, 1], ["batch_size", 1], [1, 1],          # openwakeword's own exports
    ["batch_size", 1, 1],                          # nanowakeword's (#869)
])
def test_one_score_per_frame_is_accepted(out):
    assert chk.shape_problem(GOOD_IN, [(out, F)]) is None


@pytest.mark.parametrize("window", [1, 16, 28, 41])
def test_any_fixed_window_is_accepted(window):
    # openwakeword feeds whatever inputs[0].shape[1] says; only the Echo
    # needs 16, and the controller's listener runs the rest.
    assert chk.shape_problem([(["N", window, 96], F)], GOOD_OUT) is None


@pytest.mark.parametrize("inputs, says", [
    ([], "0 inputs"),
    (GOOD_IN * 2, "2 inputs"),
    ([([None, 16, 96], "tensor(double)")], "tensor(double)"),
    ([([None, 16, 64], F)], "[N, 16, 64]"),        # not openWakeWord embeddings
    ([([None, 96], F)], "[N, 96]"),
    ([([None, 16, 96, 1], F)], "[N, 16, 96, 1]"),
    ([([None, "frames", 96], F)], "[N, N, 96]"),   # openwakeword cannot size it
    ([([None, 0, 96], F)], "[N, 0, 96]"),
    ([([None, True, 96], F)], "must be"),
])
def test_an_input_neither_side_can_feed_is_refused(inputs, says):
    why = chk.shape_problem(inputs, GOOD_OUT)
    assert why and says in why


@pytest.mark.parametrize("outputs, says", [
    ([], "no output"),
    ([([None, 2], F)], "[N, 2]"),                  # multi-class: keyed "0", "1"
    ([([None], F)], "[N]"),
    ([([None, "classes"], F)], "[N, N]"),
    ([([None, 1, 2], F)], "[N, 1, 2]"),
    ([([None, 1], "tensor(int64)")], "tensor(int64)"),
])
def test_an_output_that_is_not_one_score_is_refused(outputs, says):
    why = chk.shape_problem(GOOD_IN, outputs)
    assert why and says in why


def test_only_the_first_output_is_read():
    # Both sides ask for output 0 by name, so a second one changes nothing.
    assert chk.shape_problem(GOOD_IN, GOOD_OUT + [([None, 7], F)]) is None


# ─── score_problem ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("v", [0.0, 1.0, 0.5, 1e-9])
def test_a_probability_is_a_score(v):
    assert chk.score_problem([v]) is None


@pytest.mark.parametrize("values", [
    [], [0.1, 0.2], [-0.01], [1.01], [math.nan], [math.inf], [-math.inf]])
def test_anything_else_is_not(values):
    assert chk.score_problem(values)


# ─── problem(): real models through onnxruntime ──────────────────────────────

try:
    import numpy as np
    import onnx
    import onnxruntime  # noqa: F401
    from onnx import TensorProto as T, helper
    HAVE_ORT = True
except ImportError:
    if os.environ.get("CI"):
        raise RuntimeError("onnx and onnxruntime are required under CI: these "
                           "tests load real models")
    HAVE_ORT = False

needs_ort = pytest.mark.skipif(not HAVE_ORT, reason="needs onnx and onnxruntime")


def model(out_shape=(-1, 1), window=16, width=96, value=0.25, in_type=None,
          extra_input=False) -> bytes:
    """A classifier that answers `value` for every frame, in the given shapes."""
    in_type = T.FLOAT if in_type is None else in_type
    nodes = [helper.make_node("Cast", ["input"], ["f"], to=T.FLOAT),
             helper.make_node("ReduceMean", ["f"], ["m"], axes=[1, 2], keepdims=0),
             helper.make_node("Mul", ["m", "zero"], ["z"]),
             helper.make_node("Add", ["z", "value"], ["a"]),
             helper.make_node("Unsqueeze", ["a", "ax"], ["u"]),
             helper.make_node("Tile", ["u", "reps"], ["t"]),
             helper.make_node("Reshape", ["t", "shape"], ["output"])]
    per_frame = int(np.prod([d for d in out_shape[1:]])) if len(out_shape) > 1 else 1
    init = [helper.make_tensor("zero", T.FLOAT, [], [0.0]),
            helper.make_tensor("value", T.FLOAT, [], [value]),
            helper.make_tensor("ax", T.INT64, [1], [1]),
            helper.make_tensor("reps", T.INT64, [2], [1, per_frame]),
            helper.make_tensor("shape", T.INT64, [len(out_shape)], list(out_shape))]
    ins = [helper.make_tensor_value_info("input", in_type, ["batch", window, width])]
    if extra_input:
        ins.append(helper.make_tensor_value_info("state", T.FLOAT, [1]))
        nodes.append(helper.make_node("Identity", ["state"], ["state_out"]))
    outs = [helper.make_tensor_value_info("output", T.FLOAT, ["batch", *out_shape[1:]])]
    if extra_input:
        outs.append(helper.make_tensor_value_info("state_out", T.FLOAT, [1]))
    g = helper.make_graph(nodes, "classifier", ins, outs, init)
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8)
    onnx.checker.check_model(m)
    return m.SerializeToString()


@needs_ort
@pytest.mark.parametrize("kw", [
    {},                                 # openWakeWord's own shape
    {"out_shape": (-1, 1, 1)},          # nanowakeword's (#869)
    {"window": 28},                     # controller-side only
    {"value": 0.0}, {"value": 1.0},
])
def test_a_model_that_can_run_is_accepted(kw):
    assert chk.problem(model(**kw)) is None


@needs_ort
@pytest.mark.parametrize("data, says", [
    (b"not an onnx model", "not an ONNX model"),
    (b"\x00" * 4096, "not an ONNX model"),
])
def test_bytes_that_are_not_a_model_are_refused(data, says):
    assert says in chk.problem(data)


@needs_ort
@pytest.mark.parametrize("kw, says", [
    ({"out_shape": (-1, 2)}, "[N, 2]"),
    ({"width": 64}, "[N, 16, 64]"),
    ({"in_type": 11}, "tensor(double)"),          # TensorProto.DOUBLE
    ({"extra_input": True}, "2 inputs"),
    ({"value": 3.0}, "between 0 and 1"),          # logits, no sigmoid
    ({"value": -0.5}, "between 0 and 1"),
    ({"value": float("nan")}, "between 0 and 1"),
])
def test_a_model_that_cannot_run_is_refused_with_the_reason(kw, says):
    why = chk.problem(model(**kw))
    assert why and says in why, why


@needs_ort
def test_a_truncated_model_is_refused():
    good = model()
    assert "not an ONNX model" in chk.problem(good[: len(good) // 2])


@needs_ort
def test_every_single_score_stock_model_is_accepted():
    """Nothing openwakeword ships with one score may be turned away: four at
    [1, 16, 96] and weather at [1, 22, 96]. timer is its multi-class model
    ([1, 7]), which the controller never offered."""
    oww = pytest.importorskip("openwakeword")
    models = Path(oww.__file__).parent / "resources" / "models"
    shared = {"embedding_model", "melspectrogram", "silero_vad"}
    multi = {"timer_v0.1"}
    seen = 0
    for f in sorted(models.glob("*.onnx")):
        if f.stem in shared:
            assert chk.problem(f.read_bytes()), f.stem
            continue
        why = chk.problem(f.read_bytes())
        if f.stem in multi:
            assert why and "one score" in why, (f.stem, why)
        else:
            assert why is None, (f.stem, why)
            seen += 1
    if not seen:
        pytest.skip("no stock wake word models installed")


@needs_ort
def test_a_bytearray_is_read_as_the_model_it_holds():
    # What aiohttp's multipart reader returns. Passed to onnxruntime as it
    # came, every upload was refused as "not an ONNX model".
    assert chk.problem(bytearray(model())) is None


# ─── the upload handler asks before it writes ────────────────────────────────

@needs_ort
def test_the_endpoint_installs_what_can_run_and_nothing_else(tmp_path, monkeypatch):
    import asyncio

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    # Removed again once em_api is imported: left in sys.modules it stops the
    # importorskip("openwakeword") tests skipping (see _oww_stub.uninstall).
    import _oww_stub
    _oww_stub.install()
    try:
        import em_api
    finally:
        _oww_stub.uninstall()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    handler = em_api._post_oww_model_upload.__wrapped__   # past require_admin

    async def post(client, name, data):
        form = aiohttp.FormData()
        form.add_field("model", data, filename=name)
        r = await client.post("/upload", data=form)
        return r.status, await r.json()

    async def run():
        app = web.Application()
        app.router.add_post("/upload", handler)
        async with TestClient(TestServer(app)) as c:
            return [await post(c, "good.onnx", model()),
                    await post(c, "nano.onnx", model(out_shape=(-1, 1, 1))),
                    await post(c, "multi.onnx", model(out_shape=(-1, 2))),
                    await post(c, "text.onnx", b"not an onnx model")]

    good, nano, multi, text = asyncio.run(run())
    assert good[0] == 201 and good[1]["model"]["file"] == "good.onnx"
    assert nano[0] == 201
    assert multi[0] == 400 and multi[1]["code"] == "invalid_model"
    assert "[N, 2]" in multi[1]["error"]
    assert text[0] == 400 and text[1]["code"] == "invalid_model"
    assert sorted(p.name for p in (tmp_path / "oww_models").iterdir()) == [
        "good.onnx", "nano.onnx"]



def test_the_upload_is_checked_before_anything_is_written():
    src = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    body = src[src.index("async def _post_oww_model_upload"):
               src.index("async def _delete_oww_model")]
    assert body.index("em_oww_check.problem, data") < body.index("mkstemp")
    assert '"invalid_model"' in body
