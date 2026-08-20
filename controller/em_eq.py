"""
em_eq.py — Output EQ for EchoMuse Controller
=============================================

Applies a biquad filter chain to mono S16_LE PCM (Piper TTS output) before
it is resampled and streamed to the device speaker.

Eight independently controllable bands covering the Echo Dot Gen 2's useful
output range. Each band is a gain value in dB; 0.0 = flat (no effect).

Band centre frequencies and types:
  0:  125 Hz  — low shelf
  1:  250 Hz  — peaking, Q=1.4
  2:  500 Hz  — peaking, Q=1.4
  3: 1000 Hz  — peaking, Q=1.4
  4: 2000 Hz  — peaking, Q=1.4
  5: 3500 Hz  — peaking, Q=1.4
  6: 5500 Hz  — peaking, Q=1.4
  7: 8000 Hz  — high shelf

All filter design uses the Audio EQ Cookbook by Robert Bristow-Johnson.
High-pass uses scipy.signal.butter (already a dependency via openwakeword).

Usage:
    import em_eq
    eq_pcm = em_eq.apply(voice_response, SPEAKER_RATE, bands=[0]*8, loudness=False)
"""

import math
import logging
import numpy as np
from scipy.signal import sosfilt

import em_limiter
import em_mbc  # noqa: F401  (type reference in signatures)

log = logging.getLogger("echomuse.eq")

EQ_FREQUENCIES = [125, 250, 500, 1000, 2000, 3500, 5500, 8000]
NUM_BANDS       = len(EQ_FREQUENCIES)
DEFAULT_BANDS   = [0.0] * NUM_BANDS
_PEAK_Q         = 1.4   # ~1 octave bandwidth for middle bands


# ─── Biquad primitives ────────────────────────────────────────────────────────

