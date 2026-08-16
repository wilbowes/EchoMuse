"""
The post-reset warm-up gate for openwakeword.

The bug it exists for: `Model.reset()` and `AudioFeatures.__init__` both fill
the classifier's input with embeddings of four seconds of RANDOM NOISE, so
until FEATURE_WINDOW real chunks have been fed, every score is partly a score
of that noise. Unseeded, so it lands somewhere the classifier likes
occasionally rather than never.

Measured on one device over a day before the fix: 19 wake events that captured
no speech at all, scoring up to 0.994, every one of them 0.79-1.25s after a
turn ended — the reset site — and none past 1.28s, which is what
FEATURE_WINDOW chunks of 80ms comes to. The same artefact fired the barge-in
watcher twice within 10 chunks of its own reset, at 0.867 and 0.700, each time
cancelling a turn the user was waiting on.
"""

import em_oww_warmup as W


def feed_n(gate, n):
    """Feed n chunks, returning the trust verdict for each."""
    return [gate.feed() for _ in range(n)]


# ─── The window ───────────────────────────────────────────────────────────────

def test_window_matches_the_classifier_input():
    """
    The wake models take [1, 16, 96] — sixteen embeddings, one per 80ms chunk.
    That is where the constant comes from; it is not a tuned timeout, and
    16 x 80ms = 1.28s is exactly the band the false wakes were observed in.
    """
    assert W.FEATURE_WINDOW == 16


def test_a_new_gate_is_not_ready():
    """
    A new model is noise-seeded by its own constructor, not only by reset(),
    so a new gate must not start trusted. Getting this backwards would leave
    controller startup and every wake-model swap unguarded.
    """
    assert not W.WarmupGate().ready


def test_trust_arrives_exactly_on_the_window_th_chunk():
    gate = W.WarmupGate()
    verdicts = feed_n(gate, W.FEATURE_WINDOW)
    assert verdicts[:-1] == [False] * (W.FEATURE_WINDOW - 1)
    assert verdicts[-1] is True
    assert gate.ready


def test_trust_persists_once_earned():
    gate = W.WarmupGate()
    feed_n(gate, W.FEATURE_WINDOW)
    assert all(feed_n(gate, 500))


def test_reset_disarms_a_warmed_gate():
    """The whole point: the gate has to re-arm at every reset, not just once."""
    gate = W.WarmupGate()
    feed_n(gate, W.FEATURE_WINDOW)
    assert gate.ready
    gate.reset()
    assert not gate.ready
    assert feed_n(gate, W.FEATURE_WINDOW)[-1] is True


def test_repeated_resets_each_require_a_full_window():
    gate = W.WarmupGate()
    for _ in range(5):
        gate.reset()
        assert feed_n(gate, W.FEATURE_WINDOW - 1) == [False] * (W.FEATURE_WINDOW - 1)
        assert gate.feed() is True


# ─── The failure it reproduces ────────────────────────────────────────────────

def test_a_confident_score_inside_the_window_is_not_trusted():
    """
    The recorded events scored 0.770-0.994 and two of them cleared a threshold
    of 0.80, which is why raising the threshold was never the fix. Trust is
    decided by position in the window, never by the score.
    """
    gate = W.WarmupGate()
    gate.reset()
    for chunk, score in enumerate([0.994, 0.900, 0.871, 0.789, 0.770], start=1):
        trusted = gate.feed()
        assert not trusted, f"chunk {chunk} scoring {score} must not fire"


def test_the_real_wake_word_is_delayed_not_lost():
    """
    A wake word spoken during warm-up still fires — its audio is in the window
    while the window fills, so it is scored the moment the window goes clean.
    A gate that latched the suppression instead would drop it.
    """
    gate = W.WarmupGate()
    gate.reset()
    feed_n(gate, W.FEATURE_WINDOW - 1)
    assert gate.feed() is True


# ─── Counter hygiene ──────────────────────────────────────────────────────────

def test_the_counter_saturates():
    """A listener runs for weeks; the counter must not grow without bound."""
    gate = W.WarmupGate()
    feed_n(gate, 10_000)
    assert gate.fed == W.FEATURE_WINDOW


def test_progress_reports_position_for_the_log_line():
    gate = W.WarmupGate()
    gate.reset()
    feed_n(gate, 3)
    assert gate.progress() == f"3/{W.FEATURE_WINDOW}"


def test_a_zero_window_is_always_ready():
    """Degenerate but well-defined — a gate configured off must not block."""
    gate = W.WarmupGate(window=0)
    assert gate.ready
    gate.reset()
    assert gate.feed() is True


def test_a_negative_window_is_refused():
    try:
        W.WarmupGate(window=-1)
    except ValueError:
        return
    raise AssertionError("a negative window must be refused")
