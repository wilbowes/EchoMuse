"""
DTLN streaming noise suppression — controller-side, ASR-bound audio only.

P0-3 closure (2026-07-12): the device's vendored RNNoise was never usable
(48kHz-native model fed 16kHz audio) and stays off. Per the agreed
architecture the device is a dumb transducer and NS lives here, applied
ONLY to the audio streamed to HA's STT during a voice turn
(em_esphome._stream_mic_audio, behind the per-device `nsAsr` config flag).
The always-on wake stream is never denoised — openwakeword is trained on
noisy audio, and all controller-side adaptation on that stream is
measurement-only (noise floor tracking).

Model: DTLN (github.com/breizhn/DTLN, MIT) — dual-signal LSTM, ~1M params,
16kHz-native, shipped as two stacked ONNX models with explicit LSTM state
tensors, so streaming state lives here per-turn while the ONNX sessions
themselves are stateless and shared process-wide. Frame layout is the
reference real_time_processing_onnx.py: 512-sample FFT window, 128-sample
hop, overlap-add — ~32ms algorithmic latency, ~0.1ms CPU per hop.

Model files are vendored into the Docker image at build time (see
Dockerfile), not committed; bare-metal runs point NS_MODEL_DIR at a
directory containing model_1.onnx / model_2.onnx.
"""

import logging
import os
import threading
import time
import wave

import numpy as np

log = logging.getLogger("echomuse")

BLOCK_LEN   = 512   # FFT window (samples at 16kHz)
BLOCK_SHIFT = 128   # hop
STATE_SHAPE = (1, 2, 128, 2)

# Overlap-add latency: the block written out at step i is the OLDEST hop of
# the accumulator, so it corresponds to input samples BLOCK_LEN - BLOCK_SHIFT
# earlier. Anything mixed against the output has to be delayed by this or it
# comb-filters — which is worse than the artefact being fixed here.
OLA_DELAY = BLOCK_LEN - BLOCK_SHIFT   # 384 samples, 24ms

# Suppression floor. DTLN is free to output digital silence, and on this
# fleet it did: 8-15% of samples at EXACT zero at a healthy signal level,
# against 0.3% with NS off (#154), heard as speech chopping in and out (#137).
# That is the gate cutting into speech rather than shaving noise, and it
# costs more transcription accuracy than the residual noise ever did.
#
# The output is therefore a convex blend of the model's estimate and the
# (delayed) input, so suppression stops at NS_FLOOR_DB instead of infinity.
# Convex, not additive: where the model passes speech through untouched
# (enhanced == x) the blend is also x, so speech level is unchanged and only
# the fully-suppressed regions move. ASR gains flatten well before 20dB of
# noise reduction, so the ceiling this imposes is not a cost worth paying
# attention to; the holes were.
NS_FLOOR_DB = -20.0
_DRY = float(10.0 ** (NS_FLOOR_DB / 20.0))   # 0.1
_WET = 1.0 - _DRY

MODEL_DIR = os.environ.get(
    "NS_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "dtln"),
)
MODEL_FILES = ("model_1.onnx", "model_2.onnx")

# When set, every denoised turn writes a raw/denoised WAV pair here —
# listen to exactly what STT received. Debug/validation only; the
# directory is created on first use.
DEBUG_DIR = os.environ.get("NS_DEBUG_DIR", "")

_lock = threading.Lock()
_sessions = None      # ((sess, data_in, state_in, data_out, state_out) × 2)
_load_failed = False


def available() -> bool:
    """Model files present (or sessions already loaded) — cheap pre-check."""
    if _sessions is not None:
        return True
    return all(os.path.isfile(os.path.join(MODEL_DIR, f)) for f in MODEL_FILES)


def _io_names(sess):
    """
    Split a DTLN session's inputs/outputs into (data, state) by shape —
    the LSTM state tensor is always (1, 2, 128, 2). Derived rather than
    hardcoded so a re-exported model with different tensor names still
    loads.
    """
    def pick(entries):
        state = next(e.name for e in entries if tuple(e.shape) == STATE_SHAPE)
        data  = next(e.name for e in entries if tuple(e.shape) != STATE_SHAPE)
        return data, state
    d_in, s_in = pick(sess.get_inputs())
    d_out, s_out = pick(sess.get_outputs())
    return d_in, s_in, d_out, s_out


def _get_sessions():
    """
    Lazy singleton pair of ONNX sessions, shared across devices/turns
    (stateless — LSTM state is an explicit tensor owned by each
    StreamingDenoiser). Single-threaded sessions: each inference is ~0.1ms
    on one core; thread fan-out would cost more than it saves and this
    runs in the shared default executor alongside openwakeword.
    """
    global _sessions, _load_failed
    if _sessions is not None:
        return _sessions
    with _lock:
        if _sessions is None and not _load_failed:
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = 1
                so.inter_op_num_threads = 1
                so.log_severity_level   = 3
                loaded = []
                for f in MODEL_FILES:
                    sess = ort.InferenceSession(
                        os.path.join(MODEL_DIR, f), so,
                        providers=["CPUExecutionProvider"],
                    )
                    loaded.append((sess, *_io_names(sess)))
                _sessions = tuple(loaded)
                log.info(f"[ns] DTLN models loaded from {MODEL_DIR}")
            except Exception as e:
                _load_failed = True
                log.warning(f"[ns] DTLN model load failed ({e}) — NS unavailable")
    return _sessions


