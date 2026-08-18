"""
Tests for the dynamic bass guard.

The tests that matter are the ones that catch the two ways this goes wrong
silently: colouring audio it was not asked to touch, and appearing to work
while removing nothing. The first implementation did the second — a
subtractive crossover measured 20dB of reduction inside the band and produced
0.4dB at the output, because subtracting a phase-shifted copy is not removing
a band. Every frequency-domain assertion here exists because of that.
"""

import numpy as np
import pytest

import em_mbc as M

FS = 48000
FULL = 32768.0


def _sine(freq, seconds=1.0, amp=0.5, fs=FS):
    t = np.arange(int(fs * seconds)) / fs
    return np.sin(2 * np.pi * freq * t) * FULL * amp


def _gain_db(guard, x, chunks=1):
    """
    Output level relative to input, measured past the filter's startup
    transient. Real streams do start abruptly, so the transient is genuine —
    it just is not what these tests are about.
    """
    out = np.concatenate([guard.process(c) for c in np.array_split(x, chunks)])
    half = len(x) // 2
    return 20 * np.log10(out[half:].std() / x[half:].std())


# ─── The crossover ───────────────────────────────────────────────────────────

def test_the_crossover_sums_flat():
    """
    Linkwitz-Riley's defining property, and the reason it is used here rather
    than the subtractive split that looks simpler. If this drifts, the guard
    colours every stream it is enabled on even when nothing is compressing.
    """
    assert M.crossover_flatness_db() < 0.001


def test_a_subtractive_split_would_not_have_worked():
    """
    Pins the measurement that killed the first implementation, so nobody
    'simplifies' the crossover back to `rest = x - lowpass`.

    At 60Hz a 4th-order lowpass passes 0.998 of the signal, and the residual
    is LARGER than the input — so attenuating the low band leaves the energy
    sitting in the remainder.
    """
    from scipy.signal import butter, sosfreqz
    sos = butter(4, M.CROSSOVER_HZ / (FS / 2), btype="low", output="sos")
    w, h = sosfreqz(sos, worN=4096, fs=FS)
    at60 = np.argmin(np.abs(w - 60.0))
    assert abs(h[at60]) > 0.99
    assert abs(1 - h[at60]) > 1.0, "the subtractive residual must be shown to be broken"


# ─── What it does to audio ───────────────────────────────────────────────────

@pytest.mark.parametrize("freq,at_least_db", [
    (40, 12.0),
    (60, 10.0),
    (100, 4.0),
])
def test_loud_bass_is_removed(freq, at_least_db):
    assert _gain_db(M.BassGuard(FS), _sine(freq)) <= -at_least_db


@pytest.mark.parametrize("freq", [300, 1000, 5000, 12000])
def test_everything_the_driver_can_reproduce_is_untouched(freq):
    """
    The guard must not become a broadband compressor by accident. 300Hz is
    already well clear of the crossover and is where speech fundamentals sit.
    """
    assert _gain_db(M.BassGuard(FS), _sine(freq)) == pytest.approx(0.0, abs=0.3)


def test_quiet_bass_keeps_its_low_end():
    """
    This is what makes it dynamic rather than a high-pass filter. Below the
    threshold the driver can deliver the excursion, so nothing is taken away.
    """
    quiet = _sine(60, amp=0.0005)          # about -66 dBFS
    assert _gain_db(M.BassGuard(FS), quiet) == pytest.approx(0.0, abs=0.05)


def test_it_is_transparent_when_nothing_crosses_the_threshold():
    """Flat magnitude AND no level change, on a full-range quiet signal."""
    rng = np.random.default_rng(3)
    quiet = rng.normal(0, FULL * 0.0005, FS)
    assert _gain_db(M.BassGuard(FS), quiet) == pytest.approx(0.0, abs=0.05)


# ─── The knob ────────────────────────────────────────────────────────────────

def test_the_guard_depth_bounds_the_reduction():
    for depth in (-6.0, -12.0, -20.0):
        g = M.BassGuard(FS, bass_guard_db=depth)
        g.process(_sine(40))
        assert g.max_reduction_db <= abs(depth) + 1e-6


def test_a_deeper_guard_removes_more_bass():
    shallow = _gain_db(M.BassGuard(FS, bass_guard_db=-6.0), _sine(40))
    deep = _gain_db(M.BassGuard(FS, bass_guard_db=-30.0), _sine(40))
    assert deep < shallow


def test_zero_depth_is_a_no_op_rather_than_a_surprise():
    """
    Someone setting the guard to 0 means "off". It must not still be filtering
    and re-summing with a subtle level change.
    """
    assert _gain_db(M.BassGuard(FS, bass_guard_db=0.0),
                    _sine(40)) == pytest.approx(0.0, abs=0.05)


def test_a_positive_depth_is_clamped():
    """A stored config must degrade to safe behaviour, never boost the bass."""
    assert M.BassGuard(FS, bass_guard_db=6.0).bass_guard_db == 0.0


# ─── Streaming ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chunks", [2, 7, 37, 512])
def test_chunked_matches_one_shot(chunks):
    """
    TTS arrives as one buffer and music as many. A difference here is an
    artefact at every chunk boundary of every track.
    """
    x = _sine(60, seconds=0.3)
    one = M.BassGuard(FS).process(x)
    g = M.BassGuard(FS)
    split = np.concatenate([g.process(c) for c in np.array_split(x, chunks)])
    assert split.shape == one.shape
    assert np.allclose(one, split)


def test_sample_count_is_preserved():
    """No look-ahead here, so this is a strict 1:1 transform."""
    g = M.BassGuard(FS)
    for chunk in np.array_split(_sine(60, seconds=0.2), 9):
        assert g.process(chunk).size == chunk.size


def test_empty_input_is_handled():
    assert M.BassGuard(FS).process(np.zeros(0)).size == 0


# ─── Factory ─────────────────────────────────────────────────────────────────

def test_disabled_returns_none_so_the_call_site_stays_simple():
    assert M.for_stream(FS, False) is None
    assert isinstance(M.for_stream(FS, True), M.BassGuard)


def test_the_parameters_come_from_the_measured_stock_configuration():
    """
    These are read off a device (#229), not chosen. If someone changes them
    they should have to change this test and say why.
    """
    assert M.CROSSOVER_HZ == 115.0
    assert M.BASS_RATIO == 20.0
    assert M.BASS_THRESHOLD_DB == -50.0
    assert M.BASS_RELEASE_MS == 200.0
