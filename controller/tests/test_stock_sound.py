"""
The stock-sound profile (#229, #247): a stock Echo Dot plays through a driver
correction, three gentle compressor bands above the bass guard, and a volume
law applied before them. These hold the pieces to what they were built from:
our own measurement of the driver, the crossover maths, the compressor law,
and stock's volume behaviour.
"""

import numpy as np
import pytest
from scipy.signal import sosfreqz

import em_eq
import em_mbc
import em_volume

FS = 48000

# Our swept-sine measurement of this driver through its own loopback and
# microphone, three placements, relative to 1kHz (#247, 2026-08-29). The
# correction should be its inverse where the measurement is trustworthy:
# 200Hz-1.6kHz was placement-invariant to 1.7dB.
DRIVER_DB = {200: -21.4, 250: -17.9, 315: -12.2, 500: -2.4, 630: -0.1, 1000: 0.0}


def _response_db(sos, freqs):
    _, h = sosfreqz(sos, worN=np.asarray(freqs, dtype=float), fs=FS)
    return 20 * np.log10(np.abs(h))


def test_the_correction_is_the_measured_driver_inverted():
    freqs = sorted(DRIVER_DB)
    corr = _response_db(em_eq.correction_sos(FS), freqs)
    corr -= corr[freqs.index(1000)]
    for f, c in zip(freqs, corr):
        assert abs(c + DRIVER_DB[f]) <= 5.0, f"{f} Hz: correction {c:+.1f} dB, driver {DRIVER_DB[f]:+.1f} dB"


def test_the_correction_cuts_where_the_driver_peaks():
    # +8.9dB at 3.15kHz in every placement (#247); the correction must cut there.
    corr = _response_db(em_eq.correction_sos(FS), [1000, 3150])
    assert corr[1] - corr[0] < -6.0


def test_the_correction_is_stable():
    for row in em_eq.correction_sos(FS):
        assert np.all(np.abs(np.roots([1.0, row[4], row[5]])) < 1.0)


def test_the_four_band_split_sums_flat():
    """Bypassed, the four bands must add back to the input's magnitude,
    or the profile colours audio it is not compressing. The bass band skips
    both upper crossovers and the 115-500Hz band skips the 7.5kHz one;
    without their allpasses this measured 0.64dB of ripple."""
    g = em_mbc.BassGuard(FS, enabled=False, upper_bands=True)
    x = np.zeros(1 << 15)
    x[0] = 1.0
    h = np.abs(np.fft.rfft(g.process(x)))
    f = np.fft.rfftfreq(1 << 15, 1 / FS)
    band = (f > 20) & (f < 20000)
    assert 20 * np.log10(h[band].max() / h[band].min()) < 0.001


def _tone(freq, dbfs, seconds=1.0):
    t = np.arange(int(seconds * FS)) / FS
    return 32767 * 10 ** (dbfs / 20) * np.sin(2 * np.pi * freq * t)