def _peak_sos(fc: float, gain_db: float, Q: float, fs: float) -> np.ndarray:
    """Peaking parametric EQ biquad (Audio EQ Cookbook)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    alpha = math.sin(w0) / (2 * Q)
    b0 = 1 + alpha * A;  b1 = -2 * cw;  b2 = 1 - alpha * A
    a0 = 1 + alpha / A;  a1 = -2 * cw;  a2 = 1 - alpha / A
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _loshelf_sos(fc: float, gain_db: float, fs: float) -> np.ndarray:
    """Low shelf biquad (Audio EQ Cookbook, S=1)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    sqA   = math.sqrt(A)
    alpha = math.sin(w0) / math.sqrt(2)   # S=1
    b0 =      A * ((A+1) - (A-1)*cw + 2*sqA*alpha)
    b1 =  2 * A * ((A-1) - (A+1)*cw)
    b2 =      A * ((A+1) - (A-1)*cw - 2*sqA*alpha)
    a0 =           (A+1) + (A-1)*cw + 2*sqA*alpha
    a1 =     -2 * ((A-1) + (A+1)*cw)
    a2 =           (A+1) + (A-1)*cw - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _hishelf_sos(fc: float, gain_db: float, fs: float) -> np.ndarray:
    """High shelf biquad (Audio EQ Cookbook, S=1)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    sqA   = math.sqrt(A)
    alpha = math.sin(w0) / math.sqrt(2)   # S=1
    b0 =      A * ((A+1) + (A-1)*cw + 2*sqA*alpha)
    b1 = -2 * A * ((A-1) + (A+1)*cw)
    b2 =      A * ((A+1) + (A-1)*cw - 2*sqA*alpha)
    a0 =           (A+1) - (A-1)*cw + 2*sqA*alpha
    a1 =      2 * ((A-1) - (A+1)*cw)
    a2 =           (A+1) - (A-1)*cw - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _loudness_sos(fs: float) -> np.ndarray:
    """Speech-range presence boost for lower listening volumes."""
    return _peak_sos(2500, 5.0, 0.8, fs)


# ─── Public API ───────────────────────────────────────────────────────────────

def build_sos(bands: list, sample_rate: int, loudness: bool = False) -> np.ndarray:
    """
    Build a stacked SOS matrix for the given band gains and sample rate.

    Exposed separately so callers can cache the matrix when bands haven't
    changed between calls.
    """
    sections = []
    for i, (fc, gain_db) in enumerate(zip(EQ_FREQUENCIES, bands)):
        if i == 0:
            sections.append(_loshelf_sos(fc, gain_db, sample_rate))
        elif i == NUM_BANDS - 1:
            sections.append(_hishelf_sos(fc, gain_db, sample_rate))
        else:
            sections.append(_peak_sos(fc, gain_db, _PEAK_Q, sample_rate))
    if loudness:
        sections.append(_loudness_sos(sample_rate))
    return np.vstack(sections)


def apply(
    pcm: bytes,
    sample_rate: int,
    bands: list | None = None,
    loudness: bool = False,
    limiter: "em_limiter.Limiter | None" = None,
    guard: "em_mbc.BassGuard | None" = None,
) -> bytes:
    """
    Apply EQ to mono S16_LE PCM. Returns mono S16_LE PCM at the same rate.

    Args:
        pcm:         Raw mono S16_LE PCM bytes (decoded TTS audio).
        sample_rate: Sample rate of pcm (SPEAKER_RATE = 48000 in the
                     playback pipeline since the 48k decode change).
        bands:       List of NUM_BANDS (8) gain values in dB. None = flat.
        loudness:    Add a +5dB speech-range presence boost if True.
        limiter:     Optional peak limiter applied AFTER the EQ, in float, so
                     nothing is quantised twice. Without one this function
                     hard-clips whatever the EQ boosted past full scale, which
                     is #231.

    Returns:
        EQ-processed mono S16_LE PCM bytes, same length as input.
    """
    if len(pcm) < 2:
        return pcm

    if bands is None:
        bands = DEFAULT_BANDS

    if len(bands) != NUM_BANDS:
        log.warning(f"[eq] Expected {NUM_BANDS} bands, got {len(bands)} — padding with zeros")
        bands = list(bands) + [0.0] * (NUM_BANDS - len(bands))

    flat = not loudness and all(b == 0.0 for b in bands)
    if flat and limiter is None and guard is None:
        return pcm

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if not flat:
        samples = sosfilt(build_sos(bands, sample_rate, loudness), samples)
    # Order matters: the guard removes excursion the driver cannot deliver,
    # THEN the limiter catches what is left. Limiting first would spend gain
    # reduction on bass that is about to be thrown away, pulling down the
    # midrange for no reason.
    if guard is not None:
        samples = guard.process(samples)
    if limiter is not None:
        samples = np.concatenate([limiter.process(samples), limiter.flush()])
    # Backstop only. With a limiter attached this must never engage; without
    # one it is the historical behaviour, preserved so a caller that passes no
    # limiter is no worse off than before.
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


class StreamingEQ:
    """
    Chunk-by-chunk EQ with filter state carried across calls — for audio
    that can't be processed as one buffer (music streams). apply() on
    independent chunks would reset the biquad states at every boundary
    and click; this is bit-identical to apply() over the concatenation.
    """

    def __init__(self, sample_rate: int, bands: list | None = None,
                 loudness: bool = False,
                 limiter: "em_limiter.Limiter | None" = None,
                 guard: "em_mbc.BassGuard | None" = None):
        self._limiter = limiter
        self._guard = guard
        self._sample_rate = int(sample_rate)   # set_bands rebuilds against it
        # Last values update() applied; None until it is first called, so the
        # first call always lands rather than matching a coincidental default.
        self._applied = None
        if bands is None:
            bands = DEFAULT_BANDS
        if len(bands) != NUM_BANDS:
            bands = list(bands) + [0.0] * (NUM_BANDS - len(bands))
        if not loudness and all(b == 0.0 for b in bands):
            self._sos = None  # flat — pure passthrough
        else:
            self._sos = build_sos(bands, sample_rate, loudness)
            self._zi  = np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    @property
    def limiter(self):
        """The chain's limiter, for instrumentation. May be None."""
        return self._limiter

    @property
    def guard(self):
        """The chain's bass guard, for instrumentation. May be None."""
        return self._guard

    def update(self, *,
               bands: list | None = None,
               loudness: bool = False,
               limiter_enabled: bool | None = None,
               limiter_threshold: float | None = None,
               limiter_release: float | None = None,
               guard_enabled: bool | None = None,
               guard_db: float | None = None) -> bool:
        """
        Re-apply the whole chain's settings mid-stream. Returns True if
        anything moved.

        Called per chunk by the music feed, so it compares before it acts:
        the steady-state cost is one tuple comparison, and the processors are
        only touched when a value actually changed. That matters because the
        setters are cheap but rebuilding the EQ coefficients is not, and doing
        it 23 times a second for no reason would be silly.

        Everything here updates state IN PLACE. Nothing is reconstructed, so
        there is no discontinuity in the filter states, the limiter's held
        tail or the stream's latency — see the setters for why each of those
        would otherwise be audible.
        """
        wanted = (tuple(bands) if bands is not None else None, loudness,
                  limiter_enabled, limiter_threshold, limiter_release,
                  guard_enabled, guard_db)
        if wanted == self._applied:
            return False

        prev = self._applied
        self._applied = wanted

        # EQ only when the curve itself moved — the expensive branch.
        if prev is None or (prev[0], prev[1]) != (wanted[0], wanted[1]):
            self.set_bands(bands, loudness)

        if self._limiter is not None:
            self._limiter.set_params(threshold_db=limiter_threshold,
                                     release_ms=limiter_release,
                                     enabled=limiter_enabled)
        if self._guard is not None:
            self._guard.set_params(bass_guard_db=guard_db,
                                   enabled=guard_enabled)
        return True

    def set_bands(self, bands: list | None, loudness: bool = False) -> None:
        """
        Change the EQ curve mid-stream, keeping the filter state.

        The biquad state is carried across the coefficient change rather than
        zeroed: the section count is fixed by NUM_BANDS, so the state array
        still fits, and holding it means the filter continues from where the
        audio actually is. Zeroing would produce a transient at the moment of
        the change — precisely when someone is listening for the difference
        the change made.

        Going from flat to shaped allocates fresh (zero) state, which is
        correct: there was no filter running to carry.
        """
        if bands is None:
            bands = DEFAULT_BANDS
        if len(bands) != NUM_BANDS:
            bands = list(bands) + [0.0] * (NUM_BANDS - len(bands))

        if not loudness and all(b == 0.0 for b in bands):
            self._sos = None
            return

        sos = build_sos(bands, self._sample_rate, loudness)
        if self._sos is None or self._zi.shape[0] != sos.shape[0]:
            self._zi = np.zeros((sos.shape[0], 2), dtype=np.float64)
        self._sos = sos

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) < 2 or (self._sos is None and self._limiter is None
                            and self._guard is None):
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if self._sos is not None:
            samples, self._zi = sosfilt(self._sos, samples, zi=self._zi)
        if self._guard is not None:
            samples = self._guard.process(samples)
        if self._limiter is not None:
            samples = self._limiter.process(samples)
        return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

    def flush(self) -> bytes:
        """
        Emit the limiter's held look-ahead tail at end of stream.

        Returns empty when there is no limiter, so callers can call it
        unconditionally. Without it the last few ms of every music stream are
        dropped — inaudible on a track, obvious on a short announcement.
        """
        if self._limiter is None:
            return b""
        tail = self._limiter.flush()
        if not tail.size:
            return b""
        return np.clip(tail, -32768, 32767).astype(np.int16).tobytes()


