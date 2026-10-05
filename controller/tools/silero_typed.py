"""Rewrite silero_vad.onnx with every tensor in its typed field, not raw_data.

ORT 1.19 on armv7 takes SIGBUS (BUS_ADRALN) in CreateSession on the model as
openwakeword ships it: raw_data sits at arbitrary offsets in the protobuf, and
rewriting the int64 tensors alone was not enough. Output is bit-identical.

    python3 silero_typed.py <silero_vad.onnx> <out.onnx>

Run at image build time (Dockerfile); the result is distributed to devices
with the wake word assets (em_oww_assets.VAD_NAME).
"""
import sys

import onnx
from onnx import numpy_helper


def fix(t):
    if not t.raw_data:
        return
    a = numpy_helper.to_array(t).flatten().tolist()
    t.ClearField("raw_data")
    if t.data_type == onnx.TensorProto.INT64:
        t.int64_data.extend(a)
    elif t.data_type == onnx.TensorProto.FLOAT:
        t.float_data.extend(a)
    else:
        raise SystemExit(f"unhandled tensor type {t.data_type} in {t.name}")


def walk(g):
    for t in g.initializer:
        fix(t)
    for n in g.node:
        for a in n.attribute:
            if a.type == onnx.AttributeProto.TENSOR:
                fix(a.t)
            if a.type == onnx.AttributeProto.GRAPH:
                walk(a.g)
            for sg in a.graphs:
                walk(sg)


m = onnx.load(sys.argv[1])
walk(m.graph)
onnx.save(m, sys.argv[2])
