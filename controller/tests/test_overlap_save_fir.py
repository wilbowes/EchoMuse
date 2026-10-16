"""
Tests for em_eq._OverlapSaveFIR — the FFT-based streaming FIR convolution
backing Radar's stock EQ curve option.

The thing most likely to be subtly wrong here is the overlap bookkeeping:
a block-boundary error does not crash, it just produces a slightly wrong
sample at every chunk seam, which is exactly the kind of bug a single
one-shot test cannot catch. So the primary tool here is comparing against
`numpy.convolve` (a independent, trusted implementation) across MANY
different ways of splitting the same signal into chunks — if overlap-save
is correct, chunking can never matter; if it is wrong, it usually only
shows up for some chunk sizes and not others.
"""


import numpy as np
import pytest

import em_eq

RATE = 48000


def _reference_filter(rng, length=2048):
    """A filter with the right SHAPE of problem to catch bugs: not flat,
    not symmetric, long enough that getting the overlap length wrong by
    even one sample is visible."""
    return rng.standard_normal(length) * 0.05


def _naive_linear_convolve(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Ground truth: full linear convolution, trimmed to len(x) — i.e. the
    causal filtering of x by h, same as what a direct per-sample FIR
    implementation would produce for a stream padded with silence before
    it started."""
    return np.convolve(x, h, mode="full")[: x.size]


@pytest.mark.parametrize("chunking", [
    lambda n: [n],                                  # one shot
    lambda n: [1] * n,                               # one sample at a time
    lambda n: [n // 2, n - n // 2],                  # two halves
    lambda n: [7, 13, 1, 500, 2000, n - 2521],       # irregular, includes
                                                      # chunks both shorter
                                                      # and longer than the
                                                      # filter's own length
    lambda n: [2048] * (n // 2048) + [n % 2048] if n % 2048 else [2048] * (n // 2048),
])
def test_matches_naive_convolution_regardless_of_chunking(chunking):
    rng = np.random.default_rng(0)
    h = _reference_filter(rng)
    x = rng.standard_normal(10000)
    want = _naive_linear_convolve(x, h)

    fir = em_eq._OverlapSaveFIR(h)
    sizes = [s for s in chunking(x.size) if s > 0]
    assert sum(sizes) == x.size, "test chunking must cover the whole signal"

    got = []
    pos = 0
    for size in sizes:
        got.append(fir.process(x[pos:pos + size]))
        pos += size
    got = np.concatenate(got)

    assert got.shape == want.shape
    # float64 FFT round-trip error, not a tolerance for being approximately
    # right — this should be last-few-ULP close.
    np.testing.assert_allclose(got, want, atol=1e-8, rtol=1e-8)


def test_output_length_always_equals_input_length():
    rng = np.random.default_rng(1)
    fir = em_eq._OverlapSaveFIR(_reference_filter(rng))
    for size in (0, 1, 3, 2047, 2048, 2049, 9999):
        x = rng.standard_normal(size)
        out = fir.process(x)
        assert out.size == size, f"chunk size {size} -> output {out.size}"


def test_reset_clears_carried_history():
    rng = np.random.default_rng(2)
    h = _reference_filter(rng)
    x = rng.standard_normal(5000)

    a = em_eq._OverlapSaveFIR(h)
    a.process(x)  # prime its history with something

    b = em_eq._OverlapSaveFIR(h)
    a.reset()

    y = rng.standard_normal(3000)
    got_a = a.process(y)
    got_b = b.process(y)
    np.testing.assert_allclose(got_a, got_b, atol=1e-10)


def test_zero_filter_is_silence():
    fir = em_eq._OverlapSaveFIR(np.zeros(2048))
    out = fir.process(np.full(4096, 12345.0))
    assert np.all(out == 0.0)


def test_impulse_filter_is_identity_delayed_by_nothing():
    """A filter that is 1.0 at tap 0 and 0 elsewhere must be a pure
    passthrough — this isolates the overlap/indexing logic from any
    filter-shape effects."""
    h = np.zeros(2048)
    h[0] = 1.0
    fir = em_eq._OverlapSaveFIR(h)
    rng = np.random.default_rng(3)
    x = rng.standard_normal(5000)
    got = np.concatenate([fir.process(x[:2000]), fir.process(x[2000:])])
    np.testing.assert_allclose(got, x, atol=1e-10)


# ─── The actual Radar data, if present ─────────────────────────────────────
#
# Skipped rather than failed without ECHOMUSE_RADAR_TUNING: the Echo's files
# are not in this repository (see em_radar_tuning).

def _radar_taps(radar_tuning):
    return np.asarray(radar_tuning.fir_bands[0])


def test_radar_taps_load_and_match_the_measured_response(radar_tuning):
    """Sanity-checks the loaded data against the frequency-response shape
    measured directly from the extracted firmware file (see the session's
    own FFT analysis): strong bass lift around 80Hz, a steep drop by
    200Hz. Catches a taps file that loaded but is truncated, reordered or
    otherwise not what it claims to be."""
    taps = _radar_taps(radar_tuning)
    assert taps.size == 2048

    from scipy.signal import freqz
    w, h = freqz(taps, worN=8192, fs=RATE)
    mag_db = 20 * np.log10(np.abs(h) + 1e-12)

    def at(freq):
        return mag_db[np.argmin(np.abs(w - freq))]

    assert at(80) > 5.0, "expected a strong bass lift around 80Hz"
    assert at(200) < -3.0, "expected a steep drop by 200Hz"


def test_stock_curve_is_additive_with_the_8_band_eq(radar_tuning):
    """stock_curve must layer under the 8 bands, not replace them — a
    user who also dials in band gains should still hear both."""
    rng = np.random.default_rng(4)
    pcm = (rng.standard_normal(8192) * 5000).astype(np.int16).tobytes()

    stock_only = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, stock_curve=True)
    flat = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, stock_curve=False)
    both = em_eq.StreamingEQ(RATE, bands=[6.0] * 8, stock_curve=True)

    out_stock = np.frombuffer(stock_only.process(pcm), dtype=np.int16)
    out_flat = np.frombuffer(flat.process(pcm), dtype=np.int16)
    out_both = np.frombuffer(both.process(pcm), dtype=np.int16)

    assert not np.array_equal(out_stock, out_flat), \
        "stock_curve=True must audibly differ from flat"
    assert not np.array_equal(out_both, out_stock), \
        "adding band gains on top of stock_curve must further change the output"


def test_stock_curve_without_the_data_file_falls_back_quietly(monkeypatch):
    """A controller without a Radar's files must not crash — it should run
    as if stock_curve were never requested."""
    import em_radar_tuning
    monkeypatch.setenv(em_radar_tuning.ENV, "")
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, stock_curve=True)
    assert eq._fir is None
    pcm = (np.full(1000, 1234)).astype(np.int16).tobytes()
    assert eq.process(pcm) == pcm  # flat EQ, no guard/limiter -> passthrough