class StreamingDenoiser:
    """
    One per voice turn. process() consumes S16_LE mono 16kHz bytes and
    returns the denoised equivalent; input not a multiple of the hop size
    is carried over to the next call (our 80ms frames are exactly 10 hops,
    so in practice output length == input length).
    """

    def __init__(self, sessions=None):
        # sessions is injectable so the frame maths — the OLA alignment in
        # particular — is testable without onnxruntime or the model files,
        # neither of which exists outside the built image.
        sessions = sessions if sessions is not None else _get_sessions()
        if sessions is None:
            raise RuntimeError(f"DTLN models not loadable from {MODEL_DIR}")
        self._m1, self._m2 = sessions
        self._states_1 = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._states_2 = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._in_buf   = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._out_buf  = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._pending  = b""
        # Input history for the floor blend, delayed to match OLA_DELAY.
        self._dry_buf  = np.zeros(OLA_DELAY, dtype=np.float32)
        # How much of the output the gate took to exact zero, and how much of
        # the INPUT was already there. Both, because one without the other
        # cannot separate "the denoiser chewed the speech" from "the room was
        # silent" — which is the whole question #137 asks, and neither number
        # was recorded anywhere when it was asked.
        self.out_zeros = 0
        self.in_zeros  = 0
        self.samples   = 0

    def process(self, payload: bytes) -> bytes:
        data = self._pending + payload
        usable = len(data) - len(data) % (BLOCK_SHIFT * 2)
        self._pending = data[usable:]
        if usable == 0:
            return b""
        x = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        out = np.empty_like(x)

        sess1, d1_in, s1_in, d1_out, s1_out = self._m1
        sess2, d2_in, s2_in, d2_out, s2_out = self._m2

        for i in range(0, x.size, BLOCK_SHIFT):
            self._in_buf = np.roll(self._in_buf, -BLOCK_SHIFT)
            self._in_buf[-BLOCK_SHIFT:] = x[i:i + BLOCK_SHIFT]

            spectrum = np.fft.rfft(self._in_buf)
            mag = np.abs(spectrum).astype(np.float32).reshape(1, 1, -1)
            mask, self._states_1 = sess1.run(
                [d1_out, s1_out], {d1_in: mag, s1_in: self._states_1}
            )
            # Mask is real-valued — applying it to the complex spectrum
            # reuses the noisy phase, as in the reference implementation.
            est_block = np.fft.irfft(spectrum * mask.reshape(-1)).astype(np.float32)

            enhanced, self._states_2 = sess2.run(
                [d2_out, s2_out],
                {d2_in: est_block.reshape(1, 1, -1), s2_in: self._states_2},
            )

            self._out_buf = np.roll(self._out_buf, -BLOCK_SHIFT)
            self._out_buf[-BLOCK_SHIFT:] = 0.0
            self._out_buf += enhanced.reshape(-1)

            # Blend against the input as it stood OLA_DELAY samples ago —
            # the sample the model's output actually corresponds to.
            dry = self._dry_buf[:BLOCK_SHIFT]
            self._dry_buf = np.concatenate(
                (self._dry_buf[BLOCK_SHIFT:], x[i:i + BLOCK_SHIFT])
            )
            out[i:i + BLOCK_SHIFT] = _WET * self._out_buf[:BLOCK_SHIFT] + _DRY * dry

        pcm = (np.clip(out, -1.0, 0.999969) * 32768.0).astype(np.int16)
        self.samples   += pcm.size
        self.out_zeros += int(np.count_nonzero(pcm == 0))
        self.in_zeros  += int(np.count_nonzero(
            (x * 32768.0).astype(np.int16) == 0
        ))
        return pcm.tobytes()

    def zero_report(self) -> str:
        """
        One line for the turn log: what fraction of the stream the gate took
        to digital silence, against what arrived that way. Cheap enough to
        run every turn — two counts per 80ms frame.
        """
        if not self.samples:
            return "no samples"
        return (f"zeros out={100.0 * self.out_zeros / self.samples:.1f}% "
                f"in={100.0 * self.in_zeros / self.samples:.1f}% "
                f"floor={NS_FLOOR_DB:.0f}dB")


def dump_debug_pair(tag: str, raw: bytes, denoised: bytes) -> None:
    """
    Write a raw/denoised WAV pair to DEBUG_DIR (no-op when unset). Called
    from the turn's streaming path at end of turn — validation tooling,
    failures are logged and swallowed.
    """
    if not DEBUG_DIR or not raw:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix, pcm in (("raw", raw), ("ns", denoised)):
            path = os.path.join(DEBUG_DIR, f"{stamp}_{tag}_{suffix}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
        log.info(f"[ns] debug pair written: {DEBUG_DIR}/{stamp}_{tag}_*.wav")
    except Exception as e:
        log.warning(f"[ns] debug dump failed: {e}")
