"""Encode a known signal with aiosendspin's own FlacEncoder — the encoder Music
Assistant streams with — so codec_test.go decodes exactly what the device will
receive. Writes flac_chunks.json: the codec_header, each chunk, and the PCM.

  docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
    'pip -q install "aiosendspin[server]==9.1.1" && python gen_flac.py'
"""

import base64
import json
import math
import struct

from aiosendspin.audio.codecs import FlacEncoder

RATE = 48000
enc = FlacEncoder(sample_rate=RATE, bit_depth=16, channels=1)
n = RATE // 2  # half a second
samples = [int(12000 * math.sin(2 * math.pi * 440 * i / RATE) + 3000 * math.sin(2 * math.pi * 3100 * i / RATE)) for i in range(n)]
pcm = struct.pack(f"<{n}h", *samples)
chunks = []
step = RATE // 40  # 25ms of input per call, as the server feeds it
for off in range(0, n, step):
    part = pcm[off * 2:(off + step) * 2]
    for data, ts in enc.process(part, off * 1_000_000 // RATE, len(part) // 2 * 1_000_000 // RATE):
        chunks.append({"ts": ts, "data": base64.b64encode(data).decode()})
for data, ts in enc.flush():
    chunks.append({"ts": ts, "data": base64.b64encode(data).decode()})
json.dump({
    "header": base64.b64encode(enc.get_codec_header()).decode(),
    "chunks": chunks,
    "pcm": base64.b64encode(pcm).decode(),
}, open("flac_chunks.json", "w"))
print(len(chunks), "chunks")
