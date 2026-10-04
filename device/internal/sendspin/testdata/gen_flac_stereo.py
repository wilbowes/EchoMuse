"""gen_flac.py in stereo (#273): aiosendspin's own FlacEncoder, the one Music
Assistant streams with, on two DIFFERENT channels, so the encoder picks its
inter-channel modes (left/side, side/right, mid/side) and the decoder has to
undo them. Writes flac_chunks_stereo.json: the codec_header, each chunk, and
the interleaved PCM.

  docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
    'pip -q install "aiosendspin[server]==9.1.1" && python gen_flac_stereo.py'
"""

import base64
import json
import math
import struct

from aiosendspin.audio.codecs import FlacEncoder

RATE = 48000
enc = FlacEncoder(sample_rate=RATE, bit_depth=16, channels=2)
n = RATE // 2  # half a second


def left(i):
    return int(12000 * math.sin(2 * math.pi * 440 * i / RATE) + 3000 * math.sin(2 * math.pi * 3100 * i / RATE))


def right(i):
    # Correlated with the left for the first half (mid/side pays), then a
    # different tone (it does not), so more than one channel mode is used.
    if i < n // 2:
        return int(0.8 * left(i)) + int(900 * math.sin(2 * math.pi * 5200 * i / RATE))
    return int(-9000 * math.sin(2 * math.pi * 660 * i / RATE))


frames = []
for i in range(n):
    frames += [left(i), right(i)]
pcm = struct.pack(f"<{2 * n}h", *frames)
chunks = []
step = RATE // 40  # 25ms of input per call, as the server feeds it
for off in range(0, n, step):
    part = pcm[off * 4:(off + step) * 4]
    for data, ts in enc.process(part, off * 1_000_000 // RATE, len(part) // 4 * 1_000_000 // RATE):
        chunks.append({"ts": ts, "data": base64.b64encode(data).decode()})
for data, ts in enc.flush():
    chunks.append({"ts": ts, "data": base64.b64encode(data).decode()})
json.dump({
    "header": base64.b64encode(enc.get_codec_header()).decode(),
    "chunks": chunks,
    "pcm": base64.b64encode(pcm).decode(),
}, open("flac_chunks_stereo.json", "w"))
print(len(chunks), "chunks")
