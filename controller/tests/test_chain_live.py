"""
Changing the output chain while a track is playing.

The limiter's threshold and release, and the bass guard's depth, are taste
parameters meant to be tuned by ear in a real room. They were read once, when
the music feed built its chain, so a change only took effect on the next
track — and a listening test on 2026-08-19 reported no audible difference from
any setting, because none of them were reaching the audio.

Everything here updates in place. The rule the tests exist to hold is that a
change must be AUDIBLE but not ABRUPT: the processors keep their filter state,
their gain state and the stream's latency across a change, so nothing clicks
at the moment somebody is listening for a difference.
"""

import numpy as np
import pytest

import em_eq
import em_limiter
import em_mbc

FS = 48000


def _sine(hz, n, amp=8000.0, phase=0.0):
    t = np.arange(n, dtype=np.float64) / FS
    return amp * np.sin(2 * np.pi * hz * t + phase)


def _pcm(samples):
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


# ── Limiter ───────────────────────────────────────────────────────────────────

def test_a_bypassed_limiter_passes_audio_through_untouched():
    """
    Bypass must be unity gain, not a gentler limiter. The signal here is well
    over the threshold, so anything other than passthrough shows up.
    """
    lim = em_limiter.Limiter(FS, threshold_db=-20.0, enabled=False)
    x = _sine(200, 4800, amp=30000.0)
    got = np.concatenate([lim.process(x.copy()), lim.flush()])

    # The tail is primed with look-ahead silence, so the stream comes out
    # delayed by that much — in both states, which is the point.
    hold = lim.lookahead - 1
    assert np.allclose(got[:hold], 0.0)
    assert np.allclose(got[hold:], x, atol=1e-9)
    assert lim.max_reduction_db == 0.0


def test_toggling_the_limiter_keeps_the_stream_aligned():
    """
    The look-ahead delays the audio by 5ms. If bypass skipped the delay, the
    toggle would jump the stream forward by that much — a click, and a
    permanent 5ms disagreement with anything else being mixed.

    So a bypassed limiter must still emit the same number of samples per call
    and still hold the same tail.
    """
    on = em_limiter.Limiter(FS, enabled=True)
    off = em_limiter.Limiter(FS, enabled=False)
    x = _sine(200, 2400)
    for _ in range(4):
        assert on.process(x.copy()).size == off.process(x.copy()).size


def test_the_threshold_takes_effect_mid_stream():
    lim = em_limiter.Limiter(FS, threshold_db=-1.0)
    loud = _sine(200, 2400, amp=30000.0)

    lim.process(loud.copy())
    gentle = lim.max_reduction_db

    lim.set_params(threshold_db=-20.0)
    lim.process(loud.copy())
    assert lim.max_reduction_db > gentle + 5.0


def test_toggling_the_limiter_does_not_reset_its_gain():
    """
    The gain is carried across chunks and released slowly. Rebuilding the
    limiter to change a setting would snap it back to unity, which on loud
    material is a jump of many dB in one sample.
    """
    lim = em_limiter.Limiter(FS, threshold_db=-20.0)
    lim.process(_sine(200, 2400, amp=30000.0))
    held = lim._gain_db
    assert held < -5.0

    lim.set_params(release_ms=250.0, threshold_db=-18.0)
    assert lim._gain_db == held


# ── Bass guard ────────────────────────────────────────────────────────────────

def test_bypass_is_zero_depth_on_the_same_path_not_a_skipped_filter():
    """
    Bypass runs the crossover and sums it, rather than returning the input.

    Stated exactly: a bypassed guard is bit-identical to an ENABLED guard at
    zero depth — same filters, same state, no gain. That is what makes a
    toggle silent, and it is what stops someone "simplifying" the branch to
    `return x`, which is a different signal: LR4's halves sum magnitude-flat
    (crossover_flatness_db measures it) but the sum is an ALLPASS, so the
    phase would step at the toggle.
    """
    x = np.random.default_rng(7).normal(0, 4000, 24000)

    bypassed = em_mbc.BassGuard(FS, bass_guard_db=-40.0, enabled=False)
    zero_depth = em_mbc.BassGuard(FS, bass_guard_db=0.0, enabled=True)
    assert np.allclose(bypassed.process(x.copy()),
                       zero_depth.process(x.copy()), atol=1e-9)

    # Magnitude-flat, by the module's own measure rather than a noisy FFT.
    assert abs(em_mbc.crossover_flatness_db()) < 0.01

    # But NOT the identity — the allpass phase is real, and is exactly why
    # the bypass must not be rewritten as a passthrough.
    assert not np.allclose(bypassed.process(x.copy()), x, atol=1.0)


def test_a_bypassed_guard_applies_no_reduction():
    guard = em_mbc.BassGuard(FS, bass_guard_db=-40.0, enabled=False)
    guard.process(_sine(50, 48000, amp=30000.0))
    assert guard.max_reduction_db == 0.0


