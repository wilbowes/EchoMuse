"""Exercise the controller's native dependencies on whatever CPU runs this.

Run by cpu_baseline_check.sh under `qemu-x86_64 -cpu kvm64`, inside the
built image. See that script for why; this file is only the payload.

Three parts, and the order matters:

1. Import numpy, then confirm the CPU really is the emulated one. numpy's
   own runtime CPU detection reads CPUID, so under a genuine kvm64 it must
   report no SSE4.1/SSE4.2/POPCNT/AVX2. If it reports any of them, qemu
   ignored `-cpu` and every pass below would be a pass on the host CPU,
   so that is a failure, not a warning.
2. Import every controller module, the same set CI's import job uses, so
   a dependency added tomorrow is covered on the day it lands.
3. Actually run the code paths whose wheels carry compiled kernels: a
   wake word prediction (onnxruntime + openwakeword's numpy features), the
   output chain (scipy), the noise suppressor. Importing a module does
   not execute its SIMD kernels, so a clean import proves less than it
   looks like it does.
"""
import importlib
import pathlib
import sys
import traceback

import numpy as np
from numpy._core._multiarray_umath import __cpu_features__ as feats

EXPECT_ABSENT = ("SSE41", "SSE42", "POPCNT", "AVX2")
present = [f for f in EXPECT_ABSENT if feats.get(f)]
if "--kvm64" in sys.argv and present:
    sys.exit(f"harness fault: CPU reports {present}, so this is not kvm64 "
             f"and a pass would prove nothing")
print(f"numpy {np.__version__}; CPU reports "
      f"{', '.join(f for f in EXPECT_ABSENT if f not in present) or 'all'} "
      f"absent of {', '.join(EXPECT_ABSENT)}")

sys.path.insert(0, "/app")
mods = sorted(p.stem for p in pathlib.Path("/app").glob("*.py"))
mods = [m for m in mods if m != "em_start"]
failed = []
for m in mods:
    try:
        importlib.import_module(m)
    except BaseException:
        failed.append(m)
        print(f"--- {m} ---", file=sys.stderr)
        traceback.print_exc()
if failed:
    sys.exit("FAILED to import: " + ", ".join(failed))
print(f"imported {len(mods)} controller modules")

from openwakeword.model import Model  # noqa: E402

model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
pcm = (np.random.default_rng(0).standard_normal(16000 * 3) * 3000).astype(np.int16)
for i in range(0, len(pcm) - 1280, 1280):
    model.predict(pcm[i:i + 1280])
print("wake word prediction ran")

import em_eq  # noqa: E402
import scipy.signal as ss  # noqa: E402

sos = ss.butter(4, [100, 8000], "bandpass", fs=48000, output="sos")
ss.sosfilt(sos, np.random.default_rng(0).standard_normal(48000))
print(f"scipy filter ran; em_eq loaded from {em_eq.__file__}")

from speexdsp_ns import NoiseSuppression  # noqa: E402

ns = NoiseSuppression.create(160, 16000)
ns.process(bytes(320))
print("speexdsp noise suppression ran")
print("OK")
