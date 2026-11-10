# Audio bench

Replays recorded captures through the listening pipeline and scores them the
same way every time, so a change can be compared with a stored baseline
(#892). No Echo is needed to run it.

A capture is what `device/tools/capture_mics` writes: raw S24_3LE, 9 channels
at 16 kHz (seven microphones, then the playback loopback). Each one goes
through the firmware's own front end, `device/tools/aec_replay`, built for
this machine, and the output is scored.

## Run

Build the front end once, in the compiler image:

```sh
docker run --rm -v "$PWD":/src:ro -v /somewhere/bin:/out -w /src/device \
  -e GOARCH=amd64 -e GOOS=linux -e CGO_ENABLED=1 -e CC=gcc -e GOFLAGS=-mod=mod \
  echomuse-compiler go build -o /out/aec_replay ./tools/aec_replay
```

Then (needs numpy and openwakeword):

```sh
python bench.py run CORPUS --replay /somewhere/bin/aec_replay --out new.json --baseline base.json
python bench.py compare base.json new.json
```

With a baseline, the run exits 1 if any measure is worse or missing. Rebuild
`aec_replay` from the tree under test: that build is the thing being measured.

## Corpus

A folder with `corpus.json` beside the captures:

```json
{"name": "…", "captures": [
  {"id": "quiet-1m", "file": "quiet-1m.raw", "scenario": "normal voice 1 m",
   "model": "hey_jarvis_v0.1", "wakes": 9},
  {"id": "room", "file": "room.raw", "scenario": "silent room", "speech_free": true},
  {"id": "over-reply", "file": "c2.raw", "scenario": "speech over playback",
   "mics": "0:6,12.6:2", "gain_db": 24}
]}
```

- `wakes`: how many wake words were spoken. Gives `recall`; anything over it
  counts as a false wake.
- `speech_free`: every detection is a false wake, reported per hour.
- `mics`, `per_mic`, `gain_db`: passed to `aec_replay` (`-mics`, `-per-mic`,
  `-gain`). The default is the centre microphone at 24 dB.

Recordings of people are voices. Keep those corpora out of the repository.

## Measures

| Measure | What it is |
|---|---|
| `wake.detections`, `recall` | Wake words detected by the device's rules (0.5, or two frames at 0.25 over playback, 2 s refractory), averaged over 8 positions of the 80 ms frame |
| `wake.false_per_hour` | Detections beyond `wakes`, or all of them on speech-free audio |
| `echo.median_db`, `first_second_db` | Level removed between the front end's input and output while playback was present. Every replay starts cold |
| `levels.*` | Centre microphone floor and loud level, spread across the seven, samples at full scale |

| `cpu.*` | What a stage costs on an Echo: percent of one core, latency and frames that overran, read from a device probe's output |

Tolerances are in `metrics.CHECKS`.

## When to run it

On a change to what the microphones feed (`device/internal/aec`,
`beamformer`, `wakeword`, `processor`, `bindings/mic`): run main and the
branch over the same corpus and put `bench.py compare` in the pull request.
The playback side has its own check, which CI runs on every change
(`device/internal/bindings/speaker/quality_test.go`).

## Cost on the Echo

Quality is scored on the host; cost has to be measured on an Echo, because
its A53 is about 30 times slower and nothing here predicts it. Run a device
probe at the real duty cycle with the firmware stopped (`device/tools/
oww_probe` for the wake word), save what it prints, and hand it to the run:

```sh
python bench.py run CORPUS --replay BIN --cpu wake=oww_probe.txt --out new.json
```

It is stored as `cpu:wake` and compared like any other measure, so a change
shows what it bought and what it cost in one table. A report with no CPU line
is refused.

## Limits of this first version

- Wake words closer together than the 2 s refractory are counted once, as the
  device would. Space them 3 s apart when recording.
- No speech gate, endpointing or STT measures yet, and no false-wake hours.
- `test_metrics.py` covers the arithmetic: `python -m pytest test_metrics.py`.
