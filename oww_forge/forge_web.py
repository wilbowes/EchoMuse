"""Web UI for oww_forge — a thin aiohttp layer over forge.py.

Everything heavy (asset downloads, training) runs as a forge.py subprocess —
one at a time, streaming to a log file the UI tails. State is derived from
the /data tree on every poll, so the UI survives container restarts and
stays honest about what actually exists on disk.

No auth: this is a LAN batch tool, same trust model as `docker compose run`.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))
import forge
import forge_progress

LOGS = forge.DATA / "logs"
TMP = forge.DATA / "tmp"
STATIC = Path(__file__).parent / "static"

FORGE_PY = str(Path(__file__).parent / "forge.py")

# Where a service-account key lands when it is uploaded through the UI. The
# same path docker-compose.yml maps GOOGLE_APPLICATION_CREDENTIALS to, so
# the two routes cannot disagree about which file is in force.
GOOGLE_CREDS = forge.DATA / "google-credentials.json"

# How long a cancelled job gets to exit on its own before it is killed.
CANCEL_GRACE_S = 10.0


def _job_env() -> dict:
    """
    Environment for a forge.py subprocess. GOOGLE_APPLICATION_CREDENTIALS is
    set from the file on disk rather than inherited, so a key uploaded after
    the container started is picked up by the next job with no restart.
    """
    env = dict(os.environ)
    if GOOGLE_CREDS.exists():
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(GOOGLE_CREDS)
    else:
        env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    return env


class Job:
    def __init__(self, kind: str, label: str, argv: list):
        self.kind = kind
        self.label = label
        self.started = time.time()
        self.rc = None
        self.cancelled = False
        # Build progress folded from the log a little at a time (see
        # forge_progress), and whether a failure to do so was reported yet.
        self.train_log = forge_progress.TrainLog()
        self.progress_warned = False
        self._cpu_seen = False
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS / f"{int(self.started)}_{kind}.log"
        self._logf = open(self.log_path, "wb", buffering=0)
        # Its own process group. forge.py is a PARENT: build spawns
        # openWakeWord's train.py, which is what actually holds the GPU and
        # does the work. Killing only forge.py would orphan that, leaving a
        # run that the UI says is stopped still saturating the machine and
        # still writing to the wake word's directory.
        self.proc = subprocess.Popen(
            [sys.executable, "-u", FORGE_PY, *argv],
            stdout=self._logf,
            stderr=subprocess.STDOUT,
            env=_job_env(),
            start_new_session=True,
        )

    def poll(self):
        if self.rc is None:
            rc = self.proc.poll()
            if rc is not None:
                self.rc = rc
                self._logf.close()
        return self.rc

    def cancel(self) -> None:
        """SIGTERM the group, then SIGKILL whatever is left."""
        if self.poll() is not None:
            return
        self.cancelled = True
        try:
            pgid = os.getpgid(self.proc.pid)
        except ProcessLookupError:
            return
        self._note(f"\n[forge-ui] cancelled by request — stopping (SIGTERM)\n")
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + CANCEL_GRACE_S
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        if self.proc.poll() is None:
            self._note("[forge-ui] still running after "
                       f"{CANCEL_GRACE_S:.0f}s — SIGKILL\n")
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.poll()

    def _note(self, line: str) -> None:
        """Write to the job's own log, so the console shows what happened."""
        try:
            if not self._logf.closed:
                self._logf.write(line.encode())
        except Exception:
            pass

    def cpu_fallback(self) -> bool:
        """Whether forge.py said it could not see a GPU. The header's GPU
        probe runs once at startup, and a container can lose the device
        after that (NVML "Unknown Error" after a host systemd reload), so
        the job's own first lines are the truth about the run."""
        if not self._cpu_seen and self.kind == "build":
            try:
                with open(self.log_path, "rb") as f:
                    self._cpu_seen = b"falling back to CPU" in f.read(16384)
            except OSError:
                pass
        return self._cpu_seen

    def as_dict(self):
        self.poll()
        return {
            "cpu_fallback": self.cpu_fallback(),
            "kind": self.kind,
            "label": self.label,
            "running": self.rc is None,
            "rc": self.rc,
            "cancelled": self.cancelled,
            "started": self.started,
        }


