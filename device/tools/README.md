# capture_mics — Mic Array Channel Mapping Tool

Moved to [`capture_mics/README.md`](capture_mics/README.md) — now covers
both biscuit's 9ch array and crown's 6ch array via `-card`/`-device`/
`-channels` flags, superseding the fixed-9ch build command that used to be
documented here.

`analyse_capture.py` (in this directory) is shared by both boards and still
applies as documented there.

---

# oww_probe — on-device wake word verification and cost

Answers the two questions the host tests cannot: does the ONNX Runtime
binding actually work on this hardware, and what does it cost in the shape it
will really run?

It touches no microphone, no LEDs and not the running server — it reads three
model files, a fixture and `libonnxruntime.so`, and prints. Safe on a live
device, though phase 2 competes for CPU with whatever else is running, which
is deliberate.

## What it checks

**Phase 1 — correctness.** Runs `fixture.Verify`, the *same* comparison the
host test runs, against the same golden capture of openWakeWord's Python
output (`internal/wakeword/testdata/ort_fixture.bin`). Compares
melspectrogram output, embeddings and scores per 80ms chunk, plus a probe
block covering the classifier with a wide-range tensor — necessary because
the fixture's synthetic audio scores 0.0000 everywhere, so the audio path
alone would also pass with a classifier stuck at zero. A pass means the real
pipeline reproduces Python **on ARM**, not merely on x86.

**Phase 2 — cost.** Paces frames at the real 12.5Hz duty cycle and reports
process CPU from `getrusage`. Flat-out latency is the misleading number here:
ORT's thread pool can burn several times the inference cost spin-waiting in
the 60ms gaps, and this also captures what a C benchmark cannot — cgo call
overhead and Go GC pressure from the buffering.

Exit status is 0 only if every stage matched Python.

## Deploy

Unlike the other tools it is not a standalone module (it imports
`internal/wakeword`, which Go's internal rule only allows from inside the
module), so `build_tools.sh` builds it with the whole device module mounted.

Needs four things on the device besides the binary: `melspectrogram.onnx`,
`embedding_model.onnx`, a classifier `.onnx`, `ort_fixture.bin`, and the
armeabi-v7a `libonnxruntime.so` (12.3MB, from the Maven
`onnxruntime-android` AAR under `jni/armeabi-v7a/`).

Without adb, push over the controller shell plane:

```bash
docker cp controller/tools/push_file.py echomuse-controller:/tmp/
docker cp device/build/oww_probe echomuse-controller:/tmp/
docker exec echomuse-controller python /tmp/push_file.py \
    <device_id> /tmp/oww_probe /data/local/tmp/oww_probe --chmod 755
```

## Run

```bash
oww_probe -classifier /data/local/tmp/hey_mycroft_v0.1.onnx -seconds 30
```

Flags exist for `-threads`, `-xnnpack` and `-spinning` so the measured
optimum can be re-derived on the hardware rather than trusted: on an Echo Dot
Gen 2, one thread with XNNPACK and spinning **off** cost 36.2% of one core,
where ORT's defaults cost 243%.
