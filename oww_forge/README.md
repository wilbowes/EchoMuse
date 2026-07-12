# oww_forge — custom wake-word trainer

Trains custom [openWakeWord](https://github.com/dscripka/openWakeWord) models
("hey biscuit", "computer", …) for EchoMuse, entirely from synthetic speech —
no recording sessions needed. Deliberately **separate from the controller**:
training is a heavy, occasional batch job with ~25GB of assets and a fat
PyTorch image, none of which belongs in the always-on controller container.

## How it works

The pipeline is openWakeWord's official automatic-training flow, containerised
and orchestrated by `forge.py`:

1. **Generate** — [piper-sample-generator](https://github.com/rhasspy/piper-sample-generator)
   (LibriTTS-R VITS, ~900 speakers) synthesizes tens of thousands of positive
   clips of the wake phrase, plus *adversarial negatives*: phrases chosen for
   phoneme overlap ("hey biscuit" → "hey bisque", "hay brisket") that teach
   the model precise boundaries. Optionally layered with Google Cloud TTS
   samples for extra voice diversity (see below).
2. **Augment** — clips are convolved with room impulse responses (MIT RIR
   dataset) and mixed with background noise/music (AudioSet + Free Music
   Archive), then converted to openWakeWord input features (melspectrogram →
   frozen Google speech embedding).
3. **Train** — a small classifier head (the same `dnn/32` architecture as the
   stock models) trains against the positives plus ~2,000 hours of
   precomputed negative features (ACAV100M), with false-positive validation
   against an 11-hour held-out set. Output: a single `.onnx` file, typically
   under 1MB — exactly what the controller's `OWWModel` loads.

Versions are pinned in the Dockerfile: openWakeWord @ `368c0371` (with a
one-line patch for its `--convert_to_tflite` argparse bug — string default
`"False"` is truthy, which would end every run importing TensorFlow),
piper-sample-generator @ `v2.0.0` (the last release with the flat layout
openWakeWord's `train.py` imports from — don't bump casually; its
`torch.load` is patched for torch ≥ 2.6's `weights_only` default flip).

## Quickstart — web UI

```bash
cd oww_forge
docker compose up -d --build forge-ui
# → http://<host>:8769
```

The UI covers the whole flow: asset download with live progress, wake-word
creation, build with a streaming log console, Google-TTS mix-in, wav-upload
testing, and `.onnx` download. One job runs at a time (training saturates
the machine anyway); state is derived from disk on every poll, so it
survives container restarts. No auth — LAN tool.

## Quickstart — CLI

```bash
cd oww_forge
docker compose build forge-ui   # same image serves both

# 1. one-time asset download (~25GB — see table below; ./data can be a
#    volume on any disk with room)
docker compose run --rm forge assets

# 2. create a wake word
docker compose run --rm forge new "hey biscuit"

# 3. (optional) mix in Google TTS positives — needs credentials, see below
docker compose run --rm forge google-tts hey_biscuit

# 4. train (GPU: ~1-2h; CPU: overnight)
docker compose run --rm forge build hey_biscuit

# 5. sanity-check against a recording
docker compose run --rm forge test hey_biscuit --wav /data/my_recording.wav
```

Result: `./data/models/hey_biscuit.onnx`.

Every stage is resumable: `assets` skips completed parts, clip generation
tops up to the target count, and `build --from-step augment|train` restarts
mid-pipeline.

## GPU / CPU

The default image builds **CUDA 12.8 torch 2.7.1**, which supports Blackwell
cards (RTX 50xx, sm_120) as well as older generations — note that
notebook-era torch 2.1/cu121 cannot drive an RTX 5060 Ti at all. Fallback is
automatic: if no CUDA device is visible at runtime, torch runs on CPU and
`forge.py build` logs which device it's using (the UI shows it in the header
badge).

- Host **with** the nvidia container runtime: use `forge` / `forge-ui` as-is.
- Host **without** it: `docker compose run --rm forge-cpu …` (same image, no
  GPU reservation), or build with `GPU: "0"` for a ~3GB CPU-only image
  instead of ~10GB.

### Asset sizes

| Asset | Size | Purpose |
|---|---|---|
| ACAV100M negative features | ~17GB | 2,000h of precomputed non-wake-word features |
| validation features | ~0.5GB | false-positive validation (11h speech/noise/music) |
| AudioSet (2,000 clips, streamed) | ~2GB | background noise for augmentation |
| FMA small (1,000 clips) | ~1GB | background music for augmentation |
| MIT RIRs | ~50MB | room reverb simulation |
| piper LibriTTS-R checkpoint | ~430MB | positive sample synthesis |

### Tuning knobs

`forge.py new` writes `data/wakewords/<name>/config.yml` — edit before
`build`. The interesting fields:

- `n_samples` — 30,000 default; 50,000–100,000 measurably helps difficult phrases.
- `custom_negative_phrases` — add real-world confusions you observe
  ("hey brisket") and retrain; the cheapest fix for false activations.
- `target_false_positives_per_hour` / `max_negative_weight` — the
  false-accept vs. false-reject trade; defaults match upstream guidance.
- Choose a **3-4 syllable phrase**; short words make weak wake words no
  matter how much data you throw at them.

### Google TTS positives (optional)

`forge.py google-tts <name>` synthesizes the phrase across all premium Google
voices (Neural2/Studio/WaveNet/Chirp, en-US/GB/AU by default) with
rate/pitch variation, and drops the clips into the same positive train/test
dirs — the subsequent piper generation counts them toward `n_samples`, so
you get a mixed-family training set at no extra training cost. Piper remains
the volume source; Google adds acoustic character a single TTS family can't.

Setup: create a GCP service account with the Text-to-Speech API enabled, save
the JSON key as `./data/google-credentials.json`. Cost is trivial (~$0.50 for
the default 2,000 clips); the command prints an estimate and asks before
spending.

## Installing a model into EchoMuse

The controller passes `owwModel` straight to
`OWWModel(wakeword_models=[...])`, which accepts **file paths** as well as
built-in model names. So today, without any controller changes:

```bash
# 1. copy the model into the controller's persisted data volume
mkdir -p ../controller/data/oww_models
cp data/models/hey_biscuit.onnx ../controller/data/oww_models/

# 2. point a device (or the global config) at it — the dashboard's wake-word
#    tiles are a fixed list, so set it via the API for now:
curl -X POST http://<controller>:8768/api/devices/<device_id>/config \
     -H "Content-Type: application/json" -b "session=<cookie>" \
     -d '{"owwModel": "/app/data/oww_models/hey_biscuit.onnx"}'
```

The OWW listener hot-reloads on config change (same path as switching stock
models), and the ESPHome layer pushes the new wake-word name to Home
Assistant automatically.

### Future controller integration (proposed, not built)

Kept out of this change on purpose; likely shape when we want it:

1. Controller scans `/app/data/oww_models/*.onnx` at startup/config-read and
   merges them into the model list the dashboard offers (label = filename).
2. Dashboard `WW_MODELS` tiles become `stock + discovered custom` instead of
   a hardcoded const, plus an upload button (POST multipart → the same dir —
   mirrors the existing firmware-upload endpoint pattern).
3. Optionally `forge.py install <name> --controller http://…` doing the
   copy+config POST in one step.

## Layout

```
oww_forge/
  Dockerfile           pinned training environment (openWakeWord + piper + deps)
  docker-compose.yml   forge-ui (web) + forge/forge-cpu (CLI) services
  forge.py             CLI: assets | new | google-tts | build | test | ui
  forge_web.py         aiohttp web UI (port 8769) — thin layer over forge.py
  static/index.html    the web frontend (single file, no build step)
  google_tts.py        Google Cloud TTS positive-sample generator
  config.template.yml  per-wake-word training config template
  data/                (gitignored) assets, per-word workdirs, finished models
```