_job: Job | None = None
_gpu_info: dict | None = None


def _start_job(kind: str, label: str, argv: list) -> None:
    global _job
    if _job and _job.poll() is None:
        raise web.HTTPConflict(text=f"a job is already running: {_job.label}")
    _job = Job(kind, label, argv)


def _gpu() -> dict:
    """Probe CUDA once, in a subprocess (importing torch here would pin ~1GB)."""
    global _gpu_info
    if _gpu_info is None:
        try:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import torch,json;print(json.dumps({'available':torch.cuda.is_available(),"
                 "'device':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"
                 "'torch':torch.__version__}))"],
                capture_output=True, text=True, timeout=60,
            )
            _gpu_info = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            _gpu_info = {"available": False, "device": None, "error": str(e)}
    return _gpu_info


def _count(path: Path, pattern: str = "*.wav") -> int:
    return sum(1 for _ in path.glob(pattern)) if path.is_dir() else 0


def _size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1e6, 1) if path.exists() else 0


def _assets_state() -> list:
    neg = forge.FEATURES_DIR / forge.NEGATIVE_FEATURES
    val = forge.FEATURES_DIR / forge.VALIDATION_FEATURES
    return [
        {"part": "piper", "label": "Piper LibriTTS-R checkpoint",
         "present": forge.PIPER_CKPT.exists(), "detail": f"{_size_mb(forge.PIPER_CKPT)} MB"},
        {"part": "features", "label": "Negative + validation features",
         "present": neg.exists() and val.exists(),
         "detail": f"{_size_mb(neg) / 1000:.1f} GB + {_size_mb(val)} MB"},
        {"part": "rirs", "label": "MIT room impulse responses",
         "present": forge.dir_has_files(forge.RIR_DIR),
         "detail": f"{_count(forge.RIR_DIR)} clips"},
        {"part": "audioset", "label": "AudioSet background noise",
         "present": forge.dir_has_files(forge.AUDIOSET_DIR),
         "detail": f"{_count(forge.AUDIOSET_DIR)} clips"},
        {"part": "fma", "label": "FMA background music",
         "present": forge.dir_has_files(forge.FMA_DIR),
         "detail": f"{_count(forge.FMA_DIR)} clips"},
    ]


def _wakewords_state() -> list:
    words = []
    if not forge.WAKEWORDS.is_dir():
        return words
    for cfg_path in sorted(forge.WAKEWORDS.glob("*/config.yml")):
        try:
            cfg = yaml.safe_load(cfg_path.read_text())
        except Exception:
            continue
        name = cfg["model_name"]
        work = Path(cfg["output_dir"]) / name
        model = forge.MODELS / f"{name}.onnx"
        # Only the word being built carries progress: the stage and train
        # step from the job's log, the generate/augment ratio from its
        # directory. None until forge.py has announced a step and the fold
        # has caught up with the log, so nothing stale is shown as live. A
        # failure here (log removed under the job, a hand-edited
        # `steps: 50k`) loses the fill, not the whole poll, and is said once
        # in the console the user is already looking at.
        progress = None
        if _job and _job.poll() is None and _job.kind == "build" and f"'{name}'" in _job.label:
            try:
                state = _job.train_log.update(_job.log_path)
                if state and state["stage"] and not _job.train_log.lagging:
                    progress = forge_progress.stage_progress(work, cfg, state)
            except Exception as e:
                if not _job.progress_warned:
                    _job.progress_warned = True
                    _job._note(f"\n[forge-ui] no progress for '{name}': {type(e).__name__}: {e}\n")
        words.append({
            "name": name,
            "phrases": cfg.get("target_phrase", []),
            "n_samples": cfg.get("n_samples"),
            "n_samples_val": cfg.get("n_samples_val"),
            "steps": cfg.get("steps"),
            "custom_negative_phrases": cfg.get("custom_negative_phrases") or [],
            "clips_train": _count(work / "positive_train"),
            "clips_test": _count(work / "positive_test"),
            "features_built": (work / "positive_features_train.npy").exists(),
            "model_built": model.exists(),
            "model_size_kb": round(model.stat().st_size / 1e3) if model.exists() else None,
            "model_mtime": model.stat().st_mtime if model.exists() else None,
            "progress": progress,
        })
    return words