# ─── Chain instrumentation ────────────────────────────────────────────────
#
# Whether the output chain is doing anything has been unanswerable from
# outside it, and that has now cost four listening tests. The stages
# interact hard enough that "I hear no difference" is NOT evidence either
# way: the bass guard is worth ~7.7dB of overall level at a modest EQ and
# ~0dB under a heavy boost, because the limiter gives back exactly what the
# guard takes away (measured 2026-08-20 — guard on/off at +12dB on all
# eight bands is -0.17dB overall, and the whole difference moves into the
# midrange instead). A listener judging by loudness is then judging the one
# cue that has been cancelled out.
#
# So the settings and the WORK DONE are reported separately. Settings say
# what reached the audio path, which is the config question; max reduction
# says whether the law ever engaged, which is the audio question. A guard
# that is enabled and reports 0.00dB of reduction is being fed content with
# no bass in it — a different fault from one that never got the setting.
#
# Cost is two log lines per stream plus one per live change, so nothing runs
# per frame. `max_reduction_db` is already maintained by both processors;
# this only surfaces it.


def _stage_state(stage, off="off") -> bool:
    """
    Whether a chain stage will actually process.

    Both shapes mean disabled and both occur: the one-shot path (em_eq.apply)
    takes None from for_stream(), while StreamingEQ always holds an instance
    and carries an `enabled` flag so a stream can be toggled without dropping
    filter state.
    """
    return stage is not None and getattr(stage, "enabled", True)


def describe_chain(bands, loudness, limiter=None, guard=None) -> str:
    """
    One line naming what this stream's chain is SET to.

    Emitted when a stream starts and again whenever a live update changes
    something, so the log shows both the starting point and the fact that a
    dashboard change reached the audio — which is the half that could not be
    seen before.
    """
    shaped = bands and any(float(b) != 0.0 for b in bands)
    eq = ("flat" if not shaped
          else "/".join(f"{float(b):+g}" if float(b) else "0" for b in bands))
    parts = [f"eq={eq}", f"speech_boost={'on' if loudness else 'off'}"]
    parts.append(
        f"guard={f'{guard.bass_guard_db:g}dB' if _stage_state(guard) else 'off'}"
    )
    parts.append(
        f"limiter={f'{limiter.threshold_db:g}dB/{limiter.release_ms:g}ms'}"
        if _stage_state(limiter) else "limiter=off"
    )
    return " ".join(parts)


def describe_activity(limiter=None, guard=None) -> str:
    """
    One line naming what the chain actually DID, in dB of gain reduction.

    `n/a` distinguishes a stage that was off from one that was on and never
    engaged — the two look identical from a listening seat and want opposite
    investigations.
    """
    def red(stage):
        if not _stage_state(stage):
            return "n/a"
        return f"{stage.max_reduction_db:.2f}dB"
    return f"guard_reduction={red(guard)} limiter_reduction={red(limiter)}"
