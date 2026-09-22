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
survives container restarts. While a build runs, the card's stepper shows
how far the current stage is: clips generated, feature files finished, or
training steps parsed from the log across all three of `train.py`'s
sequences. Light and dark follow the dashboard, sharing
its `em-theme` setting. No auth — LAN tool.

A run can be **stopped** from the console bar, its settings changed under
*training settings*, and started again. Stopping keeps everything already
produced: clip generation is resumable and both later steps check what
exists, so the loop is stop, adjust, train again rather than start over. The
stop kills the whole process group, because `forge.py` is only the parent —
openWakeWord's `train.py` underneath it is what holds the GPU, and killing
the parent alone leaves that running behind a UI that says it stopped.

Note the credentials endpoint writes a Google service-account key, and there
is no auth in front of it. That is the same trust model as everything else
here (the UI can already delete training data and run arbitrary jobs), but
it is a secret at rest now, so keep the port on the LAN.

## Quickstart — published image

```bash
cd oww_forge
docker compose -f docker-compose.deploy.yml up -d forge-ui   # http://<host>:8769
```

Two images, and picking the wrong one costs a night:

| tag | torch | platforms | for |
|-----|-------|-----------|-----|
| `:latest` | CUDA 12.8 | linux/amd64 | a machine with an NVIDIA GPU |
| `:latest-cpu` | CPU | linux/amd64, linux/arm64 | everything else, including Apple Silicon |

On Apple Silicon use `-cpu` and the `forge-ui-cpu` / `forge-cpu` services. The
arm64 image runs natively instead of under emulation, which is the difference
between the overnight CPU run described below and one nobody would sit
through. It is **still CPU training**: Docker on macOS runs containers in a
Linux VM with no Metal passthrough, so there is no GPU in there to reach. Using
the M-series GPU means running the trainer outside Docker against torch's MPS
backend, which is not what this ships.

Prefer the published image unless you are changing the trainer itself. Every
pin in the Dockerfile is load-bearing, and a local build re-resolves the
floating layers underneath them on every run; the published image is the only
artifact that preserves what was actually verified.

## Quickstart — local build

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
- Host **without** it, web UI: `docker compose up -d forge-ui-cpu` (the same
  service minus the GPU reservation; naming it starts its `cpu` profile). CLI:
  `docker compose run --rm forge-cpu …`. Both work from the ~10GB CUDA image;
  build with `GPU=0 docker compose build forge-ui-cpu` for a ~3GB CPU-only
  image instead. With the published image, the same services are in
  `docker-compose.deploy.yml` on the `:latest-cpu` tag.

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
2. **Piper voices in another accent or language** (local, free) — the
   `+ Accents & languages` button, or `forge.py piper-voices <name>
   --language en_GB`. Piper publishes voices in **55 languages** and the
   catalogue is read at runtime, so this is not an English-only lever.
   Where a language has a multi-speaker voice it is chosen automatically,
   because speaker identity is what buys variety: en_US 904 speakers,
   de_DE 236, fr_FR 125, en_GB 109, vi_VN 65. `forge.py voices` lists what
   is available; the first run downloads the voice (20–80MB) and keeps it.

   Note the wake-word model still rests on openWakeWord's frozen **English**
   speech embedding, so a wake word in another language is not as well
   served by the rest of the pipeline as an English one.
3. **Google TTS mix-in** — defaults to `en-US,en-GB,en-AU` voices, so a
   `google-tts` pass before build adds genuinely British/Australian
   synthetic speakers (`--languages en-GB,en-AU` to skip the US ones).
4. **Real recordings** (best) — the UI's "+ Recordings…" button (or dropping
   16kHz wavs into `positive_train/`) adds actual samples of you and the
   kids to the training set; any phone recording format works (ffmpeg
   converts). Even 20–50 real clips measurably pull the model toward the
   voices that matter. They're augmented with reverb/noise like everything
   else, and displace synthetic clips rather than growing the set.

### Hearing the phrase before training on it