async def api_state(request):
    return web.json_response({
        "gpu": _gpu(),
        "google": _google_state(),
        "assets": _assets_state(),
        "wakewords": _wakewords_state(),
        "job": _job.as_dict() if _job else None,
    })


async def api_log(request):
    offset = int(request.query.get("offset", 0))
    if _job is None or not _job.log_path.exists():
        return web.json_response({"offset": 0, "data": ""})
    with open(_job.log_path, "rb") as f:
        f.seek(offset)
        data = f.read(65536)
    return web.json_response({"offset": offset + len(data),
                              "data": data.decode("utf-8", "replace")})


async def api_assets_download(request):
    body = await request.json() if request.can_read_body else {}
    only = body.get("only")
    argv = ["assets"] + (["--only", only] if only else [])
    _start_job("assets", f"downloading assets{f' ({only})' if only else ''}", argv)
    return web.json_response({"ok": True})


async def api_wakeword_create(request):
    body = await request.json()
    phrase = (body.get("phrase") or "").strip()
    if not phrase:
        raise web.HTTPBadRequest(text="phrase is required")
    ns = SimpleNamespace(
        phrase=phrase,
        name=(body.get("name") or "").strip() or None,
        samples=int(body.get("samples") or 30000),
        samples_val=int(body.get("samples_val") or 2000),
        steps=int(body.get("steps") or 50000),
        force=False,
    )
    try:
        forge.cmd_new(ns)
    except SystemExit as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "name": ns.name or forge.default_name(phrase)})


def _require_wakeword(name: str) -> None:
    if not (forge.WAKEWORDS / name / "config.yml").exists():
        raise web.HTTPNotFound(text=f"unknown wake word: {name}")


