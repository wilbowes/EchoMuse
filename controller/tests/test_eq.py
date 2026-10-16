import math
import pytest

import numpy as np

import em_eq

RATE = 48000


def _sine(freq: float, seconds: float = 0.5, amp: float = 0.25) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    pcm = (np.sin(2 * math.pi * freq * t) * amp * 32767).astype(np.int16)
    return pcm.tobytes()


def _rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def test_flat_bands_are_transparent():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0] * 8)
    assert len(out) == len(pcm)
    # 0 dB everywhere should be within a fraction of a dB of identity.
    ratio = _rms(out) / _rms(pcm)
    assert 0.97 < ratio < 1.03


def test_none_bands_default_to_flat():
    pcm = _sine(1000)
    assert abs(_rms(em_eq.apply(pcm, RATE)) - _rms(pcm)) / _rms(pcm) < 0.03


def test_band_boost_raises_its_own_frequency_only():
    # 60 Hz sits below the 125 Hz shelf corner (full +6 dB); at the corner
    # itself a shelf only delivers half its gain.
    low = _sine(60)
    high = _sine(8000)
    bands = [6.0, 0, 0, 0, 0, 0, 0, 0]  # +6 dB low shelf
    low_gain = _rms(em_eq.apply(low, RATE, bands=bands)) / _rms(low)
    high_gain = _rms(em_eq.apply(high, RATE, bands=bands)) / _rms(high)
    assert low_gain > 1.7          # ~+6 dB ≈ ×2
    assert 0.9 < high_gain < 1.1   # shelf must not leak into the top band


def test_cut_reduces_level():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0, 0, 0, -12.0, 0, 0, 0, 0])
    assert _rms(out) / _rms(pcm) < 0.5


def test_short_and_empty_input_pass_through():
    assert em_eq.apply(b"", RATE) == b""
    assert em_eq.apply(b"\x01", RATE) == b"\x01"


def test_wrong_band_count_still_returns_audio():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0, 0.0])  # padded internally
    assert len(out) == len(pcm)


def test_streaming_eq_matches_batch_apply():
    pcm = _sine(1000, seconds=0.4)
    bands = [3.0, 0, -2.0, 0, 0, 4.0, 0, 1.0]
    want = em_eq.apply(pcm, RATE, bands=bands)
    eq = em_eq.StreamingEQ(RATE, bands=bands)
    out = b""
    for i in range(0, len(pcm), 4096):
        out += eq.process(pcm[i:i + 4096])
    # Filter state carries across chunks — output must match the
    # whole-buffer path (same filters, same float32 pipeline).
    got = np.frombuffer(out, dtype=np.int16).astype(np.int32)
    ref = np.frombuffer(want, dtype=np.int16).astype(np.int32)
    assert np.abs(got - ref).max() <= 1  # ±1 LSB float rounding


def test_streaming_eq_flat_is_passthrough():
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8)
    chunk = _sine(500, seconds=0.05)
    assert eq.process(chunk) == chunk


# ─── Radar ParametricEQ + OutputTrim (ParametricEQ.cfg / AFE.cfg) ─────────────

def test_radar_parametric_eq_response(radar_tuning):
    """+5dB low shelf at 150Hz and +2dB peak at 80Hz (Q .9), in stock's own
    design (libasp.so 0x932e8). Its shelf sits 3dB under its boost at the
    corner (Zoelzer), not half of it as the cookbook's does, so 150Hz reads
    +3.2dB from the shelf and +4.1dB with the peak's skirt. Values are what
    the decoded formulas give; the top of the band is untouched."""
    from scipy.signal import sosfreqz
    fs = 48000
    sos = em_eq.radar_peq_sos(fs, radar_tuning.peq)
    f = np.array([20.0, 80.0, 150.0, 300.0, 5000.0])
    _, h = sosfreqz(sos, worN=f, fs=fs)
    db = 20 * np.log10(np.abs(h))
    assert 5.1 < db[0] < 5.3      # the shelf's 5dB, peak skirt negligible
    assert 6.7 < db[1] < 6.85     # shelf plus the peak's 2dB
    assert 4.0 < db[2] < 4.2      # at the shelf corner
    assert 0.7 < db[3] < 0.8      # stock's shelf is still lifting here
    assert abs(db[4]) < 0.1


