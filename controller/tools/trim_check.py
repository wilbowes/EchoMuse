#!/usr/bin/env python3
"""Does trimming openwakeword's raw-audio buffer change any score?

AudioFeatures keeps up to 10 s of raw audio (deque(maxlen=sr*10) =
160,000 Python ints) and, on every predict(), does
list(self.raw_data_buffer)[-n_samples-480:] — converting the whole deque
to a list to take the last 1,760 samples. Nothing else ever reads it
(grep raw_data_buffer in the installed package: lines 164, 174, 393, 397,
407 — the read is line 397 only).

This builds two models over identical audio, one with the buffer replaced
by a short deque, and compares every prediction. np.random is seeded
because openwakeword seeds its feature buffer with random noise in the
constructor; without the seed the two models start differently and any
difference measures that instead.

The controller trims through em_controller._trim_oww_raw_buffer (#176) to
the same 8*1280+480 samples used here. Needs openwakeword, which the
controller test venv does not install: run it where the controller's
dependencies are (the controller image, or any venv with openwakeword),
from a checkout:

    python controller/tools/trim_check.py
"""
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER))
import em_oww_models  # noqa: E402

PCM = CONTROLLER.parent / "device/internal/wakeword/testdata/vad_fixture.pcm"
CHUNK = 1280
FPS = 16000 / CHUNK
TRIMMED = 8 * CHUNK + 480          # 10,720 samples: 8 chunks + the mel tail


def model_class():
    """openwakeword only when actually needed, with a usable message."""
    try:
        return em_oww_models.model_class()
    except ImportError as exc:
        raise SystemExit(
            f"trim_check needs openwakeword ({exc}), which the controller test "
            "venv does not install — run it where the controller's dependencies "
            "are, e.g. the controller image"
        ) from exc


def frame_list(n):
    audio = np.frombuffer(open(PCM, "rb").read(), dtype=np.int16)
    audio = np.tile(audio, n // (len(audio) // CHUNK) + 1)
    return [np.frombuffer(audio[i * CHUNK:(i + 1) * CHUNK].tobytes(), dtype=np.int16)
            for i in range(n)]


def build(trim):
    cls = model_class()
    np.random.seed(0)
    m = cls(wakeword_models=["hey_jarvis"], enable_speex_noise_suppression=False)
    if trim:
        m.preprocessor.raw_data_buffer = deque(maxlen=TRIMMED)
    return m


fs = frame_list(1400)

stock = build(False)
trimmed = build(True)
print(f"stock buffer maxlen={stock.preprocessor.raw_data_buffer.maxlen}  "
      f"trimmed={trimmed.preprocessor.raw_data_buffer.maxlen}")

# Feed both models the same audio, interleaved so neither is favoured.
diffs = 0
worst = 0.0
N = 1200
for i, f in enumerate(fs[:N]):
    a = stock.predict(f)["hey_jarvis"]
    b = trimmed.predict(f)["hey_jarvis"]
    if i >= 16:                      # past the noise-seeded window
        d = abs(float(a) - float(b))
        worst = max(worst, d)
        if d != 0.0:
            diffs += 1
print(f"{N} frames scored twice: {diffs} differ, max |delta| {worst:.3e}")


def cpu(model, frames):
    t0 = time.process_time()
    for f in frames:
        model.predict(f)
    return (time.process_time() - t0) / len(frames) * 1000


for label, m in (("stock", build(False)), ("trimmed", build(True))):
    for f in fs[:300]:               # buffer full: the cost is length-driven
        m.predict(f)
    ms = cpu(m, fs[300:1300])
    print(f"{label:8s} {ms:6.3f} ms/frame  "
          f"{ms * FPS / 10:5.2f}% of one core per streaming Echo")