def _to_wav16k(src: Path, dest: Path) -> None:
    """Any browser/phone audio (webm/opus, m4a, mp3, wav…) → 16kHz mono wav."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(dest)],
        check=True, timeout=60,
    )


async def _save_uploads(request, field_name: str) -> list:
    TMP.mkdir(parents=True, exist_ok=True)
    reader = await request.multipart()
    paths = []
    async for field in reader:
        if field.name != field_name:
            continue
        suffix = Path(field.filename or "clip.webm").suffix or ".webm"
        dest = TMP / f"up_{int(time.time() * 1000)}_{len(paths)}{suffix}"
        with open(dest, "wb") as f:
            while chunk := await field.read_chunk():
                f.write(chunk)
        paths.append(dest)
    return paths


async def api_add_samples(request):
    """Real recordings (you, the kids) → the positive training set. The
    generate step counts existing clips toward n_samples, so these displace
    synthetic ones rather than growing the set."""
    name = request.match_info["name"]
    _require_wakeword(name)
    import yaml as _yaml

    cfg = _yaml.safe_load((forge.WAKEWORDS / name / "config.yml").read_text())
    base = Path(cfg["output_dir"]) / cfg["model_name"]
    train_dir, test_dir = base / "positive_train", base / "positive_test"
    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)
    uploads = await _save_uploads(request, "audio")
    if not uploads:
        raise web.HTTPBadRequest(text="no audio uploaded")
    n_ok, errors = 0, []
    try:
        for i, src in enumerate(uploads):
            out_dir = test_dir if (i + 1) % 10 == 0 else train_dir
            dest = out_dir / f"real_{int(time.time())}_{i}.wav"
            try:
                _to_wav16k(src, dest)
                n_ok += 1
            except Exception as e:
                errors.append(f"{src.name}: {e}")
    finally:
        for p in uploads:
            p.unlink(missing_ok=True)
    return web.json_response({"ok": not errors, "added": n_ok, "errors": errors})


async def api_build(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    missing = forge.missing_assets()
    if missing:
        raise web.HTTPConflict(text="missing assets:\n" + "\n".join(missing))
    body = await request.json() if request.can_read_body else {}
    argv = ["build", name]
    if body.get("from_step") and body["from_step"] != "generate":
        argv += ["--from-step", body["from_step"]]
    _start_job("build", f"building '{name}'", argv)
    return web.json_response({"ok": True})


async def api_google_tts(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    body = await request.json() if request.can_read_body else {}
    samples = int(body.get("samples") or 2000)
    langs = (body.get("languages") or "en-US,en-GB,en-AU").strip()
    _start_job("google-tts", f"Google TTS × {samples} ({langs}) for '{name}'",
               ["google-tts", name, "--samples", str(samples), "--languages", langs, "--yes"])
    return web.json_response({"ok": True})


async def api_job_cancel(request):
    """
    Stop the running job. Partial work is KEPT: clip generation is resumable
    and augment/train both check what already exists, so the point of
    stopping is usually to change a parameter and pick up where it left off
    rather than to throw the run away.
    """
    if _job is None or _job.poll() is not None:
        raise web.HTTPConflict(text="no job is running")
    label = _job.label
    _job.cancel()
    return web.json_response({"ok": True, "cancelled": label})


# ---------------------------------------------------------------- google

def _google_state() -> dict:
    """
    What is on disk, never the key itself. The private key is the whole
    secret and there is no reason for it to travel back to a browser.
    """
    if not GOOGLE_CREDS.exists():
        return {"present": False}
    try:
        blob = json.loads(GOOGLE_CREDS.read_text())
    except Exception as e:
        return {"present": True, "valid": False, "error": f"not readable as JSON: {e}"}
    return {
        "present": True,
        "valid": True,
        "project_id": blob.get("project_id"),
        "client_email": blob.get("client_email"),
    }


def _validate_service_account(raw: bytes) -> dict:
    """
    Reject anything that is not a service-account key BEFORE it is written.
    An OAuth client secret is the file people reach for first and it looks
    close enough to pass a glance; it fails much later, inside a training
    job, as an authentication error with nothing pointing back at the
    upload.
    """
    try:
        blob = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise web.HTTPBadRequest(text=f"not valid JSON: {e}")
    if not isinstance(blob, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    if blob.get("type") != "service_account":
        got = blob.get("type") or ("an OAuth client secret" if "installed" in blob
                                   or "web" in blob else "unrecognised")
        raise web.HTTPBadRequest(
            text=f"this is {got}, not a service-account key. In the Google Cloud "
                 "console: IAM & Admin, Service Accounts, Keys, Add key, JSON."
        )
    missing = [k for k in ("project_id", "client_email", "private_key") if not blob.get(k)]
    if missing:
        raise web.HTTPBadRequest(text=f"service-account key is missing: {', '.join(missing)}")
    return blob


async def api_google_get(request):
    return web.json_response(_google_state())


async def api_google_put(request):
    """Accepts the key as a file upload or as a pasted JSON body."""
    raw = b""
    if request.content_type and "multipart" in request.content_type:
        reader = await request.multipart()
        async for field in reader:
            if field.name in ("credentials", "file"):
                while chunk := await field.read_chunk():
                    raw += chunk
                break
    else:
        raw = await request.read()
    if not raw.strip():
        raise web.HTTPBadRequest(text="no credentials supplied")

    _validate_service_account(raw)
    GOOGLE_CREDS.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temp file with the restrictive mode set BEFORE any
    # content lands in it, so the key is never briefly world-readable.
    tmp = GOOGLE_CREDS.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(GOOGLE_CREDS)
    os.chmod(GOOGLE_CREDS, 0o600)
    return web.json_response(_google_state())


async def api_google_delete(request):
    GOOGLE_CREDS.unlink(missing_ok=True)
    return web.json_response({"ok": True, "present": False})


async def api_google_check(request):
    """
    Ask Google to list voices. This is the only thing that distinguishes a
    well-formed key from a WORKING one: the usual failure is a valid key on
    a project where the Text-to-Speech API was never enabled, which no
    amount of local validation can see.
    """
    if not GOOGLE_CREDS.exists():
        raise web.HTTPBadRequest(text="no credentials uploaded")
    probe = (
        "import json\n"
        "from google.cloud import texttospeech as t\n"
        "vs = t.TextToSpeechClient().list_voices().voices\n"
        "langs = sorted({c for v in vs for c in v.language_codes})\n"
        "print(json.dumps({'voices': len(vs), 'languages': len(langs)}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60, env=_job_env(),
    )
    if out.returncode != 0:
        tail = (out.stderr or out.stdout).strip().splitlines()
        return web.json_response(
            {"ok": False, "error": tail[-1] if tail else "unknown error"}, status=200)
    try:
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return web.json_response({"ok": False, "error": out.stdout.strip()[:300]})
    return web.json_response({"ok": True, **info, **_google_state()})


# ---------------------------------------------------------------- config

# Only the knobs worth turning between runs. target_phrase is deliberately
# NOT here: changing it invalidates every clip already generated for this
# wake word, which is a new wake word rather than an edited one.
EDITABLE_INTS = {
    "n_samples": (100, 2_000_000),
    "n_samples_val": (10, 200_000),
    "steps": (100, 5_000_000),
}


def _patch_config(text: str, ints: dict, negatives) -> str:
    for key, value in ints.items():
        text, n = re.subn(rf"(?m)^{re.escape(key)}:[ \t]*\S+[ \t]*$",
                          f"{key}: {value}", text)
        if n != 1:
            raise web.HTTPConflict(
                text=f"could not find a unique '{key}:' line to update")
    if negatives is not None:
        block = ("custom_negative_phrases: []" if not negatives else
                 "custom_negative_phrases:\n" +
                 "\n".join(f'  - "{p}"' for p in negatives))
        # Matches both the empty inline form and a previously written block,
        # stopping at the next top-level key.
        text, n = re.subn(
            r"(?ms)^custom_negative_phrases:.*?(?=^\S)", block + "\n\n", text)
        if n != 1:
            raise web.HTTPConflict(
                text="could not find the custom_negative_phrases block to update")
    return text


async def api_wakeword_patch(request):
    """
    Change training parameters on an existing wake word. Edits the lines in
    place rather than round-tripping the YAML, because the template's
    comments explain every field and a dump would silently delete them.
    """
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None:
        raise web.HTTPConflict(
            text=f"stop the running job first: {_job.label}")
    body = await request.json()

    ints = {}
    for key, (lo, hi) in EDITABLE_INTS.items():
        if body.get(key) in (None, ""):
            continue
        try:
            value = int(body[key])
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text=f"{key} must be a whole number")
        if not lo <= value <= hi:
            raise web.HTTPBadRequest(text=f"{key} must be between {lo} and {hi}")
        ints[key] = value

    negatives = body.get("custom_negative_phrases")
    if negatives is not None:
        if isinstance(negatives, str):
            negatives = [p.strip() for p in negatives.split(",")]
        negatives = [p.strip().lower() for p in negatives if p and p.strip()]

    if not ints and negatives is None:
        raise web.HTTPBadRequest(text="nothing to change")

    cfg_path = forge.WAKEWORDS / name / "config.yml"
    updated = _patch_config(cfg_path.read_text(), ints, negatives)
    yaml.safe_load(updated)   # never leave a config the trainer cannot read
    cfg_path.write_text(updated)
    return web.json_response({"ok": True, "changed": {**ints, **(
        {"custom_negative_phrases": negatives} if negatives is not None else {})}})


async def api_piper_voices(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    body = await request.json() if request.can_read_body else {}
    samples = int(body.get("samples") or 4000)
    lang = (body.get("language") or "en_GB").strip()
    argv = ["piper-voices", name, "--samples", str(samples), "--language", lang]
    if body.get("voices"):
        argv += ["--voices", body["voices"]]
    _start_job("piper-voices", f"{lang} voices × {samples} for '{name}'", argv)
    return web.json_response({"ok": True})


async def api_voices(request):
    """
    The published voice catalogue, so the UI can offer languages rather than
    hardcoding the maintainer's own. Cached on disk after the first fetch.
    """
    import piper_voices

    loop = asyncio.get_running_loop()
    lang = request.query.get("language")
    try:
        if lang:
            rows = await loop.run_in_executor(None, piper_voices.catalogue, forge.ASSETS)
            return web.json_response([v for v in rows if v["language"] == lang])
        return web.json_response(
            await loop.run_in_executor(None, piper_voices.languages, forge.ASSETS))
    except SystemExit as e:
        raise web.HTTPBadRequest(text=str(e))
    except Exception as e:
        raise web.HTTPBadGateway(text=f"could not read the voice catalogue: {e}")


async def api_preview(request):
    """
    Synthesize a phrase so it can be HEARD before a training run is started.

    Synchronous rather than a job: it takes about a second, and the whole
    value is comparing two spelling variants back to back. Runs in a thread
    because onnxruntime does not release the event loop.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="nothing to say")
    if len(text) > 200:
        raise web.HTTPBadRequest(text="phrase is too long to preview")
    import piper_voices

    try:
        wav = await asyncio.get_running_loop().run_in_executor(
            None, piper_voices.preview, text, forge.ASSETS,
            body.get("voice") or None, int(body.get("speaker") or 0),
            body.get("language") or None,
        )
    except SystemExit as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.Response(body=wav, content_type="audio/wav")


