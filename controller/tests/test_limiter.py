"""
Tests for the speaker-path limiter.

The limiter exists because em_eq hard-clipped anything the EQ boosted past
full scale (#231). So the tests that matter are the ones that would have
caught THAT: does a boosted signal come out unclipped, and does the streaming
path behave identically to the one-shot path — because the music feed uses
one and TTS the other, and a limiter that pumps at chunk boundaries is a new
artefact rather than a fix.
"""

import numpy as np
import pytest

import em_limiter as L

FS = 48000
FULL = 32768.0


def _sine(freq=440.0, seconds=0.5, amp=1.0, fs=FS):
    t = np.arange(int(fs * seconds)) / fs
    return np.sin(2 * np.pi * freq * t) * FULL * amp


def _run(lim, x, chunks=1):
    """Full stream through the limiter, including the flushed tail."""
    parts = [lim.process(c) for c in np.array_split(x, chunks)]
    parts.append(lim.flush())
    return np.concatenate(parts)


def _delay(lim):
    """
    Samples of look-ahead latency. The limiter primes its tail with silence so
    process() is 1:1, which means the audio comes out shifted by this much.
    """
    return lim.lookahead - 1


def _aligned(lim, out):
    """Output with the look-ahead delay removed, for comparing against input."""
    return out[_delay(lim):]


# ─── The thing it was built for ──────────────────────────────────────────────

def test_a_signal_over_full_scale_comes_out_under_threshold():
    lim = L.Limiter(FS)
    out = _run(lim, _sine(amp=2.0))
    ceiling = FULL * 10 ** (L.DEFAULT_THRESHOLD_DB / 20.0)
    assert np.abs(out).max() <= ceiling + 1e-6
    assert lim.clipped == 0


@pytest.mark.parametrize("amp", [1.2, 2.0, 4.0, 16.0])
def test_nothing_clips_however_hard_it_is_driven(amp):
    """
    The EQ can apply +12dB and the presence boost stacks on top, so the
    limiter must hold for inputs far above full scale rather than for a
    plausible range.
    """
    lim = L.Limiter(FS)
    out = _run(lim, _sine(amp=amp))
    assert lim.clipped == 0, "the backstop clip engaged, so the limiter has a bug"
    assert np.abs(out).max() <= FULL


def test_the_backstop_clip_is_counted_not_silent():
    """
    `clipped` must stay zero in every other test. It is asserted here as a
    real attribute rather than assumed, because a silent backstop is exactly
    how the original hard clip survived unnoticed.
    """
    lim = L.Limiter(FS)
    assert lim.clipped == 0
    _run(lim, _sine(amp=3.0))
    assert lim.clipped == 0


# ─── Transparency: it must not touch audio that does not need it ─────────────

def test_signal_below_threshold_is_returned_unchanged():
    """Transparency: below the threshold the limiter is a pure delay."""
    lim = L.Limiter(FS)
    x = _sine(amp=0.5)
    out = _aligned(lim, _run(lim, x))
    assert np.allclose(out, x, atol=1e-6), "quiet audio must pass through untouched"
    assert lim.max_reduction_db == 0.0