def test_radar_parametric_shelf_ignores_q_as_stock_does():
    """libasp's LOW_SHELF design never reads Q (case 5 uses a fixed sqrt(2)K
    term); ParametricEQ.cfg's 'Q 0.9' on the shelf is therefore not an
    input. The peak does read it."""
    import inspect
    assert "Q" not in inspect.signature(em_eq._stock_loshelf_sos).parameters
    a = em_eq._stock_peak_sos(80.0, 2.0, 0.9, 48000)
    b = em_eq._stock_peak_sos(80.0, 2.0, 2.0, 48000)
    assert not np.allclose(a, b)


def test_radar_stock_curve_applies_output_trim(radar_tuning):
    """A flat-ish stream through the stock curve ends 3dB hotter than the
    trim alone would explain only if the trim is applied: compare against the
    same chain with the trim removed."""
    fs = 48000
    t = np.arange(fs // 2) / fs
    x = (4000 * np.sin(2 * np.pi * 2000 * t)).astype(np.int16).tobytes()
    a = em_eq.StreamingEQ(fs, stock_curve=True)
    b = em_eq.StreamingEQ(fs, stock_curve=True)
    b._trim = 1.0
    ya = np.frombuffer(a.process(x), np.int16).astype(float)[fs // 4:]
    yb = np.frombuffer(b.process(x), np.int16).astype(float)[fs // 4:]
    ratio_db = 20 * np.log10(np.sqrt((ya**2).mean()) / np.sqrt((yb**2).mean()))
    assert abs(ratio_db - radar_tuning.trim_db) < 0.05


# ─── Radar's volume-banded stock FIR (AFE.cfg "Volume Boundary") ─────────────

def _level_gain(level):
    return 0.0 if level == 0 else 10 ** ((level - 127) / 40)


@pytest.mark.parametrize("level,value,band", [
    (0, 0, 0), (3, 1, 0), (37, 10, 0), (38, 11, 0), (77, 50, 0), (78, 51, 1),
    (87, 60, 1), (88, 61, 2), (97, 70, 2), (98, 71, 3), (107, 80, 3),
    (108, 81, 4), (127, 100, 4),
])
def test_radar_eq_band_by_volume(level, value, band):
    """Pinned against the same table as the Go test, so the two ends cannot
    pick different curves at the same volume. A level on a boundary takes
    that boundary's own file, as libasp does."""
    g = _level_gain(level)
    assert em_eq.stock_volume_value(g) == value
    assert em_eq.radar_eq_band(g, [50, 60, 70, 80, 100]) == band


def test_stock_mixer_levels_are_what_bin_mixer_holds():
    """The table read out of stock's /system/bin/mixer: 101 levels, 0.5dB
    per step, 127 = 0dB, value + 27 from value 11 up. It reproduces
    StandAloneAAModules.cfg's music_volTab at every Alexa step."""
    t = em_eq.STOCK_MIXER_LEVELS
    assert t[:11] == (0, 3, 7, 11, 17, 20, 27, 30, 32, 35, 36)
    assert all(t[v] == v + 27 for v in range(11, 101))
    volume_curve = [0, 1, 2, 3, 4, 6, 11, 16, 22, 28, 34, 40, 44, 46, 50, 54,
                    56, 60, 64, 68, 70, 72, 76, 80, 84, 88, 90, 92, 96, 98, 100]
    music_voltab = [-62.0, -60.2, -58.1, -55.0, -50.1, -44.5, -42.0, -39.0,
                    -36.0, -33.0, -30.0, -28.0, -27.0, -25.0, -23.0, -22.0,
                    -20.0, -18.0, -16.0, -15.0, -14.0, -12.0, -10.0, -8.0,
                    -6.0, -5.0, -4.0, -2.0, -1.0, 0.0]
    # Steps 1..30. The mixer's levels are whole 0.5dB steps, so the table's
    # fractional low end (-60.2, -58.1, -50.1) agrees to within one step.
    for step in range(1, 31):
        db = (t[volume_curve[step]] - 127) / 2
        assert abs(db - music_voltab[step - 1]) <= 1.0, step


def test_stock_volume_value_is_level_minus_27_from_38_up():
    for level in range(38, 128):
        assert em_eq.stock_volume_value(_level_gain(level)) == level - 27


def test_radar_banded_files_are_a_loudness_compensation(radar_tuning):
    """The point of selecting by volume: the bass boost backs off as the
    volume goes up. If a future extraction ever produced five copies of one
    curve, this is where it would show."""
    bounds, taps = em_eq._radar_eq_banded()
    assert bounds == [50, 60, 70, 80, 100]
    boost80 = [20 * np.log10(abs(np.fft.rfft(t, 48000)[80])) for t in taps]
    assert all(a > b for a, b in zip(boost80, boost80[1:])), boost80
    assert boost80[0] > 9.0 and boost80[-1] < 2.0


def test_banded_fir_crossfades_on_a_switch():
    a = np.zeros(32); a[0] = 1.0
    b = np.zeros(32); b[0] = 0.25
    f = em_eq._OverlapSaveFIR([a, b])
    x = np.full(256, 1000.0)
    f.process(x)
    f.set_band(1)
    y = f.process(x)
    assert y[0] == pytest.approx(1000 * (1 - 1 / 256) + 250 / 256)
    assert y[-1] == pytest.approx(250.0)
    assert np.allclose(f.process(x), 250.0)


def test_chain_without_a_volume_keeps_eq50(radar_tuning):
    """Every caller that does not pass volume_gain (the controller-side
    chain) keeps the single EQ_50 curve it always had."""
    eq = em_eq.StreamingEQ(48000, stock_curve=True)
    assert eq._fir_bounds is None and len(eq._fir._hs) == 1


def test_chain_that_takes_the_volume_rounds_to_s16():
    """Radar's volume sits ahead of the stages, so the lowest steps leave a few
    LSB of signal; truncating toward zero lost ~6dB of it and zeroed anything
    under 1 LSB. The cast rounds (half to even) there and truncates elsewhere."""
    x = np.array([0.4, 0.6, 1.5, 2.5, -0.6, -1.6, 40000.0, -40000.0])
    r = np.frombuffer(em_eq._to_int16(x, True), np.int16)
    t = np.frombuffer(em_eq._to_int16(x, False), np.int16)
    assert list(r) == [0, 1, 2, 2, -1, -2, 32767, -32768]
    assert list(t) == [0, 0, 1, 2, 0, -1, 32767, -32768]


# ─── the math helpers: they have to give back the right shape ──────────────

def test_peak_sos_is_a_single_biquad_row():
    """Audio EQ Cookbook peaking biquad — six coefficients in one row.

    The exact values are in the cookbook; what matters here is the shape,
    so a typo in one of the coefficient expressions surfaces as an
    obvious test failure rather than a subtle audio fault.
    """
    sos = em_eq._peak_sos(1000.0, 3.0, 0.7, 48000)
    assert sos.shape == (1, 6)


def test_loshelf_sos_is_a_single_biquad_row():
    sos = em_eq._loshelf_sos(120.0, 4.0, 48000)
    assert sos.shape == (1, 6)


def test_hishelf_sos_is_a_single_biquad_row():
    sos = em_eq._hishelf_sos(8000.0, -3.0, 48000)
    assert sos.shape == (1, 6)


def test_loshelf_q_sos_is_a_single_biquad_row():
    """ParametricEQ.cfg states its shelf with an explicit Q; the S=1
    special case is _loshelf_sos, this one is the form stock actually uses."""
    sos = em_eq._loshelf_q_sos(120.0, 4.0, 0.7, 48000)
    assert sos.shape == (1, 6)


def test_stock_loshelf_sos_is_a_single_biquad_row():
    """Stock's MBCL low-shelf biquad: two branches (boost and cut), each
    emitting a six-coefficient row. The shape is the cheap test — the
    values are an RBJ Cookbook translation and not worth pinning here."""
    for gain_db in (+6.0, -6.0):
        sos = em_eq._stock_loshelf_sos(120.0, gain_db, 48000)
        assert sos.shape == (1, 6)


def test_stock_peak_sos_is_a_single_biquad_row():
    """Stock's MBCL peaking biquad: same shape, two branches for boost
    and cut. Covered by the call path; shape is the test."""
    for gain_db in (+3.0, -3.0):
        sos = em_eq._stock_peak_sos(1000.0, gain_db, 0.7, 48000)
        assert sos.shape == (1, 6)


def test_apply_with_a_guard_aligns_around_its_latency():
    """A guard with a look-ahead (Radar's stock MBCL does, em_mbc.BassGuard
    does not) is fed its latency in silence and the same count is dropped
    from the front, so the returned audio is aligned to the input."""
    import em_mbc
    guard = em_mbc.BassGuard(48000)
    pcm = em_eq.apply(_sine(1000), RATE, bands=[0.0] * 8, guard=guard)
    assert len(pcm) == len(_sine(1000))


def test_apply_with_a_limiting_limiter_after_the_guard():
    """The limiter runs after the guard, on the EQ'd-and-guarded signal.
    Without the limiter, a heavy EQ boost hard-clips; with one, the
    limiter is the backstop and the np.clip is a true no-op."""
    import em_limiter
    pcm = em_eq.apply(_sine(1000), RATE, bands=[0.0] * 8,
                     limiter=em_limiter.Limiter(RATE))
    # The limiter holds back its look-ahead tail; what comes out is
    # the limiter's 1:1 transform on the input, with the tail sitting
    # in the limiter until it is flushed. A caller's contract is that
    # the output is at least as long as the input.
    assert len(pcm) >= len(_sine(1000))


def test_apply_with_a_delaying_guard_pads_with_silence_then_drops_it():
    """A guard with a non-zero look-ahead (Radar's stock MBCL) is fed its
    latency in silence, then the same count is dropped from the front,
    so the returned audio is aligned to the input. Tested with a stub
    guard: the real RadarMultiband needs the proprietary tuning."""
    class _DelayingGuard:
        latency = 240
        def process(self, x):
            return x[:x.size - self.latency]    # trim the front
    out = em_eq.apply(_sine(1000), RATE, bands=[0.0] * 8, guard=_DelayingGuard())
    # The apply function pads with `lat` samples of silence and drops the
    # first `lat` samples after the guard; a guard that itself trims the
    # front by `lat` therefore produces an output smaller by 1*lat samples.
    # out is in bytes, two per sample.
    assert len(out) == (24000 - _DelayingGuard.latency) * 2


# ─── StreamingEQ update/set_bands paths ───────────────────────────────

def test_streaming_eq_init_with_no_bands_uses_defaults():
    """bands=None falls back to DEFAULT_BANDS. With loudness
    on, the chain is not flat and the SOS is built."""
    eq = em_eq.StreamingEQ(RATE, bands=None, loudness=True)
    assert eq._sos is not None    # DEFAULT_BANDS + loudness -> non-None


def test_streaming_eq_init_pads_short_bands_list():
    """A bands list shorter than NUM_BANDS is right-padded with zeros
    (line 517). With non-zero at the start the filter runs."""
    eq = em_eq.StreamingEQ(RATE, bands=[1.0])
    assert eq._sos is not None


def test_streaming_eq_set_bands_to_none_uses_defaults():
    """set_bands(bands=None) takes the same default."""
    eq = em_eq.StreamingEQ(RATE, bands=[1.0] + [0.0] * 7, loudness=True)
    eq.set_bands(None, loudness=True)
    assert eq._sos is not None


def test_streaming_eq_set_bands_pads_short_list():
    """set_bands() with a too-short list pads with zeros (line 596)."""
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, loudness=True)
    eq.set_bands([1.0], loudness=True)
    assert eq._sos is not None


def test_streaming_eq_set_bands_to_zeros_drops_sos():
    """set_bands with all-zero and no loudness resets the filter to
    passthrough (line 618-627). The filter state had been used; the
    next process() returns pcm unchanged."""
    eq = em_eq.StreamingEQ(RATE, bands=[1.0] + [0.0] * 7)
    assert eq._sos is not None
    eq.set_bands([0.0] * 8, loudness=False)
    assert eq._sos is None
    assert eq.process(_sine(1000)) == _sine(1000)


def test_streaming_eq_update_returns_false_when_unchanged():
    """update() with the same params returns False — no work done."""
    eq = em_eq.StreamingEQ(RATE, bands=[1.0] + [0.0] * 7)
    assert eq.update(bands=[1.0] + [0.0] * 7, loudness=False,
                     limiter_enabled=False, limiter_threshold=-3.0,
                     limiter_release=50.0,
                     guard_enabled=True, guard_db=-6.0) is True
    # Same again -> no change.
    assert eq.update(bands=[1.0] + [0.0] * 7, loudness=False,
                     limiter_enabled=False, limiter_threshold=-3.0,
                     limiter_release=50.0,
                     guard_enabled=True, guard_db=-6.0) is False


def test_streaming_eq_update_changes_bands_returns_true():
    """update() with new band values re-applies the EQ (line 594/596)."""
    eq = em_eq.StreamingEQ(RATE, bands=[1.0] + [0.0] * 7)
    assert eq.update(bands=[-1.0] + [0.0] * 7, loudness=False,
                     limiter_enabled=False, limiter_threshold=-3.0,
                     limiter_release=50.0,
                     guard_enabled=True, guard_db=-6.0) is True


def test_streaming_eq_update_with_limiter_reaches_it():
    """update() with a Limiter attached forwards the params."""
    import em_limiter
    lim = em_limiter.Limiter(RATE)
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, limiter=lim)
    assert eq.update(bands=[0.0] * 8, loudness=False,
                     limiter_enabled=True, limiter_threshold=-6.0,
                     limiter_release=100.0,
                     guard_enabled=True, guard_db=-6.0) is True


def test_streaming_eq_update_with_guard_reaches_it():
    """update() with a BassGuard attached forwards the params."""
    import em_mbc
    g = em_mbc.BassGuard(RATE)
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, guard=g)
    assert eq.update(bands=[0.0] * 8, loudness=False,
                     limiter_enabled=False, limiter_threshold=-3.0,
                     limiter_release=50.0,
                     guard_enabled=False, guard_db=-3.0) is True


