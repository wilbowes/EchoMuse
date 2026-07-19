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

### Pronunciation & accents

The synthetic positives come from an **American** TTS corpus (LibriTTS-R),
and phrases are phonemized with US readings — "hey clara" trains on the
US vowel, not a British "clar-ra". Three levers, in increasing strength:

1. **Phonetic spelling variants** — `target_phrase` accepts multiple
   entries that train *one* model firing on any of them. Comma-separate them
   at creation time (`forge.py new "hey clara, hey clarra"` or in the UI's
   phrase field) and cover how your household actually says it. Trade-off
   observed in practice: covering two pronunciation clusters with the same
   small classifier makes the auto-trainer more conservative (the two-spelling
   `hey_clarra` trained to *zero* false positives/hour but lower recall than
   single-spelling `hey_clara`, 0.43 vs 0.52 on the augmented test set) —
   if a variant model feels deaf, add real recordings and retrain, or lower
   the device's `owwThreshold` a notch.
2. **Google TTS mix-in** — defaults to `en-US,en-GB,en-AU` voices, so a
   `google-tts` pass before build adds genuinely British/Australian
   synthetic speakers (`--languages en-GB,en-AU` to skip the US ones).
3. **Real recordings** (best) — the UI's "+ Recordings…" button (or dropping
   16kHz wavs into `positive_train/`) adds actual samples of you and the
   kids to the training set; any phone recording format works (ffmpeg
   converts). Even 20–50 real clips measurably pull the model toward the
   voices that matter. They're augmented with reverb/noise like everything
   else, and displace synthetic clips rather than growing the set.

### Testing a built model

Three ways: the UI's **🎤 Record test** (browser mic → score; needs
HTTPS or localhost for mic permission), **Test file…** (upload any audio
file), or `forge.py test <name> --wav <files-or-dir>`. Scores near 1.0 on
your voice and near 0.0 on ordinary speech are what you want; the
controller's default threshold is ~0.5.

### Google TTS positives (optional)

`forge.py google-tts <name>` synthesizes the phrase across all premium Google
voices (Neural2/Studio/WaveNet/Chirp, en-US/GB/AU by default) with
rate/pitch variation, and drops the clips into the same positive train/test
dirs — the subsequent piper generation counts them toward `n_samples`, so
you get a mixed-family training set at no extra training cost. Piper remains
the volume source; Google adds acoustic character a single TTS family can't.

Setup: create a GCP service account with the Text-to-Speech API enabled, save
the JSON key as `./data/google-credentials.json`. **Usually free**: the API's
always-free tier covers ~1M premium-voice characters/month and a 2,000-clip
wake-word run is ~25k characters (~2% of it). Past the free tier it's ~$16/1M
chars; the command prints an estimate and asks before running.

## Installing a model into EchoMuse

Use the dashboard: **Config tab → Wake word → “+ Custom model”** and pick
the `.onnx` from `data/models/`. The upload lands in the controller's
persisted data volume (`oww_models/` beside the SQLite DB, so it survives
image upgrades), the model appears as a tile alongside the stock ones and
is auto-selected for the device you uploaded from. The OWW listener
hot-reloads on config change (same path as switching stock models), and
the ESPHome layer pushes the new wake-word name to Home Assistant
automatically. A custom tile's `×` deletes the file (refused while any
device or the global default still selects it).

Equivalent API (`em_api.py`):

```bash
curl -X POST http://<controller>:8768/api/oww_models/upload \
     -H "Authorization: Bearer <token>" \
     -F model=@data/models/hey_biscuit.onnx
# → {"model": {"name": "hey_biscuit", "path": "/app/data/oww_models/hey_biscuit.onnx", …}}
# then set owwModel to that path via /api/devices/<id>/config or the global config
```

Dropping a file into `controller/data/oww_models/` by hand works too —
`GET /api/oww_models` scans the directory per request, so it shows up on
the next dashboard load.

`owwModel` stores the **file path** for custom models (stock models stay
plain names). Note openwakeword keys its prediction dict by the filename
*stem*, not the path — the controller maps path → stem everywhere it reads
scores (`em_oww_models.prediction_key`), so keep filenames unique.

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
