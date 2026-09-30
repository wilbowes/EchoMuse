"""Replay a synthetic NTP exchange through aiosendspin's time filter.

The Go port (timefilter.go) must agree with this to the microsecond, since the
spec requires the algorithm and a player out by a few ms is out of sync with
its group. Regenerate with:

  docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
    'pip -q install aiosendspin==9.1.1 && python gen_timefilter.py > timefilter_vectors.json'
"""

import json
import random

from aiosendspin.client.time_sync import SendspinTimeFilter

rng = random.Random(89)
f = SendspinTimeFilter()
out = []
t = 1_000_000
true_offset = 123_456_789.0
drift = 40e-6  # server clock 40ppm fast
for i in range(300):
    # Mostly a clean LAN, with WiFi-shaped spikes and one clock step.
    t += rng.randint(150_000, 3_100_000)
    rtt = rng.randint(1_500, 9_000) if rng.random() > 0.08 else rng.randint(50_000, 900_000)
    if i == 220:
        true_offset += 40_000  # a server clock adjustment
    off = true_offset + drift * t
    asym = rng.uniform(-0.4, 0.4) * rtt
    measurement = round(off + asym / 2)
    max_error = round(rtt / 2)
    f.update(measurement, max_error, t)
    probes = [t + rng.randint(0, 5_000_000) for _ in range(3)]
    server_probes = [p + round(off) for p in probes]
    out.append({
        "m": measurement, "e": max_error, "t": t,
        "sync": f.is_synchronized,
        "err": f.error if f.is_synchronized else None,
        "probes": probes,
        "server": [f.compute_server_time(p) for p in probes],
        "server_probes": server_probes,
        "client": [f.compute_client_time(p) for p in server_probes],
    })
print(json.dumps(out))