Spelling variants only pay off if they read the way you intended, and until
2026-08-20 the only way to find out was to generate 30,000 clips and train
on them. **Hear each spelling** (when creating a wake word) speaks the
variants in order, and each spelling on an existing wake word is clickable.
The speaker is held fixed, so two spellings can be compared without the
voice changing underneath the comparison. It uses the same Piper voices as
the accent lever above, so the first press downloads one.

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

**It will be rate-limited, and that is handled.** Google refuses requests at
any real concurrency — measured at 1388 of 2000 clips on one run — so
transient failures back off and retry the same request five times, jittered,
at four workers. A run of a few thousand clips therefore takes minutes
rather than seconds, and the log distinguishes errors absorbed by retrying
from clips actually abandoned. Do not read "absorbed N transient errors" as
a fault; it means the quota was hit and the retries covered it.

Two related behaviours worth knowing: a voice is only retired from a run for
a **permanent** refusal (Chirp rejects `pitch`, for instance, and is then
asked without it), never for a transient one; and the working request shape
is remembered per voice, which removes about a third of the API calls.

Setup, from the web UI: **Google voices** on the left, then upload (or paste)
the JSON key and press **Test connection**. The key is written to
`./data/google-credentials.json` with mode 600 and picked up by the next job
with no container restart, so the compose mapping is no longer something you
have to arrange yourself. Test connection is worth pressing: the usual
failure is a perfectly valid key on a project where the Text-to-Speech API
was never enabled, and nothing local can see that.

Setup, by hand: create a GCP service account with the Text-to-Speech API
enabled, save the JSON key as `./data/google-credentials.json`. Note this
must be a **service account** key, not an OAuth client secret — the UI
rejects the latter, but the CLI will only fail later, inside a job.
**Usually free**: the API's
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

## Model metadata

New ONNX exports carry `wake_word` (the first `target_phrase`), the complete
`target_phrases` list, `language`, `trained_by`, and a UTC `training_date`.
Set `language` in the training config before building a non-English model.
`recommended_threshold` is optional and advisory: it does not change a device's
configured threshold. If the build environment supplies `FORGE_VERSION`, that
value is recorded as `oww_forge_version`; an unknown version is omitted.

The controller uses the metadata name and language when advertising the wake
word to Home Assistant. A voice turn uses the same name that its HA connection
advertised; button turns still carry no wake phrase. Model IDs and prediction
keys remain filename-based. Replacing a model invalidates the metadata cache,
and a config refresh reconnects HA if its name or language changed, even if
the filename did not.

Stock models and older or third-party files without metadata retain the existing
filename-derived name and English language fallback. Unreadable optional metadata
also falls back, with a warning. Publication preserves other ONNX metadata and
uses an atomic replacement, so a failed stamp cannot replace a finished model.
The exported file's hash changes, causing normal model asset synchronization.

This exporter publishes ONNX only. Converting it separately to TFLite does not
preserve these ONNX fields; metadata must be carried by that converter if a
future consumer needs it.

The real export/read/inference regression runs in the separate CI job:
`python -m pytest oww_forge/tests/` (pytest, numpy, pyyaml, onnx and onnxruntime).
The normal lightweight controller suite tests fallback and refresh behavior
without importing runtime dependencies.

## Layout

```
oww_forge/
  Dockerfile           pinned training environment (openWakeWord + piper + deps)
  docker-compose.yml   forge-ui (web) + forge/forge-cpu (CLI) services
  forge.py             CLI: assets | new | voices | piper-voices | google-tts |
                            build | test | ui
  forge_web.py         aiohttp web UI (port 8769) — thin layer over forge.py
  static/index.html    the web frontend (single file, no build step)
  google_tts.py        Google Cloud TTS positive-sample generator
  piper_voices.py      Piper ONNX voices — accents/languages, and the phrase
                       preview; catalogue fetched, never hardcoded
  docker-compose.deploy.yml   pulls the published image instead of building
  config.template.yml  per-wake-word training config template
  data/                (gitignored) assets, per-word workdirs, finished models
```
