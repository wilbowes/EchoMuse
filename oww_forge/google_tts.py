"""Extra positive training samples via Google Cloud Text-to-Speech.

Piper's LibriTTS-R generator provides sample volume (hundreds of speakers,
cheap, local); Google's Neural2/Studio/Chirp voices add a different — and
much higher-fidelity — acoustic character. A modest layer of Google samples
(a few thousand) on top of the Piper set adds voice diversity the model
can't get from a single TTS family.

Clips are written straight into the wake word's positive_train/positive_test
directories. openWakeWord's generate step counts existing files toward
n_samples, so Google clips added *before* `forge.py build` simply displace
that many Piper generations rather than growing the set.

Auth: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON with the
Cloud Text-to-Speech API enabled (compose maps /data/google-credentials.json).
"""

import itertools
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SPEAKING_RATES = [0.85, 0.95, 1.0, 1.1, 1.2]
PITCHES = [-4.0, -2.0, 0.0, 2.0, 4.0]
TEST_FRACTION = 0.1
# per-character prices (USD per 1M chars) for a rough estimate only
EST_PRICE_PER_MCHAR = 16.0


def log(msg: str) -> None:
    print(f"[google-tts] {msg}", flush=True)


def synthesize(phrases, n_samples, train_dir: Path, test_dir: Path,
               languages, include_standard=False, assume_yes=False) -> None:
    try:
        from google.cloud import texttospeech
    except ImportError:
        sys.exit("google-cloud-texttospeech is not installed in this image")

    try:
        client = texttospeech.TextToSpeechClient()
    except Exception as e:
        sys.exit(
            f"could not create TTS client ({e}) — set GOOGLE_APPLICATION_CREDENTIALS "
            "to a service-account JSON (see oww_forge/README.md)"
        )

    languages = [l.strip() for l in languages if l.strip()]
    voices = []
    for v in client.list_voices().voices:
        if not any(code.startswith(tuple(languages)) for code in v.language_codes):
            continue
        if not include_standard and "Standard" in v.name:
            continue
        voices.append(v)
    if not voices:
        sys.exit(f"no voices matched languages {languages}")
    log(f"{len(voices)} voices across {languages}")

    n_chars = sum(len(p) for p in phrases) // len(phrases) * n_samples
    log(f"~{n_chars} characters ≈ ${n_chars / 1e6 * EST_PRICE_PER_MCHAR:.2f} "
        f"(premium-voice rate, estimate only)")
    if not assume_yes:
        reply = input("continue? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            sys.exit("aborted")

    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    combos = list(itertools.product(voices, SPEAKING_RATES, PITCHES))
    random.shuffle(combos)
    jobs = []
    for i, (voice, rate, pitch) in enumerate(itertools.islice(itertools.cycle(combos), n_samples)):
        phrase = phrases[i % len(phrases)]
        out_dir = test_dir if random.random() < TEST_FRACTION else train_dir
        dest = out_dir / f"google_{i:06d}_{voice.name}.wav"
        jobs.append((phrase, voice, rate, pitch, dest))

    bad_voices = set()

    def synth_one(job):
        phrase, voice, rate, pitch, dest = job
        if voice.name in bad_voices:
            return 0
        req = dict(
            input=texttospeech.SynthesisInput(text=phrase),
            voice=texttospeech.VoiceSelectionParams(
                language_code=voice.language_codes[0], name=voice.name
            ),
        )
        # Newer voice families (Journey/Chirp) reject pitch and/or rate —
        # degrade per-voice rather than skipping the voice outright.
        for audio_kwargs in (
            dict(speaking_rate=rate, pitch=pitch),
            dict(speaking_rate=rate),
            dict(),
        ):
            try:
                resp = client.synthesize_speech(
                    **req,
                    audio_config=texttospeech.AudioConfig(
                        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                        sample_rate_hertz=16000,
                        **audio_kwargs,
                    ),
                )
                # LINEAR16 responses arrive with a WAV header — write as-is.
                dest.write_bytes(resp.audio_content)
                return 1
            except Exception:
                continue
        bad_voices.add(voice.name)
        return 0

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ok in pool.map(synth_one, jobs):
            done += ok
            if done and done % 200 == 0:
                log(f"…{done}/{n_samples}")
    if bad_voices:
        log(f"skipped {len(bad_voices)} incompatible voices: {sorted(bad_voices)[:10]}…")
    log(f"wrote {done} clips → {train_dir.parent}")
    if done < n_samples * 0.5:
        log("WARNING: more than half the requests failed — check API quota/credentials")
