"""Writes tones.pk: 1 s of stereo Opus, 1 kHz on the left and 440 Hz on the
right at -12 dBFS, 128 kbps, 20 ms frames, encoded the way aiosendspin encodes
(PyAV's libopus). Each packet is a 2-byte big-endian length then the packet.

    docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
      'pip -q install av==19.0.2 numpy && python gen_fixture.py'
"""
import struct

import av
import numpy as np

RATE, FRAME, SECS = 48000, 960, 1.0
n = int(RATE * SECS)
t = np.arange(n) / RATE
amp = 10 ** (-12 / 20) * 32767
pcm = np.stack([amp * np.sin(2 * np.pi * 1000 * t), amp * np.sin(2 * np.pi * 440 * t)], axis=1)
pcm = pcm.round().astype(np.int16)

enc = av.AudioCodecContext.create("libopus", "w")
enc.sample_rate, enc.layout, enc.format = RATE, "stereo", "s16"
enc.bit_rate = 128000
enc.options = {"frame_duration": "20"}
enc.open()

out = bytearray()
count = 0
for i in range(0, n, FRAME):
    f = av.AudioFrame.from_ndarray(pcm[i:i + FRAME].reshape(1, -1), format="s16", layout="stereo")
    f.sample_rate, f.pts = RATE, i
    for p in enc.encode(f):
        out += struct.pack(">H", p.size) + bytes(p)
        count += 1
for p in enc.encode(None):
    out += struct.pack(">H", p.size) + bytes(p)
    count += 1
open("tones.pk", "wb").write(out)
print(f"{count} packets, {len(out)} bytes, pre-skip {enc.delay if hasattr(enc, 'delay') else '?'}")
