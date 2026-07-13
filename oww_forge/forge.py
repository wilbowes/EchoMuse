#!/usr/bin/env python3
"""oww_forge — train custom openWakeWord models for EchoMuse.

Runs inside the oww-forge container (see Dockerfile / docker-compose.yml).
Everything persistent lives on the /data volume:

    /data/assets/       shared training assets (downloaded once, ~25GB)
    /data/wakewords/    one directory per wake word (config + clips + features)
    /data/models/       finished .onnx models, ready to install

Typical flow:

    forge.py assets                    # one-time, ~25GB of downloads
    forge.py new "hey biscuit"         # writes wakewords/hey_biscuit/config.yml
    forge.py google-tts hey_biscuit    # OPTIONAL extra positives via Google TTS
    forge.py build hey_biscuit         # generate → augment → train → models/hey_biscuit.onnx
    forge.py test hey_biscuit --wav some_recording.wav
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("FORGE_DATA", "/data"))
ASSETS = DATA / "assets"
WAKEWORDS = DATA / "wakewords"
MODELS = DATA / "models"
FORGE_DIR = Path(__file__).parent
TRAIN_PY = "/opt/openwakeword/openwakeword/train.py"

PIPER_CKPT = ASSETS / "piper" / "en_US-libritts_r-medium.pt"
PIPER_CKPT_URL = (
    "https://github.com/rhasspy/piper-sample-generator/releases/download/"
    "v2.0.0/en_US-libritts_r-medium.pt"
)
# voice config expected at <ckpt>.json; lives in the repo (which the
# Dockerfile replaces with the /data symlink), not in the release assets
PIPER_CKPT_JSON_URL = (
    "https://raw.githubusercontent.com/rhasspy/piper-sample-generator/"
    "195e3bd967d54589c2137c9de2b22ad526ba6b6f/models/en_US-libritts_r-medium.pt.json"
)
FEATURES_DIR = ASSETS / "features"
HF_FEATURES_REPO = "davidscripka/openwakeword_features"
NEGATIVE_FEATURES = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALIDATION_FEATURES = "validation_set_features.npy"
RIR_DIR = ASSETS / "mit_rirs"
AUDIOSET_DIR = ASSETS / "audioset_16k"
# agkphysics/AudioSet stores bal_train as ~40 parquet shards (the old
# bal_trainNN.tar files the openWakeWord notebook used are gone). A handful
# of shards yields the ~2k clips the notebook worked with.
AUDIOSET_PARQUET_URLS = [
    f"https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{i:02d}.parquet"
    for i in range(0, 4)
]
FMA_DIR = ASSETS / "fma_16k"

ASSET_PARTS = ["piper", "features", "rirs", "audioset", "fma"]


def log(msg: str) -> None:
    print(f"[forge] {msg}", flush=True)


def slugify(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def download(url: str, dest: Path) -> None:
    """Plain HTTP download with a .part temp file so interrupts don't leave
    a truncated file that idempotency checks would then treat as complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url}")

    def hook(blocks, block_size, total):
        done = blocks * block_size
        if total > 0 and blocks % 2000 == 0:
            log(f"  …{done / 1e6:.0f} / {total / 1e6:.0f} MB")

    urllib.request.urlretrieve(url, part, reporthook=hook)
    part.rename(dest)
    log(f"  → {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def write_wav_16k(dest: Path, audio, sr: int) -> None:
    import librosa
    import numpy as np
    import soundfile as sf

    audio = np.asarray(audio, dtype="float32")
    if audio.ndim > 1:  # stereo → mono
        audio = audio.mean(axis=1)
    if sr != 16000:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    sf.write(dest, audio, 16000, subtype="PCM_16")


def dir_has_files(path: Path, pattern: str = "*") -> bool:
    return path.is_dir() and next(path.glob(pattern), None) is not None


# ---------------------------------------------------------------- assets

def fetch_piper() -> None:
    if PIPER_CKPT.exists():
        log(f"piper checkpoint present: {PIPER_CKPT}")
    else:
        download(PIPER_CKPT_URL, PIPER_CKPT)
    ckpt_json = PIPER_CKPT.with_suffix(".pt.json")
    if not ckpt_json.exists():
        download(PIPER_CKPT_JSON_URL, ckpt_json)


def fetch_features() -> None:
    from huggingface_hub import hf_hub_download

    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    for fname in (NEGATIVE_FEATURES, VALIDATION_FEATURES):
        if (FEATURES_DIR / fname).exists():
            log(f"features present: {fname}")
            continue
        log(f"downloading {fname} (resumable — rerun on interrupt; the negative set is ~17GB)")
        hf_hub_download(
            repo_id=HF_FEATURES_REPO,
            filename=fname,
            repo_type="dataset",
            local_dir=FEATURES_DIR,
        )
        log(f"  → {FEATURES_DIR / fname}")


def fetch_rirs() -> None:
    if dir_has_files(RIR_DIR, "*.wav"):
        log(f"RIRs present: {RIR_DIR}")
        return
    import datasets

    log("downloading MIT environmental impulse responses (~270 clips)")
    RIR_DIR.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset(
        "davidscripka/MIT_environmental_impulse_responses",
        split="train",
        streaming=True,
    )
    n = 0
    for row in ds:
        audio = row["audio"]
        name = Path(audio.get("path") or f"rir_{n}.wav").name
        write_wav_16k(RIR_DIR / name, audio["array"], audio["sampling_rate"])
        n += 1
    log(f"  → {n} RIR wavs in {RIR_DIR}")


def fetch_audioset(max_clips: int) -> None:
    if dir_has_files(AUDIOSET_DIR, "*.wav"):
        log(f"AudioSet clips present: {AUDIOSET_DIR}")
        return
    # Read the parquet shards with pyarrow directly: the shards carry
    # huggingface feature metadata written by a newer `datasets` than our
    # pinned 2.14 can parse, so load_dataset() chokes on them.
    import io

    import pyarrow.parquet as pq
    import soundfile as sf

    log(f"downloading AudioSet background clips (up to {max_clips})")
    AUDIOSET_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for url in AUDIOSET_PARQUET_URLS:
        shard = ASSETS / "audioset_shard.parquet"
        download(url, shard)
        try:
            pf = pq.ParquetFile(shard)
            for batch in pf.iter_batches(columns=["audio"], batch_size=32):
                for rec in batch.column(0):
                    try:
                        data, sr = sf.read(io.BytesIO(rec["bytes"].as_py()))
                        write_wav_16k(AUDIOSET_DIR / f"audioset_{n:05d}.wav", data, sr)
                        n += 1
                    except Exception as e:
                        log(f"  skipping clip {n}: {e}")
                    if n % 250 == 0:
                        log(f"  …{n}/{max_clips}")
                    if n >= max_clips:
                        break
                if n >= max_clips:
                    break
        finally:
            shard.unlink(missing_ok=True)
        if n >= max_clips:
            break
    log(f"  → {n} background clips in {AUDIOSET_DIR}")


def fetch_fma(max_clips: int) -> None:
    if dir_has_files(FMA_DIR, "*.wav"):
        log(f"FMA clips present: {FMA_DIR}")
        return
    import datasets

    log(f"downloading FMA music clips (streaming, {max_clips} × 30s)")
    FMA_DIR.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
    n = 0
    for row in ds:
        audio = row["audio"]
        write_wav_16k(FMA_DIR / f"fma_{n:05d}.wav", audio["array"], audio["sampling_rate"])
        n += 1
        if n % 100 == 0:
            log(f"  …{n}/{max_clips}")
        if n >= max_clips:
            break
    log(f"  → {n} music clips in {FMA_DIR}")


def cmd_assets(args) -> None:
    parts = args.only.split(",") if args.only else ASSET_PARTS
    unknown = set(parts) - set(ASSET_PARTS)
    if unknown:
        sys.exit(f"unknown asset part(s): {', '.join(unknown)} (valid: {', '.join(ASSET_PARTS)})")
    if "piper" in parts:
        fetch_piper()
    if "features" in parts:
        fetch_features()
    if "rirs" in parts:
        fetch_rirs()
    if "audioset" in parts:
        fetch_audioset(args.audioset_clips)
    if "fma" in parts:
        fetch_fma(args.fma_clips)
    log("assets done")


def missing_assets() -> list:
    missing = []
    if not (PIPER_CKPT.exists() and PIPER_CKPT.with_suffix(".pt.json").exists()):
        missing.append("piper checkpoint + voice config (forge.py assets --only piper)")
    for fname in (NEGATIVE_FEATURES, VALIDATION_FEATURES):
        if not (FEATURES_DIR / fname).exists():
            missing.append(f"{fname} (forge.py assets --only features)")
    if not dir_has_files(RIR_DIR, "*.wav"):
        missing.append("MIT RIRs (forge.py assets --only rirs)")
    if not (dir_has_files(AUDIOSET_DIR, "*.wav") or dir_has_files(FMA_DIR, "*.wav")):
        missing.append("background noise (forge.py assets --only audioset,fma)")
    return missing


# ---------------------------------------------------------------- new

def cmd_new(args) -> None:
    # comma-separated variants train ONE model that fires on any of them —
    # the lever for pronunciation/accent coverage
    phrases = [p.strip().lower() for p in args.phrase.split(",") if p.strip()]
    if not phrases:
        sys.exit("empty phrase")
    name = args.name or slugify(phrases[0])
    ww_dir = WAKEWORDS / name
    cfg_path = ww_dir / "config.yml"
    if cfg_path.exists() and not args.force:
        sys.exit(f"{cfg_path} already exists (use --force to overwrite the config)")
    ww_dir.mkdir(parents=True, exist_ok=True)
    template = (FORGE_DIR / "config.template.yml").read_text()
    cfg = (
        template.replace("@NAME@", name)
        .replace("@PHRASES@", "\n".join(f'  - "{p}"' for p in phrases))
        .replace("@N_SAMPLES@", str(args.samples))
        .replace("@N_SAMPLES_VAL@", str(args.samples_val))
        .replace("@STEPS@", str(args.steps))
        .replace("@OUTPUT_DIR@", str(ww_dir))
    )
    cfg_path.write_text(cfg)
    log(f"created {cfg_path}")
    log(f"phrases: {phrases}  positives: {args.samples}  steps: {args.steps}")
    log(f"next: forge.py build {name}   (optionally forge.py google-tts {name} first)")


# ---------------------------------------------------------------- build

BUILD_STEPS = ["generate", "augment", "train"]
STEP_FLAGS = {"generate": "--generate_clips", "augment": "--augment_clips", "train": "--train_model"}


def cmd_build(args) -> None:
    name = args.name
    cfg_path = WAKEWORDS / name / "config.yml"
    if not cfg_path.exists():
        sys.exit(f"no such wake word: {cfg_path} missing (run forge.py new first)")
    missing = missing_assets()
    if missing:
        sys.exit("missing training assets:\n  - " + "\n  - ".join(missing))

    import torch

    if torch.cuda.is_available():
        log(f"torch {torch.__version__} — CUDA: {torch.cuda.get_device_name(0)}")
    else:
        log(f"torch {torch.__version__} — no CUDA device visible, falling back to CPU "
            "(generation and training will be slow)")

    steps = BUILD_STEPS[BUILD_STEPS.index(args.from_step):]
    if args.only_step:
        steps = [args.only_step]

    # Heal an interrupted augment: train.py's "features already exist" check
    # only looks at the first of the four .npy files, so a run killed midway
    # (container restart) would skip augment and then crash in training on
    # the missing ones. Partial set → recompute all four.
    if "augment" in steps:
        feat_dir = WAKEWORDS / name / name
        feats = ["positive_features_train.npy", "positive_features_test.npy",
                 "negative_features_train.npy", "negative_features_test.npy"]
        have = [f for f in feats if (feat_dir / f).exists()]
        if have and len(have) < len(feats):
            log(f"found {len(have)}/4 feature files from an interrupted augment — recomputing all")
            args.overwrite = True

    for step in steps:
        log(f"=== step: {step} ===")
        cmd = [sys.executable, TRAIN_PY, "--training_config", str(cfg_path), STEP_FLAGS[step]]
        if args.overwrite and step == "augment":
            cmd.append("--overwrite")
        subprocess.run(cmd, check=True)

    if "train" in steps:
        src = WAKEWORDS / name / f"{name}.onnx"
        if not src.exists():
            sys.exit(f"training finished but {src} was not produced — check the logs above")
        MODELS.mkdir(parents=True, exist_ok=True)
        dest = MODELS / f"{name}.onnx"
        shutil.copy2(src, dest)
        log(f"model ready: {dest} ({dest.stat().st_size / 1e3:.0f} kB)")
        log("install into EchoMuse: see oww_forge/README.md §Installing")


# ---------------------------------------------------------------- google-tts

def cmd_google_tts(args) -> None:
    import yaml

    cfg_path = WAKEWORDS / args.name / "config.yml"
    if not cfg_path.exists():
        sys.exit(f"no such wake word: {cfg_path} missing (run forge.py new first)")
    cfg = yaml.safe_load(cfg_path.read_text())
    out_base = Path(cfg["output_dir"]) / cfg["model_name"]

    import google_tts

    google_tts.synthesize(
        phrases=cfg["target_phrase"],
        n_samples=args.samples,
        train_dir=out_base / "positive_train",
        test_dir=out_base / "positive_test",
        languages=args.languages.split(","),
        include_standard=args.include_standard,
        assume_yes=args.yes,
    )


# ---------------------------------------------------------------- test

def cmd_test(args) -> None:
    import numpy as np
    import soundfile as sf
    from openwakeword.model import Model

    model_path = MODELS / f"{args.name}.onnx"
    if not model_path.exists():
        sys.exit(f"{model_path} not found (run forge.py build first)")
    oww = Model(wakeword_models=[str(model_path)], inference_framework="onnx")

    wavs = []
    for p in args.wav:
        p = Path(p)
        wavs.extend(sorted(p.glob("*.wav")) if p.is_dir() else [p])
    if not wavs:
        sys.exit("no wav files found")

    for wav in wavs:
        audio, sr = sf.read(wav, dtype="int16")
        if sr != 16000:
            f = audio.astype("float32") / 32768.0
            import librosa

            f = librosa.resample(f.T if f.ndim > 1 else f, orig_sr=sr, target_sr=16000)
            audio = (np.clip(f if f.ndim == 1 else f.mean(axis=0), -1, 1) * 32767).astype("int16")
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype("int16")
        oww.reset()
        peak = 0.0
        for i in range(0, len(audio) - 1280, 1280):
            scores = oww.predict(audio[i : i + 1280])
            peak = max(peak, max(scores.values()))
        print(f"{wav}: peak score {peak:.3f}")


# ---------------------------------------------------------------- ui

def cmd_ui(args) -> None:
    import forge_web

    forge_web.run(host=args.host, port=args.port)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(prog="forge.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("assets", help="download shared training assets (~25GB total)")
    p.add_argument("--only", help=f"comma-separated subset of: {','.join(ASSET_PARTS)}")
    p.add_argument("--fma-clips", type=int, default=1000,
                   help="number of 30s FMA music clips to fetch (default 1000 ≈ 1GB)")
    p.add_argument("--audioset-clips", type=int, default=2000,
                   help="number of 10s AudioSet noise clips to fetch (default 2000)")
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("new", help="create a wake-word training config")
    p.add_argument("phrase", help='wake phrase; comma-separate pronunciation variants '
                                  '(e.g. "hey clara, hey clarra") — one model fires on any')
    p.add_argument("--name", help="model name (default: slug of the phrase)")
    p.add_argument("--samples", type=int, default=30000, help="synthetic positives (default 30000)")
    p.add_argument("--samples-val", type=int, default=2000)
    p.add_argument("--steps", type=int, default=50000, help="max training steps (default 50000)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config.yml")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("build", help="run the training pipeline (generate → augment → train)")
    p.add_argument("name")
    p.add_argument("--from-step", choices=BUILD_STEPS, default="generate",
                   help="resume from this step (clip generation is itself resumable)")
    p.add_argument("--only-step", choices=BUILD_STEPS, help="run a single step")
    p.add_argument("--overwrite", action="store_true",
                   help="recompute augmented features even if they exist")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("google-tts",
                       help="add extra positive samples via Google Cloud TTS (run before build; "
                            "needs GOOGLE_APPLICATION_CREDENTIALS)")
    p.add_argument("name")
    p.add_argument("--samples", type=int, default=2000, help="clips to synthesize (default 2000)")
    p.add_argument("--languages", default="en-US,en-GB,en-AU",
                   help="comma-separated language codes to draw voices from")
    p.add_argument("--include-standard", action="store_true",
                   help="also use lower-quality Standard voices")
    p.add_argument("--yes", action="store_true", help="skip the cost-estimate confirmation")
    p.set_defaults(func=cmd_google_tts)

    p = sub.add_parser("ui", help="serve the web frontend")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8769)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("test", help="score wav file(s) against a trained model")
    p.add_argument("name")
    p.add_argument("--wav", nargs="+", required=True, help="wav file(s) or directorie(s)")
    p.set_defaults(func=cmd_test)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, str(FORGE_DIR))
    main()
