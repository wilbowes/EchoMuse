"""
Speech gate — a voice turn's audio reaches Home Assistant only once somebody
is actually speaking.

Why: Whisper does not return nothing for non-speech. A wake with nobody
talking after it — a false wake over music, or a person who changed their
mind — streamed ducked-music residue to HA, whose VAD engaged on it, and
Whisper transcribed it as "Thank you. Thank you." The assistant then answered
("you're very welcome") a question nobody asked. Measured 2026-09-22 on VVV:
four such turns in eight minutes over music.

HA's own VAD cannot be the gate, because it is what engaged. The RMS check
in `_stream_mic_audio` cannot either: residue is by definition above the
room's floor. Silero VAD separates the two cleanly on the recordings we have
— every real utterance peaked 0.72-1.00, both silent turns 0.02-0.03 — and
costs ~0.4ms per 80ms frame. The model ships inside openwakeword, which the
controller already carries, so nothing new is distributed.

The gate HOLDS audio rather than dropping it. Once a frame scores at the
threshold, everything held is released in order ahead of it, so HA receives
exactly the stream it would have received ungated, only later. That matters:
HA's microVAD spends its first 760ms returning a -1 sentinel, and trimming
the lead-in to a short preroll would eat the start of short commands ("stop")
— the failure `ha_vad_stalled_verdict` exists for. If the gate never opens,
nothing is sent and the turn ends on the ordinary no-speech timeout.

Lives on the controller, not the device, so it covers both listening modes,
every trigger (wake, button, follow-up) and any board built against the wire
spec. Privacy is unaffected: this runs after the Echo's own gate has opened.

Fails open. Without the model the turn streams exactly as before.

`SpeechGate` is pure and tested; `Silero` needs onnxruntime and is not.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque

import numpy as np

log = logging.getLogger("echomuse")

# Silero's usual operating point. Speech measured 0.72-1.00, non-speech
# 0.02-0.03, so this is not a tight call on the data we have.
THRESHOLD = 0.5

# Bound on held audio. The no-speech timeout ends a turn after 5s of audio
# with no speech, so this is only reached if that stops being true; keeping
# the newest audio is right then, since speech onset is at the end.
MAX_HOLD_S = 8.0
FRAME_S = 0.08

# 640 samples (40ms) per model call, two per 80ms frame. The frame's
# probability is their mean, which is how openwakeword's wrapper scores and
# what the thresholds above were measured with.
CHUNK = 640


class SpeechGate:
    """Holds a turn's frames until one scores as speech, then passes all."""

    def __init__(self, threshold: float = THRESHOLD, max_hold_s: float = MAX_HOLD_S):
        self.threshold = threshold
        self._held: deque[bytes] = deque(maxlen=max(1, int(max_hold_s / FRAME_S)))
        self.open = False
        self.peak = 0.0          # highest probability seen while held
        self.frames_seen = 0
        self.opened_at = -1      # frame index that opened it, -1 if never
        self.dropped = 0         # frames pushed out of a full hold

    def push(self, frame: bytes, prob: float) -> list[bytes]:
        """Take one frame and its speech probability; return what may be sent."""
        self.frames_seen += 1
        if self.open:
            return [frame]
        self.peak = max(self.peak, prob)
        if len(self._held) == self._held.maxlen:
            self.dropped += 1
        self._held.append(frame)
        if prob < self.threshold:
            return []
        self.open = True
        self.opened_at = self.frames_seen - 1
        out = list(self._held)
        self._held.clear()
        return out

    def summary(self) -> str:
        """One clause for the turn log: what the gate did."""
        if self.open:
            return (f"opened at frame {self.opened_at} "
                    f"(+{self.opened_at * FRAME_S * 1000:.0f}ms)")
        return (f"never opened: {self.frames_seen} frames held, "
                f"peak {self.peak:.2f} < {self.threshold:.2f}, nothing sent to HA")


_lock = threading.Lock()
_session = None
_load_failed = False


def _model_path() -> str | None:
    override = os.environ.get("SPEECH_GATE_MODEL")
    if override:
        return override
    try:
        import openwakeword
    except Exception:
        return None
    return os.path.join(os.path.dirname(openwakeword.__file__),
                        "resources", "models", "silero_vad.onnx")


def _get_session():
    """The shared inference session, loaded once. None if unavailable."""
    global _session, _load_failed
    with _lock:
        if _session is not None or _load_failed:
            return _session
        path = _model_path()
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            _session = ort.InferenceSession(path, sess_options=opts,
                                            providers=["CPUExecutionProvider"])
            log.info(f"Speech gate: Silero VAD loaded ({path})")
        except Exception as e:
            _load_failed = True
            log.warning(f"Speech gate unavailable ({e}) — turns stream ungated")
        return _session


class Silero:
    """Per-turn Silero state over the shared session. Not thread-safe: one
    turn scores its frames in order, one at a time."""

    def __init__(self, session):
        self._sess = session
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def prob(self, frame: bytes) -> float:
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32767.0
        ps = []
        for i in range(0, len(x) - CHUNK + 1, CHUNK):
            out, self._h, self._c = self._sess.run(
                None, {"input": x[None, i:i + CHUNK], "h": self._h,
                       "c": self._c, "sr": self._sr})
            ps.append(float(out[0][0]))
        return float(np.mean(ps)) if ps else 0.0


def new_turn() -> Silero | None:
    """A scorer for one turn, or None if the model is unavailable."""
    sess = _get_session()
    return Silero(sess) if sess is not None else None
