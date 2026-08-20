"""
em_limiter.py — look-ahead peak limiter for the speaker path.

Sits after the EQ and before int16 conversion, so the whole chain stays in
float and nothing is quantised twice.

WHY THIS EXISTS
---------------
`em_eq` used to end in `np.clip`, which is a hard clipper. The dashboard
offers eight ±12dB faders plus a presence boost, so any combination that
pushed past full scale was square-waved sample by sample, silently. Measured
on a speech-like signal at −1dBFS with a modest bass boost: 4.74% of samples
clipped (#231). That is the same failure as the DAC clipping fixed in #162,
one stage earlier and in our own software.

A gain trim is the obvious fix and the wrong one: attenuating by the chain's
peak response makes a +12dB bass boost 12dB quieter overall, which the user
experiences as "the EQ made it quiet". A limiter keeps the loudness and
controls only the peaks — which is why the stock firmware carries a
multiband compressor-limiter rather than a trim.

DESIGN
------
Three stages, all vectorised — this runs on every audio chunk, so a
per-sample Python loop is not available to us.

1. **Look-ahead.** The gain is computed from a running MAXIMUM of |x| over
   the next `lookahead` samples, so it has already come down by the time the
   peak arrives. That is what makes the attack inaudible without a separate
   attack filter, and it is why the limiter is not a clipper with extra
   steps.

2. **Slew-limited release, in dB.** Gain may fall instantly but may only
   RISE at `release_db_per_s`. Written as a recursion that would be
   sequential:

       g[n] = min(target[n], g[n-1] + slew)

   which is a running minimum in a sheared coordinate system, so it is exact
   and vectorised rather than approximated:

       e[k] = target_db[k] − slew·k
       g_db[n] = slew·n + min(e[0..n])            (np.minimum.accumulate)

   The shear is rebased per chunk so `slew·n` cannot grow without bound
   across a long stream.

3. **A final clip that must never engage.** Kept as a backstop, and it is
   counted: if `clipped` is ever non-zero the limiter has a bug, and a
   silent backstop is how the original problem survived.

STATE
-----
Carried across chunks: the tail of the previous chunk (for the look-ahead
window) and the last gain. Without it, every chunk boundary would restart
the release and the music path would pump audibly at each one — the same
reason `em_eq.StreamingEQ` carries its biquad states.
"""

import numpy as np

# Full-scale for S16_LE, and the ceiling the threshold is measured against.
# They differ by one: int16 runs -32768..+32767, so a threshold of 0dBFS
# taken against 32768 produces a sample that WRAPS to full-scale negative on
# the cast — the single worst artefact available, and the one this module
# exists to prevent.
_FULL_SCALE = 32768.0
_CEILING    = 32767.0

# Below this the signal is silence and the gain is left alone — dividing a
# threshold by a near-zero envelope produces a huge gain that then has to be
# clamped, and the clamp is where a click comes from.
_EPS = 1e-9

DEFAULT_THRESHOLD_DB    = -1.0
DEFAULT_LOOKAHEAD_MS    = 5.0
DEFAULT_RELEASE_MS      = 150.0

# `release_ms` is the time to recover THIS many dB of gain reduction, which
# is the only way to state a slew rate that means the same thing whether the
# limiter is pulling 1dB or 12dB. Documented on the dashboard control too.
RELEASE_REFERENCE_DB = 10.0


def _running_max(x: np.ndarray, window: int) -> np.ndarray:
    """
    Maximum over [n, n+window) for each n, with the tail padded by the last
    value rather than by zeros: a zero pad would let the gain spring back up
    inside the final samples of a chunk and undo the look-ahead exactly where
    the next chunk's peak is about to arrive.
    """
    if window <= 1:
        return x
    padded = np.concatenate([x, np.full(window - 1, x[-1] if len(x) else 0.0)])
    # Sliding window view is O(n·window) in memory but O(n) in time and needs
    # no scipy; window is ~240 samples at 5ms/48kHz, so this is cheap.
    strides = np.lib.stride_tricks.sliding_window_view(padded, window)
    return strides.max(axis=1)