def test_silence_does_not_produce_a_gain_excursion():
    """
    A near-zero envelope divides into a huge target gain, and clamping that
    is where a click comes from. Silence must simply stay silent.
    """
    lim = L.Limiter(FS)
    out = _run(lim, np.zeros(FS // 10))
    assert np.abs(out).max() == 0.0


# ─── Streaming parity ────────────────────────────────────────────────────────

@pytest.mark.parametrize("chunks", [2, 7, 37, 512])
def test_chunked_is_identical_to_one_shot(chunks):
    """
    TTS goes through one call and music through many. If they differ, the
    music path has an artefact at every chunk boundary — the same failure
    em_eq.StreamingEQ carries its biquad state to avoid.
    """
    x = _sine(amp=2.0, seconds=0.3)
    one = _run(L.Limiter(FS), x, chunks=1)
    many = _run(L.Limiter(FS), x, chunks=chunks)
    assert one.shape == many.shape
    assert np.allclose(one, many)


def test_process_returns_exactly_what_it_was_given():
    """
    1:1 per call, which is what makes this a drop-in.

    em_player sends back whatever the EQ chain returns, so a short buffer
    becomes a short audio frame on the wire — its first period arrived 478
    bytes short before the look-ahead was primed with silence.
    """
    lim = L.Limiter(FS)
    for chunk in np.array_split(_sine(amp=2.0, seconds=0.25), 11):
        assert lim.process(chunk).size == chunk.size


def test_the_stream_gains_only_the_lookahead_tail():
    """
    Total out = total in + the flushed tail. Any other relationship means
    playback drifts against the durations the controller computes.
    """
    lim = L.Limiter(FS)
    x = _sine(amp=2.0, seconds=0.25)
    out = _run(lim, x, chunks=11)
    assert out.size == x.size + _delay(lim)


def test_flush_emits_the_tail_and_can_be_called_once():
    """
    Without flush the last few ms of every response are dropped — inaudible
    on a track and obvious on a short announcement.
    """
    lim = L.Limiter(FS)
    x = _sine(amp=2.0, seconds=0.05)
    body = lim.process(x)
    tail = lim.flush()
    assert body.size == x.size
    assert tail.size == _delay(lim)
    assert lim.flush().size == 0, "a second flush must not re-emit"


def test_a_buffer_shorter_than_the_lookahead_is_not_lost():
    lim = L.Limiter(FS)
    x = _sine(amp=2.0, seconds=0.001)          # ~48 samples, under a 5ms window
    out = np.concatenate([lim.process(x), lim.flush()])
    assert out.size == x.size + _delay(lim)
    assert np.abs(out).max() <= 32767.0 + 1e-6


# ─── Attack and release character ────────────────────────────────────────────

def test_the_gain_is_already_down_when_the_peak_arrives():
    """
    This is the whole point of the look-ahead. A limiter that reacts on the
    peak itself has already let the peak through, which is a clipper.
    """
    x = np.zeros(FS // 10)
    x[FS // 20:] = FULL * 4.0                  # step into heavy overload
    lim = L.Limiter(FS)
    out = np.concatenate([lim.process(x), lim.flush()])
    ceiling = FULL * 10 ** (L.DEFAULT_THRESHOLD_DB / 20.0)
    assert np.abs(out).max() <= ceiling + 1e-6, "a peak got through unlimited"


def test_release_is_gradual_rather_than_instant():
    """
    Gain must fall fast and rise slowly. An instant return to unity after a
    transient is what pumping sounds like.

    Measured on the OUTPUT rather than on internal state: a steady tone after
    a burst should still be attenuated shortly afterwards, and back to its
    true level later. Reading `_gain_db` at the end of a long silence proves
    nothing, since by then it has correctly recovered.
    """
    # The burst needs ~12dB of reduction and release is 10dB per 400ms, so the
    # tone has to run past ~480ms for full recovery to be reachable at all.
    burst = np.full(2000, FULL * 4.0)
    tone = _sine(freq=200.0, seconds=1.2, amp=0.5)      # well under threshold
    x = np.concatenate([burst, tone])
    lim = L.Limiter(FS, release_ms=400.0)
    out = _run(lim, x)

    after = out[2000 + _delay(lim):]
    early = np.abs(after[:2000]).max()               # ~40ms after the burst
    late = np.abs(after[-2000:]).max()               # ~1.16s after
    assert early < late, "gain snapped back instantly — that is pumping"
    assert late == pytest.approx(np.abs(tone).max(), rel=0.02), \
        "the tone never returned to its own level"


def test_a_faster_release_recovers_sooner():
    """Pins the direction of the knob, which is what a user is choosing."""
    x = np.concatenate([np.full(2000, FULL * 4.0),
                        _sine(freq=200.0, seconds=0.2, amp=0.5)])
    s_lim, f_lim = L.Limiter(FS, release_ms=4000.0), L.Limiter(FS, release_ms=20.0)
    slow = np.abs(_run(s_lim, x)[2000 + _delay(s_lim):]).max()
    fast = np.abs(_run(f_lim, x)[2000 + _delay(f_lim):]).max()
    assert fast > slow


# ─── Parameter handling ──────────────────────────────────────────────────────

def test_threshold_above_zero_dbfs_is_refused():
    """
    A threshold over full scale asks the limiter to permit clipping, which is
    the one thing it exists to prevent. Clamped rather than raising, so a bad
    stored config degrades to safe behaviour instead of killing playback.
    """
    lim = L.Limiter(FS, threshold_db=6.0)
    assert lim.threshold_db == 0.0
    out = _run(lim, _sine(amp=2.0))
    # 32767, not 32768: int16 is asymmetric and 32768 wraps on the cast.
    assert np.abs(out).max() <= 32767.0 + 1e-6


def test_a_lower_threshold_pulls_the_peak_lower():
    for db in (-1.0, -3.0, -6.0):
        lim = L.Limiter(FS, threshold_db=db)
        out = _run(lim, _sine(amp=2.0))
        assert np.abs(out).max() <= FULL * 10 ** (db / 20.0) + 1e-6


def test_reduction_is_reported():
    """Gain reduction has to be visible, or the limiter is another silent stage."""
    lim = L.Limiter(FS)
    _run(lim, _sine(amp=2.0))
    assert lim.max_reduction_db == pytest.approx(6.0 + 1.0, abs=0.3)


# ─── #275: bypassed clipping is the backstop working, not a bug ──────────────

def test_bypassed_limiter_counts_its_clips_separately():
    """
    While limiterEnabled is off, a boosted EQ ahead legitimately pushes
    samples past the ceiling for the backstop np.clip to catch — measured
    in #275: 86 clipped samples across the sweep_params fixture. Counting
    those into `clipped` made "non-zero means the limiter has a bug" send
    someone hunting a bug in a limiter that was behaving correctly. The
    two states want opposite investigations, so they get two counters.
    """
    lim = L.Limiter(FS, enabled=False)
    hot = _sine(amp=4.0)                     # far past the ceiling
    out = _run(lim, hot)
    assert lim.clipped == 0, \
        "a bypassed limiter must not read as a buggy limiting one"
    assert lim.clipped_bypassed > 0, \
        "over-ceiling samples while bypassed must be counted - visibly"
    # And they pass through hot: nobody is limiting this signal.
    assert np.abs(out).max() > L._CEILING


def test_enabling_moves_the_count_back_to_clipped():
    """
    The same material through an ENABLED limiter must land in `clipped`
    if the backstop ever engages - and never in clipped_bypassed.
    """
    lim = L.Limiter(FS)
    _run(lim, _sine(amp=3.0))
    assert lim.clipped_bypassed == 0


def test_toggle_midstream_splits_the_two_counters():
    """
    set_params(enabled=False) mid-stream is supported (the music path
    re-reads config per chunk). Clips after the toggle belong to the
    bypassed counter alone; the enabled half never leaks into it.
    """
    lim = L.Limiter(FS)
    x = _sine(amp=3.0)
    lim.process(x[: len(x) // 2])            # enabled: limited, nothing clipped
    lim.set_params(enabled=False)
    lim.process(x[len(x) // 2:])
    lim.flush()
    assert lim.clipped_bypassed > 0, \
        "hot material after the toggle must be visible as bypassed clipping"
    assert lim.clipped == 0, \
        "post-toggle clips must not read as a buggy limiting limiter"
