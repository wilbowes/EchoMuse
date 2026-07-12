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

    def __init__(self):
        sessions = _get_sessions()
        if sessions is None:
            raise RuntimeError(f"DTLN models not loadable from {MODEL_DIR}")
        self._m1, self._m2 = sessions
        self._states_1 = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._states_2 = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._in_buf   = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._out_buf  = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._pending  = b""

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
            out[i:i + BLOCK_SHIFT] = self._out_buf[:BLOCK_SHIFT]

        return (np.clip(out, -1.0, 0.999969) * 32768.0).astype(np.int16).tobytes()


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