class Limiter:
    """
    Streaming look-ahead peak limiter. One instance per audio stream.

    `process` takes and returns float samples in S16 units (±32768), matching
    what em_eq works in, so callers do not shuffle scales around.
    """

    def __init__(self,
                 sample_rate: int,
                 threshold_db: float = DEFAULT_THRESHOLD_DB,
                 lookahead_ms: float = DEFAULT_LOOKAHEAD_MS,
                 release_ms: float = DEFAULT_RELEASE_MS,
                 enabled: bool = True):
        self.sample_rate = int(sample_rate)
        # Bypassed rather than absent, so a stream can be toggled without
        # dropping the instance — see set_params.
        self.enabled = bool(enabled)
        self.threshold_db = 0.0
        self._thresh = _CEILING
        # Kept only so the setting can be reported. The gain law uses _slew;
        # this is the number a user set, which the slew cannot be read back
        # to without knowing the sample rate and the reference.
        self.release_ms = float(release_ms)

        self.lookahead = max(1, int(self.sample_rate * lookahead_ms / 1000.0))
        self._slew = 0.0
        self.set_params(threshold_db=threshold_db, release_ms=release_ms)

        # Carried state.
        #
        # The tail is PRIMED with look-ahead silence so that every process()
        # call returns exactly as many samples as it was given. Without the
        # priming the first call comes up short by the look-ahead, which is
        # invisible in a byte-accumulating caller and reshapes the frames of
        # one that sends what it gets back — em_player does the latter, so its
        # first period arrived 478 bytes short. A limiter must be a drop-in;
        # the cost is that the audio is delayed by 5ms, which nothing here can
        # perceive, and that the final 5ms lives in the tail until flush().
        self._tail = np.zeros(max(0, self.lookahead - 1), dtype=np.float64)
        self._gain_db = 0.0                          # last gain, dB (≤ 0)

        # Instrumentation. `clipped` must stay zero; see the module docstring.
        self.max_reduction_db = 0.0
        self.clipped = 0

    def set_params(self,
                   threshold_db: float | None = None,
                   release_ms: float | None = None,
                   enabled: bool | None = None) -> None:
        """
        Change the limiter mid-stream, without touching carried state.

        These are taste parameters tuned by ear in a real room, and the chain
        used to be built once per stream — so hearing a change meant skipping
        the track, which makes an A/B nearly impossible to judge. Only scalars
        move here: `lookahead` is deliberately not settable, because it sizes
        the held tail and changing it mid-stream would drop or duplicate the
        samples sitting in it.

        Bypass is a flag rather than a None instance for the same reason. The
        gain state, the tail and the 5ms of latency all persist while
        disabled, so toggling costs no click and no realignment.
        """
        if threshold_db is not None:
            # Above 0dBFS would ask the limiter to permit clipping, which is
            # the one thing it exists to prevent.
            self.threshold_db = float(min(threshold_db, 0.0))
            self._thresh = _CEILING * (10.0 ** (self.threshold_db / 20.0))
        if release_ms is not None:
            self.release_ms = float(release_ms)
            # dB per sample the gain may rise.
            self._slew = (RELEASE_REFERENCE_DB
                          / (max(1.0, float(release_ms)) / 1000.0)
                          / self.sample_rate)
        if enabled is not None:
            self.enabled = bool(enabled)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """
        Limit one chunk. Returns the same number of samples it was given.

        Internally the chunk is delayed by `lookahead`, which is what lets the
        gain lead the audio; the delay is absorbed by holding a tail rather
        than by returning short reads, so callers see a pure 1:1 transform.
        """
        if samples.size == 0:
            return samples

        x = np.asarray(samples, dtype=np.float64)

        # Prepend whatever the previous call held back, so the look-ahead
        # window spans the boundary.
        buf = np.concatenate([self._tail, x]) if self._tail.size else x

        if not self.enabled:
            # Bypassed: unity gain, but the tail bookkeeping below runs
            # unchanged so the stream keeps its latency and its sample
            # alignment. Dropping the delay instead would shift the audio by
            # 5ms at the moment of the toggle, which is a click.
            gain_db = np.zeros(buf.size, dtype=np.float64)
            return self._emit(buf, gain_db)

        # 1. Look-ahead envelope.
        env = _running_max(np.abs(buf), self.lookahead)

        # 2. Target gain, then slew-limited release in dB.
        with np.errstate(divide="ignore", invalid="ignore"):
            target = np.where(env > _EPS, self._thresh / np.maximum(env, _EPS), 1.0)
        target = np.minimum(target, 1.0)
        target_db = 20.0 * np.log10(np.maximum(target, 1e-12))

        n = np.arange(target_db.size, dtype=np.float64)
        # Shear, take the running minimum, unshear.
        #
        # The seed carries the previous chunk's history into this one. It is
        # `_gain_db + _slew`, NOT `_gain_db`: the shear is rebased to local
        # index 0, and the release permits one sample of rise between the last
        # emitted sample and this one. Seeding with `_gain_db` alone withholds
        # that single step, which is inaudible on its own (~0.0014dB) and
        # makes the streaming path drift from the one-shot path — so the music
        # feed and TTS would no longer produce identical audio. Caught by
        # test_chunked_is_identical_to_one_shot at 37 chunks and above.
        sheared = target_db - self._slew * n
        sheared[0] = min(sheared[0], self._gain_db + self._slew)
        gain_db = self._slew * n + np.minimum.accumulate(sheared)
        gain_db = np.minimum(gain_db, 0.0)

        return self._emit(buf, gain_db)

    def _emit(self, buf: np.ndarray, gain_db: np.ndarray) -> np.ndarray:
        """
        Apply the gain, hold back the look-ahead, and carry the state.

        Shared by the limiting and bypassed paths so a toggle cannot change
        the stream's framing or latency — only whether the gain is unity.
        """
        out = buf * (10.0 ** (gain_db / 20.0))

        # 3. Emit everything except the final `lookahead` samples, which have
        # not yet seen their whole window; hold them for next time.
        hold = self.lookahead - 1
        if hold > 0:
            emit, self._tail = out[:-hold], buf[-hold:]
            self._gain_db = float(gain_db[-hold - 1]) if gain_db.size > hold else self._gain_db
        else:
            emit, self._tail = out, np.zeros(0, dtype=np.float64)
            self._gain_db = float(gain_db[-1])

        if emit.size:
            self.max_reduction_db = max(self.max_reduction_db,
                                        float(-gain_db[:emit.size].min()))
            self.clipped += int(np.count_nonzero(np.abs(emit) > _CEILING))

        # The first call emits `lookahead-1` fewer samples than it was given
        # (they are in the tail) and every later call emits that many more.
        # Callers stream, so a constant few-ms latency is invisible — but a
        # caller that expects 1:1 on a single short buffer would see a short
        # read, which `flush()` exists to settle.
        return emit

    def flush(self) -> np.ndarray:
        """
        Emit the held tail at the end of a stream.

        Without this the last few milliseconds of every response are dropped —
        inaudible on a long track and exactly the kind of thing that goes
        unnoticed until someone plays a very short announcement.
        """
        if not self._tail.size:
            return np.zeros(0, dtype=np.float64)
        tail, self._tail = self._tail, np.zeros(0, dtype=np.float64)
        # No further look-ahead is possible, so hold the last gain rather than
        # springing back to unity, which would be an audible step.
        out = tail * (10.0 ** (self._gain_db / 20.0))
        self.clipped += int(np.count_nonzero(np.abs(out) > _CEILING))
        return out


def for_stream(sample_rate: int,
               enabled: bool,
               threshold_db: float = DEFAULT_THRESHOLD_DB,
               release_ms: float = DEFAULT_RELEASE_MS) -> "Limiter | None":
    """
    Build a limiter for one stream, or None when disabled.

    ONE INSTANCE PER STREAM, never shared: it carries look-ahead and gain
    state, so two streams through one limiter would duck each other — a voice
    response would pull the gain down on the music playing underneath it.

    Lives here rather than in em_controller so em_player can use it too
    without importing em_controller, which would be circular. It takes plain
    values rather than a Device for the same reason every other pure module
    in this tree does: the test suite cannot import em_controller.
    """
    if not enabled:
        return None
    return Limiter(sample_rate,
                   threshold_db=threshold_db,
                   release_ms=release_ms)