async def api_test(request):
    name = request.match_info["name"]
    if not (forge.MODELS / f"{name}.onnx").exists():
        raise web.HTTPNotFound(text="model not built yet")
    uploads = await _save_uploads(request, "wav")
    if not uploads:
        raise web.HTTPBadRequest(text="no audio uploaded")
    wavs = []
    try:
        for src in uploads:
            wav = src.with_suffix(".conv.wav")
            try:
                _to_wav16k(src, wav)
            except Exception as e:
                return web.json_response({"ok": False, "output": f"could not decode audio: {e}"})
            wavs.append(wav)
        out = subprocess.run(
            [sys.executable, FORGE_PY, "test", name, "--wav", *map(str, wavs)],
            capture_output=True, text=True, timeout=300,
        )
        return web.json_response({"ok": out.returncode == 0,
                                  "output": out.stdout + out.stderr})
    finally:
        for p in uploads + wavs:
            p.unlink(missing_ok=True)


async def api_delete(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None and name in _job.label:
        raise web.HTTPConflict(text="a job for this wake word is running")
    shutil.rmtree(forge.WAKEWORDS / name, ignore_errors=True)
    (forge.MODELS / f"{name}.onnx").unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def api_rename(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None and f"'{name}'" in _job.label:
        raise web.HTTPConflict(text="stop the job running for this wake word first")
    new = ((await request.json()).get("name") or "").strip()
    try:
        forge.rename_wakeword(name, new)
    except (ValueError, FileExistsError) as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "name": new})


