"""Golden for ort.VAD: synthetic audio and Silero's per-frame probability.

Seeded noise with a 1.5s voice-like harmonic burst, so the fixture spans both
ends of the scale. No recorded speech: fixtures here are public.

    python3 gen_vad_fixture.py <path to openwakeword's silero_vad.onnx>
"""
import json
import sys

import numpy as np
import onnxruntime as ort

rng = np.random.default_rng(7)
sr = 16000
t = np.arange(sr * 4) / sr
x = 0.01 * rng.standard_normal(len(t))
seg = (t > 1.0) & (t < 2.5)
f0 = 140 + 20 * np.sin(2 * np.pi * 3 * t)
voice = sum((0.3 / k) * np.sin(2 * np.pi * k * np.cumsum(f0) / sr) for k in range(1, 12))
x[seg] += voice[seg] * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t[seg]))
pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)

s = ort.InferenceSession(sys.argv[1])
h = np.zeros((2, 1, 64), np.float32)
c = h.copy()
xf = pcm.astype(np.float32) / 32767
probs = []
for i in range(0, len(xf) - 1280 + 1, 1280):
    ps = []
    for j in (0, 640):
        out, h, c = s.run(None, {"input": xf[None, i + j:i + j + 640],
                                 "sr": np.array(16000, np.int64), "h": h, "c": c})
        ps.append(float(out[0][0]))
    probs.append(float(np.mean(ps)))

pcm.tofile("vad_fixture.pcm")
json.dump({"probs": probs}, open("vad_fixture.json", "w"))