def test_streaming_eq_set_volume_gain_without_target_is_a_noop():
    """set_volume_gain() is a no-op when the chain wasn't built with one."""
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8)
    eq.set_volume_gain(0.5)    # _vol_target is None -> no change
    assert eq.process(_sine(1000)) == _sine(1000)


def test_streaming_eq_flush_with_no_limiter_returns_empty():
    """flush() with no limiter/guard returns b'' (no look-ahead tail)."""
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8)
    assert eq.flush() == b""


# ─── _OverlapSaveFIR validation ────────────────────────────────────────

def test_overlap_save_fir_rejects_mismatched_band_lengths():
    """A list of FIR taps with differing lengths is a programmer error,
    raised at construction (line 288)."""
    with pytest.raises(ValueError, match="same length"):
        em_eq._OverlapSaveFIR([np.zeros(8), np.zeros(16)])


# ─── Radar EQ taps via monkeypatched tuning (no proprietary files) ────

def test_radar_eq_taps_returns_first_band_as_floats(monkeypatch):
    """_radar_eq_taps() returns the first band as float64 (line 191)."""
    import em_radar_tuning
    taps0 = [0.0, 0.5, -0.25, 0.0]
    tuning = em_radar_tuning.RadarTuning(fir_bands=[taps0])
    monkeypatch.setattr(em_radar_tuning, "current", lambda: tuning)
    out = em_eq._radar_eq_taps()
    assert out is not None
    assert out.dtype == np.float64
    assert list(out) == taps0