@pytest.mark.parametrize("freq", [1000, 3000])
def test_a_quiet_upper_band_is_untouched(freq):
    g = em_mbc.BassGuard(FS, -30.0, upper_bands=True)
    y = g.process(_tone(freq, -20))
    x = _tone(freq, -20)
    tail = slice(FS // 2, None)
    assert 20 * np.log10(np.std(y[tail]) / np.std(x[tail])) == pytest.approx(0.0, abs=0.1)


def test_a_loud_upper_band_is_compressed_two_to_one():
    """A 1kHz tone at 0dBFS peak is 10dB over the -10dB threshold, so the
    gentle law should take off about half of that."""
    g = em_mbc.BassGuard(FS, -30.0, upper_bands=True)
    x = _tone(1000, 0)
    y = g.process(x)
    tail = slice(FS // 2, None)
    reduction = 20 * np.log10(np.std(y[tail]) / np.std(x[tail]))
    assert -6.0 < reduction < -4.0


def test_the_profile_floors_the_bass_band_as_deep_as_stock():
    """+26dB of correction at 150-200Hz holds speech far over the bass
    threshold; at the default -30dB floor the band played 10dB hotter than
    stock and the driver distorted."""
    g = em_mbc.BassGuard(FS, -30.0, upper_bands=True)
    g.process(_tone(80, 0))
    assert g.max_reduction_db == pytest.approx(40.0, abs=0.01)
    g.set_params(bass_guard_db=-20.0)
    g.process(_tone(80, 0))
    assert g._bass.floor_db == em_mbc.STOCK_BASS_FLOOR_DB
    assert em_mbc.BassGuard(FS, -30.0)._bass.floor_db == -30.0


def test_upper_bands_are_off_by_default():
    """The default chain must stay bit-exact with the vectors it was built
    against; only the stock-sound profile asks for the extra bands."""
    assert em_mbc.BassGuard(FS).upper_bands is False
    x = _tone(1000, 0)
    assert np.array_equal(em_mbc.BassGuard(FS).process(x), em_mbc.BassGuard(FS).process(x))


def test_the_stock_volume_law():
    assert em_volume.stock_level_db(0) is None
    assert em_volume.stock_level_db(em_volume.DEVICE_VOLUME_MAX) == em_volume.STOCK_CEILING_DB
    levels = [em_volume.stock_level_db(lv) for lv in range(1, em_volume.DEVICE_VOLUME_MAX + 1)]
    assert all(b > a for a, b in zip(levels, levels[1:])), "every level up is louder"


def test_every_button_press_is_four_db():
    """The physical buttons step 8 levels from 47 to 127 (internal/server
    volume.go). Under stock's table those presses were +2.9, +1.9, +2.7,
    +1.1, +0.3, +3.5 and then nothing: the complaint that started this."""
    presses = range(47, em_volume.DEVICE_VOLUME_MAX + 1, 8)
    steps = [em_volume.stock_level_db(b) - em_volume.stock_level_db(a)
             for a, b in zip(presses, presses[1:])]
    assert len(steps) == 10
    assert all(s == pytest.approx(4.0) for s in steps)


def test_the_stock_law_is_the_plain_law_lifted():
    for lv in (1, 13, 47, 64, 89, 114, 127):
        assert em_volume.stock_level_db(lv) - em_volume.level_to_db(lv) == em_volume.STOCK_CEILING_DB


# Stock's correction, absolute, read off its FIR (#247 provenance): it falls
# away below ~90Hz with a notch at 65Hz. Ours must never boost MORE than it
# there: the first trial did, by 15-28dB at 30-80Hz, and the driver distorted.
STOCK_LOW_DB = {30: 1.9, 40: 4.1, 50: 3.5, 60: -3.0, 64: -15.0, 66: -17.0, 70: -1.3, 80: 10.1, 100: 19.3}


def test_the_correction_does_not_boost_bass_the_driver_cannot_move():
    freqs = sorted(STOCK_LOW_DB)
    ours = _response_db(em_eq.correction_sos(FS), freqs)
    for f, o in zip(freqs, ours):
        assert o <= STOCK_LOW_DB[f] + 3.0, f"{f} Hz: ours {o:+.1f} dB, stock {STOCK_LOW_DB[f]:+.1f} dB"


def _thd_db(y, freq):
    y = y[FS // 2:]
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    bins = np.fft.rfftfreq(y.size, 1 / FS)

    def power(f):
        i = int(np.argmin(np.abs(bins - f)))
        return float((spec[i - 3:i + 4] ** 2).sum())
    return 10 * np.log10(sum(power(k * freq) for k in range(2, 8)) / power(freq))


@pytest.mark.parametrize("freq", [150, 200, 300])
def test_the_compressors_do_not_distort_a_steady_tone(freq):
    """Without a hold the gain recovered between a waveform's peaks and was
    cut on the next: -40dB THD (1%) here, heard as distortion on VVV."""
    g = em_mbc.BassGuard(FS, -30.0, upper_bands=True)
    y = g.process(_tone(freq, 0))
    assert _thd_db(y, freq) < -70.0


@pytest.mark.parametrize("hold_ms", [1.0, 1.02])   # an even and an odd window
def test_the_hold_is_a_trailing_minimum_across_chunks(hold_ms):
    band = em_mbc._BandGain(2.0, -10.0, 100.0, -40.0, FS, hold_ms=hold_ms)
    n = band._hold
    x = np.random.default_rng(0).normal(0, 20000, 5000)
    level = 20 * np.log10(np.maximum(np.abs(x) / 32768, 1e-12))
    target = np.maximum(-np.maximum(level + 10, 0) * 0.5, -40)
    padded = np.concatenate([np.zeros(n - 1), target])
    expected = np.array([padded[i:i + n].min() for i in range(target.size)])
    gain, want = 0.0, []
    for t in expected:   # instant attack, slew-limited release, one sample at a time
        gain = min(t, gain + band._slew, 0.0)
        want.append(gain)
    got = np.concatenate([band.gains_db(x[a:a + 777]) for a in range(0, x.size, 777)])
    assert np.allclose(got, want, atol=1e-9)


def test_the_upper_bands_do_not_grit_on_a_rising_syllable():
    """Two tones in the 115-500Hz band under a 4Hz syllable envelope, at
    0dBFS peak. With an instant attack the gain stepped down on each new
    peak: -39dB of energy away from the tones, heard as grit on VVV at every
    volume (2026-10-16). The 10ms attack gives -52."""
    t = np.arange(FS * 3) / FS
    env = 0.5 - 0.5 * np.cos(2 * np.pi * 4 * t)
    x = env * (np.sin(2 * np.pi * 200 * t) + np.sin(2 * np.pi * 300 * t)) * 0.5 * 32767
    g = em_mbc.BassGuard(FS, -30.0, upper_bands=True)
    y = np.concatenate([g.process(x[i:i + 1000]) for i in range(0, x.size, 1000)])[FS // 2:]
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size))) ** 2
    f = np.fft.rfftfreq(y.size, 1 / FS)
    tones = (abs(f - 200) < 25) | (abs(f - 300) < 25)
    assert 10 * np.log10(spec[~tones].sum() / spec.sum()) < -48.0


def test_the_attack_is_a_one_pole_on_the_held_target_across_chunks():
    band = em_mbc._BandGain(2.0, -10.0, 100.0, -40.0, FS, hold_ms=1.0, attack_ms=2.0)
    n = band._hold
    a = 1 - np.exp(-1 / (2.0 * FS / 1000))
    x = np.random.default_rng(1).normal(0, 20000, 5000)
    level = 20 * np.log10(np.maximum(np.abs(x) / 32768, 1e-12))
    target = np.maximum(-np.maximum(level + 10, 0) * 0.5, -40)
    padded = np.concatenate([np.zeros(n - 1), target])
    smooth, gain, want = 0.0, 0.0, []
    for i in range(target.size):
        smooth = a * padded[i:i + n].min() + (1 - a) * smooth
        gain = min(smooth, gain + band._slew, 0.0)
        want.append(gain)
    got = np.concatenate([band.gains_db(x[k:k + 777]) for k in range(0, x.size, 777)])
    assert np.allclose(got, want, atol=1e-9)


def test_the_voice_filter_adds_body_and_softens_presence():
    """Voice gets stock's warmth on top of the shared curve: +3dB around
    230Hz, less at 2.1kHz, and nothing much at 1kHz."""
    r = _response_db(em_eq.voice_sos(FS), [228, 1000, 2105])
    assert r[0] == pytest.approx(3.0, abs=0.2)
    assert abs(r[1]) < 1.0
    assert r[2] < -2.0
    for row in em_eq.voice_sos(FS):
        assert np.all(np.abs(np.roots([1.0, row[4], row[5]])) < 1.0)


def test_the_bass_band_is_held_harder_than_the_upper_two():
    """This driver's 2nd harmonic at 150-300Hz rises with level, so the
    115-500Hz band compresses earlier and harder than stock's 2:1 from -10."""
    (r2, t2, _), (r3, t3, _), (r4, t4, _) = em_mbc.UPPER_LAWS
    assert r2 > r3 and r2 > r4
    assert t2 < t3 and t2 < t4