def test_the_depth_takes_effect_mid_stream():
    guard = em_mbc.BassGuard(FS, bass_guard_db=-6.0)
    bass = _sine(50, 24000, amp=30000.0)

    guard.process(bass.copy())
    shallow = guard.max_reduction_db
    assert shallow == pytest.approx(6.0, abs=0.5)

    guard.set_params(bass_guard_db=-30.0)
    guard.process(bass.copy())
    assert guard.max_reduction_db > shallow + 10.0


def test_toggling_the_guard_keeps_the_crossover_running():
    """
    The biquads must stay warm while bypassed. If the filters were skipped,
    re-enabling would start them from zero state and ring — a thump on the
    toggle.

    Checked by comparing a guard toggled off then on against one that was
    never toggled: after the state has settled, the two must agree.
    """
    x = _sine(50, 4800, amp=30000.0)
    steady = em_mbc.BassGuard(FS, bass_guard_db=-20.0)
    toggled = em_mbc.BassGuard(FS, bass_guard_db=-20.0)

    steady.process(x.copy())
    toggled.set_params(enabled=False)
    toggled.process(x.copy())
    toggled.set_params(enabled=True)

    later = _sine(50, 4800, amp=30000.0, phase=np.pi / 3)
    a = steady.process(later.copy())
    b = toggled.process(later.copy())
    # Same filter state means the same output once both are running.
    assert np.abs(a - b).max() < np.abs(a).max() * 0.02


# ── The chain, as the feed drives it ──────────────────────────────────────────

def _eq():
    return em_eq.StreamingEQ(
        FS, [0.0] * em_eq.NUM_BANDS, False,
        limiter=em_limiter.Limiter(FS),
        guard=em_mbc.BassGuard(FS))


ARGS = dict(bands=[0.0] * em_eq.NUM_BANDS, loudness=False,
            limiter_enabled=True, limiter_threshold=-1.0,
            limiter_release=150.0, guard_enabled=True, guard_db=-20.0)


def test_update_reports_whether_anything_moved():
    eq = _eq()
    assert eq.update(**ARGS) is True          # first call always lands
    assert eq.update(**ARGS) is False         # nothing changed
    assert eq.update(**{**ARGS, "guard_db": -12.0}) is True


def test_update_does_not_rebuild_the_eq_for_an_unrelated_change():
    """
    update() runs per chunk, so rebuilding the biquads whenever anything moved
    would be needless work — and would discard the filter state mid-track.
    """
    eq = _eq()
    eq.update(**{**ARGS, "bands": [3.0] + [0.0] * (em_eq.NUM_BANDS - 1)})
    sos = eq._sos
    zi = eq._zi

    eq.update(**{**ARGS, "bands": [3.0] + [0.0] * (em_eq.NUM_BANDS - 1),
                 "guard_db": -30.0, "limiter_threshold": -6.0})
    assert eq._sos is sos, "EQ rebuilt for a change that was not the EQ"
    assert eq._zi is zi

    assert eq._limiter.threshold_db == -6.0
    assert eq._guard.bass_guard_db == -30.0


def test_a_band_change_keeps_the_filter_running():
    """
    Coefficients change; state does not. Zeroing it would be a transient at
    the exact moment the user is judging the change they just made.
    """
    eq = _eq()
    eq.update(**{**ARGS, "bands": [6.0] + [0.0] * (em_eq.NUM_BANDS - 1)})
    eq.process(_pcm(_sine(100, 2400)))
    before = eq._zi.copy()

    eq.update(**{**ARGS, "bands": [-6.0] + [0.0] * (em_eq.NUM_BANDS - 1)})
    assert np.array_equal(eq._zi, before), "filter state was reset"


def test_going_flat_and_back_is_handled():
    """
    A flat curve drops _sos entirely, so the way back has to allocate fresh
    state rather than reuse an array that is no longer there.
    """
    eq = _eq()
    eq.update(**{**ARGS, "bands": [6.0] + [0.0] * (em_eq.NUM_BANDS - 1)})
    assert eq._sos is not None

    eq.update(**ARGS)                      # back to flat
    assert eq._sos is None

    eq.update(**{**ARGS, "bands": [4.0] + [0.0] * (em_eq.NUM_BANDS - 1)})
    assert eq._sos is not None
    out = eq.process(_pcm(_sine(100, 2400)))
    assert len(out) == 2400 * 2


def test_the_chain_keeps_working_across_a_live_change():
    """
    End to end: a stream that changes settings mid-flight still returns the
    same number of bytes it was given, chunk for chunk. The feed sends what it
    gets back, so a short read would reshape the frames on the wire.
    """
    eq = _eq()
    eq.update(**ARGS)
    chunk = _pcm(_sine(60, 2400, amp=25000.0))

    sizes = []
    for i in range(8):
        if i == 4:
            eq.update(**{**ARGS, "guard_enabled": False,
                         "limiter_enabled": False})
        sizes.append(len(eq.process(chunk)))

    assert sizes[1:] == [len(chunk)] * 7