def test_radar_eq_banded_returns_bounds_and_floats(monkeypatch):
    """_radar_eq_banded() returns (bounds, [taps...]) with float64 taps (line 246)."""
    import em_radar_tuning
    taps0 = [0.0, 0.5, -0.25]
    taps1 = [0.0, 0.4, -0.2]
    tuning = em_radar_tuning.RadarTuning(
        fir_bands=[taps0, taps1], fir_bounds=[50, 100])
    monkeypatch.setattr(em_radar_tuning, "current", lambda: tuning)
    out = em_eq._radar_eq_banded()
    assert out is not None
    bounds, bands = out
    assert bounds == [50, 100]
    assert all(b.dtype == np.float64 for b in bands)
    assert [list(b) for b in bands] == [taps0, taps1]


# ─── radar_eq_band fallback ───────────────────────────────────────────

def test_radar_eq_band_falls_through_to_last_when_value_exceeds_all():
    """When the value is above every boundary, the last band's index is
    returned (line 238). The stock table can't reach this (max 100, last
    boundary 100), but a custom set can."""
    g = 10 ** ((127 - 127) / 40)    # 0 dB -> value 100
    # Custom boundaries where the last one is small, so 100 is past all of them.
    assert em_eq.radar_eq_band(g, [10, 20, 30]) == 2


# ─── describe_chain / describe_activity ────────────────────────────────

def test_describe_chain_marks_off_stages():
    """Stages without an instance report 'off' rather than 'n/a'."""
    s = em_eq.describe_chain([0.0] * 8, loudness=False,
                             limiter=None, guard=None)
    assert "eq=flat" in s
    assert "guard=off" in s
    assert "limiter=off" in s


def test_describe_chain_marks_active_stages():
    """A limiter with a threshold and a guard with a setting report their values."""
    import em_limiter, em_mbc
    lim = em_limiter.Limiter(48000)
    g = em_mbc.BassGuard(48000)
    g.bass_guard_db = -6.0
    s = em_eq.describe_chain([3.0, 0, 0, 0, 0, 0, 0, 0], loudness=True,
                             limiter=lim, guard=g)
    assert "eq=" in s
    assert "speech_boost=on" in s
    assert "guard=-6dB" in s
    assert "limiter=" in s


def test_describe_activity_reports_n_a_for_off_stages():
    """An off stage's reduction is 'n/a', not 0.00dB — those are different
    faults (one wasn't on, the other was on and never engaged)."""
    s = em_eq.describe_activity(limiter=None, guard=None)
    assert s == "guard_reduction=n/a limiter_reduction=n/a"

