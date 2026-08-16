"""
The post-reset warm-up gate for openwakeword, as pure logic.

`openwakeword.Model.reset()` does not clear the classifier's input; it REFILLS
it with embeddings computed from four seconds of RANDOM NOISE — and so does
`AudioFeatures.__init__`, so a newly constructed model starts the same way:

    def reset(self):
        self.raw_data_buffer.clear()
        self.melspectrogram_buffer = np.ones((76, 32))
        self.accumulated_samples = 0
        self.raw_data_remainder = np.empty(0)
        self.feature_buffer = self._get_embeddings(
            np.random.randint(-1000, 1000, 16000*4).astype(np.int16))

Measured against the installed package: that leaves 41 noise embeddings in
`feature_buffer`, and the classifier's input is [1, 16, 96] — the most recent
FEATURE_WINDOW of them. So every prediction is partly a score OF THAT NOISE
until FEATURE_WINDOW real chunks have been fed, and the noise is unseeded, so
it lands somewhere the classifier likes occasionally rather than never.

That is not a theoretical concern. On a single fleet device over one day: 19
wake events that captured no speech at all, scoring up to 0.994, every one of
them starting 0.79–1.25s after a turn ended — which is where the reset is.
The upper end of that band is the tell: FEATURE_WINDOW chunks is 1.28s, and
nothing was ever observed past it, because by then the noise is shifted out.

It also fired the barge-in watcher, which resets on the same model and scores
immediately: twice within 9-10 chunks of starting, at 0.867 and 0.700, each
time CANCELLING a turn the user was waiting on. That is the worse of the two
symptoms — a false wake wastes a moment, a false barge loses the request.

Raising the threshold does not help and is the trap worth naming: these score
higher than real speech does, and two of the recorded events cleared a
threshold of 0.80. The scores are not marginal, they are confident scores of
noise.

Reproduced away from the room, so the mechanism is not inferred from field
data alone: score the SAME audio twice, once immediately after `reset()` and
once on a model already warmed on audio of the same kind. 60 trials per level,
uniform noise, hey_jarvis, over the max of the 16 chunks:

    input level    cold max   cold p95   warm max
    ±30            0.225      0.135      0.001
    ±300           0.346      0.244      0.008
    ±1000          0.333      0.292      0.004
    ±3000          0.464      0.417      0.004

Identical input, two orders of magnitude apart, and the level barely matters —
it is the window that decides. Note where the cold p95 lands against the
defaults: `bargeInThreshold` is 0.05, cleared at every level, which is why the
barge-in watcher — the one site that resets and scores immediately — was where
this hurt rather than merely showed. Pure digital silence never fires (0/150
resets), which is why the fault needed a room to appear in.

The firmware's own Go port of this pipeline does NOT have the bug: it starts
its feature buffer empty and `Score` returns `ErrNotReady` until FEATURE_WINDOW
real embeddings exist (`device/internal/wakeword/stream.go`). So on shadow
mode the two detectors were not comparing like with like for the first 1.28s
after every reset — the device declined to answer while this one scored noise.

Gating on this gate rather than skipping the chunk outright is deliberate. A
device that scores wake words itself reports them on the control plane, and
the wake listener acts on those in the same loop iteration (see
`em_shadow.decide_wake_source`); a `continue` here would swallow a detection
this model had no part in.

Note the real wake word is not lost, only delayed at worst. Its audio stays in
the window while the window fills, so an utterance spoken during warm-up is
still scored — at the moment the window goes clean instead of before it.
"""

# Embeddings the classifier consumes per prediction: the wake models' input
# shape is [1, FEATURE_WINDOW, 96]. One embedding is produced per 1280-sample
# (80ms) chunk, so this is also how many chunks must be fed after a reset
# before the window holds no reset noise — 1.28s.
FEATURE_WINDOW = 16


class WarmupGate:
    """
    Tracks whether a model's classifier window still contains reset noise.

    Call `reset()` wherever `Model.reset()` is called, and `feed()` exactly
    once per chunk scored. `feed()` returns whether that chunk's score can be
    trusted; it must be called for every chunk regardless of the score, or the
    window never fills.

    A NEW gate starts un-warmed, because a new model does too: `AudioFeatures`
    seeds `feature_buffer` with those same noise embeddings in its constructor,
    not only in `reset()`. So constructing a model and constructing a gate need
    no coordination — every gate begins where its model does. Starting ready
    would leave the first second after startup and after every wake-model swap
    unguarded, which is the one window nothing else covers.
    """

    def __init__(self, window: int = FEATURE_WINDOW) -> None:
        if window < 0:
            raise ValueError("window must not be negative")
        self._window = window
        self._fed = 0

    def reset(self) -> None:
        """Record that the model's buffers were just seeded with noise."""
        self._fed = 0

    def feed(self) -> bool:
        """
        Record one scored chunk. True if that chunk's score is trustworthy.

        Saturates rather than counting forever, so a long-running listener
        cannot overflow anything.
        """
        if self._fed < self._window:
            self._fed += 1
        return self._fed >= self._window

    @property
    def ready(self) -> bool:
        """Whether scores are currently trustworthy, without feeding."""
        return self._fed >= self._window

    @property
    def fed(self) -> int:
        """Chunks fed since the last reset, saturating at the window size."""
        return self._fed

    def progress(self) -> str:
        """`N/16`, for a log line explaining a suppressed detection."""
        return f"{self._fed}/{self._window}"
