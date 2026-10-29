# Writes a deterministic test signal as a Sun .au file (16-bit big-endian),
# the input format sbcenc reads: two tones per channel plus a little noise.
import math, random, struct, sys

rate, ch, n, path = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
random.seed(rate * 10 + ch)
data = bytearray()
for t in range(n):
    for c in range(ch):
        v = (9000 * math.sin(2 * math.pi * 330 * (c + 1) * t / rate)
             + 3000 * math.sin(2 * math.pi * (rate / 16) * t / rate)
             + random.gauss(0, 400))
        data += struct.pack('>h', int(max(-32768, min(32767, v))))
hdr = b'.snd' + struct.pack('>IIIII', 24, len(data), 3, rate, ch)
open(path, 'wb').write(hdr + bytes(data))
