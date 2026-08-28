# capture_mics — raw mic array capture

## What this is for

Proves a mic PCM actually captures real audio — no digital zeros, no ALSA
errors — before any binding code is written for it. Originally built for
biscuit's 9-channel array (channel-mapping to physical mic position, paired
with `analyse_capture.py` — see `../README.md`); parametrized 2026-08-26 with
`-card`/`-device`/`-channels` flags so the same binary targets crown's
6-channel array (`card0,device22`) without a forked copy.

Used on real crown hardware 2026-08-26 to answer wilbowes/EchoMuse#322's
mic-bring-up question: is capture a config change or unsolved R&D? Result:
config change — see `docs/echo-show-8-hardware-map.md`.

## Why this tool and not `tinycap`

`tinycap` can't request `S24_3LE` (3-byte-packed 24-bit), the only format
these mic PCMs accept on either board. This uses the GoTinyAlsa binding
directly, the same one the real firmware uses, so a pass here means the real
binding will work too — not just that *some* tool can open the device.

## Build

Inside the `echomuse-compiler` Docker container:

```bash
cd device
REPO_ROOT=$(git rev-parse --show-toplevel)
docker run --rm \
  --entrypoint bash \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -v "$(pwd)":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  echomuse-compiler \
  -c "cd /sdk/tools/capture_mics && go build -tags server -o capture_mics ."
```

## Run

Needs root (the mic PCM node is `system:audio`-owned; `adb shell` starts as
uid 2000 `shell`) — `adb root` first.

```bash
adb push capture_mics /data/local/tmp/capture_mics
adb shell chmod 755 /data/local/tmp/capture_mics

# biscuit (defaults — card 0, device 24, 9ch)
adb shell /data/local/tmp/capture_mics 5

# crown — flags MUST come before the seconds argument (Go's flag package
# stops parsing at the first non-flag token)
adb shell /data/local/tmp/capture_mics -card 0 -device 22 -channels 6 5

adb pull /data/local/tmp/capture.raw
```

Output: raw interleaved S24_3LE at 16kHz, N channels — `N * 3` bytes/frame.

## Reading the result

A real capture has non-zero RMS per channel and no `ALSA stream error` in the
output. Quick per-channel RMS/peak check (adjust `channels`):

```python
import struct, math
data = open("capture.raw", "rb").read()
channels = 6
frame = channels * 3
n = len(data) // frame
def s24(b):
    v = b[0] | (b[1]<<8) | (b[2]<<16)
    return v - 0x1000000 if v & 0x800000 else v
for c in range(channels):
    vals = [s24(data[i*frame+c*3:i*frame+c*3+3]) for i in range(n)]
    rms = math.sqrt(sum(v*v for v in vals)/n)
    print(f"ch{c}: rms={rms:.0f} ({20*math.log10(rms/2**23):.1f} dBFS)")
```

A channel reading exact 0 (not just quiet) across the whole capture is a real
signal-path problem, not noise — worth distinguishing "unused slot" from
"reference channel" before assuming a bad capsule.