async def api_model_download(request):
    name = request.match_info["name"]
    path = forge.MODELS / f"{name}.onnx"
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        "Content-Disposition": f'attachment; filename="{name}.onnx"'})


async def index(request):
    return web.FileResponse(STATIC / "index.html")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/log", api_log)
    app.router.add_post("/api/assets/download", api_assets_download)
    app.router.add_post("/api/job/cancel", api_job_cancel)
    app.router.add_get("/api/google", api_google_get)
    app.router.add_put("/api/google", api_google_put)
    app.router.add_post("/api/google/check", api_google_check)
    app.router.add_delete("/api/google", api_google_delete)
    app.router.add_post("/api/wakewords", api_wakeword_create)
    app.router.add_patch("/api/wakewords/{name}", api_wakeword_patch)
    app.router.add_post("/api/wakewords/{name}/build", api_build)
    app.router.add_post("/api/wakewords/{name}/google-tts", api_google_tts)
    app.router.add_post("/api/wakewords/{name}/piper-voices", api_piper_voices)
    app.router.add_get("/api/voices", api_voices)
    app.router.add_post("/api/preview", api_preview)
    app.router.add_post("/api/wakewords/{name}/test", api_test)
    app.router.add_post("/api/wakewords/{name}/samples", api_add_samples)
    app.router.add_delete("/api/wakewords/{name}", api_delete)
    app.router.add_post("/api/wakewords/{name}/rename", api_rename)
    app.router.add_get("/api/models/{name}.onnx", api_model_download)
    return app


def run(host: str = "0.0.0.0", port: int = 8769) -> None:
    print(f"[forge-ui] listening on http://{host}:{port}", flush=True)
    web.run_app(make_app(), host=host, port=port, print=None)
