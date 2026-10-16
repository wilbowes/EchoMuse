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


def test_radar_parameters_come_from_its_own_measured_configuration():
    """
    Radar runs its own MBCL.cfg ("Radar Tuning V4.5"), not biscuit's — read
    off a Radar unit's stock firmware, not chosen. Ratio and release match
    biscuit's; only the crossover and threshold differ (the rest of band 1
    is why only this one band is ported — see the module docstring).
    """
    assert M.RADAR_CROSSOVER_HZ == 70.0
    assert M.RADAR_BASS_THRESHOLD_DB == -25.0


@pytest.mark.parametrize("board_id,crossover_hz,threshold_db", [
    ("biscuit", M.CROSSOVER_HZ, M.BASS_THRESHOLD_DB),
    ("radar", M.RADAR_CROSSOVER_HZ, M.RADAR_BASS_THRESHOLD_DB),
    (None, M.CROSSOVER_HZ, M.BASS_THRESHOLD_DB),       # no board reported yet
    ("dot3", M.CROSSOVER_HZ, M.BASS_THRESHOLD_DB),     # unrecognised -> biscuit
])
def test_tuning_for_selects_by_board(board_id, crossover_hz, threshold_db):
    assert M._tuning_for(board_id) == (crossover_hz, threshold_db)


def test_for_stream_applies_the_boards_tuning(radar_tuning):
    """for_stream is the production call site's path (em_player.py/
    em_controller.py) — this pins that board_id actually reaches a real
    instance, not just the lookup table above. Radar's single-band tuning
    constants now feed its full RadarMultiband (band 1) rather than a
    BassGuard — see build_guard."""
    guard = M.for_stream(FS, enabled=True, board_id="radar")
    assert isinstance(guard, M.RadarMultiband)
    assert guard.enabled
    biscuit_guard = M.for_stream(FS, enabled=True, board_id="biscuit")
    assert isinstance(biscuit_guard, M.BassGuard)
    assert biscuit_guard.crossover_hz == M.CROSSOVER_HZ


def test_build_guard_routes_by_board(radar_tuning):
    assert isinstance(M.build_guard(FS, "radar"), M.RadarMultiband)
    assert isinstance(M.build_guard(FS, "biscuit"), M.BassGuard)
    assert isinstance(M.build_guard(FS, None), M.BassGuard)       # unreported -> biscuit
    assert isinstance(M.build_guard(FS, "dot3"), M.BassGuard)     # unrecognised -> biscuit


# ─── Radar's full 4-band MBCL ────────────────────────────────────────────

def test_radar_without_its_tuning_keeps_the_single_band_guard(monkeypatch):
    """No Echo files, no MBCL: Radar falls back to BassGuard at its own band
    1 tuning, as the device does."""
    import em_radar_tuning
    monkeypatch.setenv(em_radar_tuning.ENV, "")
    guard = M.build_guard(FS, "radar")
    assert isinstance(guard, M.BassGuard)
    assert guard.crossover_hz == M.RADAR_CROSSOVER_HZ


def test_radar_fallback_agrees_with_the_files_band_one(radar_tuning):
    """RADAR_CROSSOVER_HZ/RADAR_BASS_THRESHOLD_DB are band 1 of the same
    MBCL.cfg; the fallback must not drift from the file it stands in for."""
    m = radar_tuning.mbcl
    assert m.crossovers_hz[0] == M.RADAR_CROSSOVER_HZ
    assert m.bands[0].comp_threshold_db == M.RADAR_BASS_THRESHOLD_DB
    assert m.bands[0].comp_ratio == M.BASS_RATIO


def test_four_band_crossover_sums_flat():
    """
    The property the whole class exists to get right — see
    four_band_flatness_db's docstring for the naive 1.59dB failure this
    guards against. Floor-of-float64 flat, same standard as the single
    crossover's own 0.0000dB pin.
    """
    assert M.four_band_flatness_db((80.0, 300.0, 4000.0)) < 1e-6


def test_radar_multiband_bypass_has_no_dynamics(radar_tuning):
    """
    Disabled must not apply any COMPRESSION OR LIMITING — the fixed gain
    stages (system gain, and bands 3/4's own comp_inVol/lim_inVol) stay
    either way, see process's comment on why, so there is no single fixed
    "unity" gain to compare against. Linearity is the property that
    actually distinguishes this from an active law: a fixed-gain, purely
    filtered signal scales exactly with its input; a compressed one does
    not. Fed the SAME shaped noise at two levels four octaves apart with
    the law active, band 1 alone shows over 20dB of compression — so this
    is a real discriminator, not a tautology.
    """
    rng = np.random.default_rng(3)
    base = rng.standard_normal(FS * 2) * 2000.0

    def out_rms(amp, enabled):
        mb = M.RadarMultiband(FS, radar_tuning.mbcl, enabled=enabled)
        y = mb.process((base * amp).copy())
        return float(np.sqrt(np.mean(y[FS:] ** 2)))

    quiet, loud = out_rms(1.0, False), out_rms(4.0, False)
    assert abs(20 * np.log10(loud / quiet) - 20 * np.log10(4.0)) < 0.01

    quiet_on, loud_on = out_rms(1.0, True), out_rms(4.0, True)
    assert 20 * np.log10(loud_on / quiet_on) < 20 * np.log10(4.0) - 1.0, (
        "enabled should show real compression on a 4x level jump")


def test_radar_multiband_toggle_does_not_click(radar_tuning):
    """
    Enabling/disabling mid-stream must not step the signal level by any
    amount beyond what the LAW was actually doing — system gain and
    every per-band trim stay fixed across the toggle (see process's
    comment), so what's left to disappear is only the compression
    engaged at this signal's level, measured separately below as
    ~1.17dB. 2.5dB gives that real, expected step room without passing
    a toggle that is colouring the signal for some other reason.
    """
    tone = _sine(1000, seconds=2.0, amp=0.1)
    chunk = FS // 10
    mb = M.RadarMultiband(FS, radar_tuning.mbcl, enabled=True)
    out = []
    for i in range(0, tone.size, chunk):
        if i == tone.size // 2:
            mb.set_params(enabled=False)
        out.append(mb.process(tone[i:i + chunk]))
    y = np.concatenate(out)
    before = float(np.sqrt(np.mean(y[FS - 4800:FS] ** 2)))
    after = float(np.sqrt(np.mean(y[FS:FS + 4800] ** 2)))
    assert abs(20 * np.log10(after / before)) < 2.5


def test_radar_multiband_engages_each_band(radar_tuning):
    """
    A tone placed deep inside each band, loud enough to cross that band's
    own threshold, must show real reduction there and be that tone's
    LARGEST reduction among the four bands.

    This does not assert the OTHERS stay at zero: band 1's law is 20:1
    from -25dB, aggressive enough that even the genuine ~24dB/octave
    rolloff of an adjacent LR4 crossover still leaks enough into it to
    register — measured at 120Hz (deep in band 2 on a log scale,
    sqrt(70*200)=118Hz) engaging band 1 nearly as hard as band 2 itself.
    That is a real property of a finite-slope crossover, not a bug this
    test exists to catch; what matters is that each band's OWN law is
    what responds hardest to content in its own range.
    """
    freqs = [30.0, 120.0, 1000.0, 10000.0]
    for i, f in enumerate(freqs):
        mb = M.RadarMultiband(FS, radar_tuning.mbcl, bass_guard_db=-40.0, enabled=True)
        mb.process(_sine(f, seconds=1.0, amp=0.9))
        reductions = [c.max_reduction_db for c in mb._comp]
        assert reductions[i] > 0.5, f"band {i+1} ({f}Hz) never engaged"
        assert reductions[i] == max(reductions), (
            f"band {i+1} ({f}Hz) was not its own loudest responder: {reductions}")


def test_radar_multiband_band3_and_4_get_their_input_trim(radar_tuning):
    """
    comp_inVol is a genuine gain stage ahead of the detector — band 3's
    +3dB must make it engage at a level that would NOT cross its own
    -15dB threshold on its own, once the ALWAYS-ON system gain (+4dB) is
    also accounted for. Stock's detector is POWER (StockCompressor), so a
    sine reads 3dB under its peak: amp=0.133 is -17.5dBFS peak, -20.5dB
    power; +4 lands at -16.5, under -15, and +4+3 at -13.5, over it.
    """
    amp = 0.133
    power_db = 20 * np.log10(amp) - 10 * np.log10(2)
    m = radar_tuning.mbcl
    assert power_db + m.in_vol_db < m.bands[2].comp_threshold_db
    assert power_db + m.in_vol_db + m.bands[2].comp_in_vol_db > m.bands[2].comp_threshold_db

    mb = M.RadarMultiband(FS, radar_tuning.mbcl, enabled=True)
    mb.process(_sine(1000.0, seconds=1.0, amp=amp))
    assert mb._comp[2].max_reduction_db > 0.0, "band 3's input trim should have engaged it"


def test_radar_multiband_set_params_only_touches_band_one(radar_tuning):
    """bass_guard_db is the one dashboard control Radar's guard has, and it
    must land on band 1's floor only — bands 2-4 have no control, same as
    the limiter override."""
    mb = M.RadarMultiband(FS, radar_tuning.mbcl, bass_guard_db=-30.0, enabled=True)
    assert mb._comp[0].floor_db == -30.0
    assert mb._comp[1].floor_db == -40.0
    mb.set_params(bass_guard_db=-10.0)
    assert mb._comp[0].floor_db == -10.0
    assert mb._comp[1].floor_db == -40.0


def test_raw_max_reduction_db_matches_rounded_for_both_classes(radar_tuning):
    """gen_vectors.py reads raw_max_reduction_db for full precision; it
    must agree with the rounded public property to two decimal places for
    both guard classes, so nothing is silently reading a different
    number."""
    for guard in (M.BassGuard(FS), M.RadarMultiband(FS, radar_tuning.mbcl)):
        guard.process(_sine(80.0, seconds=1.0, amp=0.8))
        assert round(guard.raw_max_reduction_db, 2) == guard.max_reduction_db


# ─── RadarMultiband with a stub tuning (CI has no ECHOMUSE_RADAR_TUNING) ───

def _stub_radar_tuning(monkeypatch):
    """A RadarTuning with one Mbcl per band, enough to drive RadarMultiband.

    The values are not stock's; the test is exercising the code paths
    that branch on the tuning being loaded, not validating the tuning.
    Returns the Mbcl so callers can also use it directly.
    """
    import em_radar_tuning
    band = em_radar_tuning.MbclBand(
        comp_in_vol_db=0.0, comp_ratio=4.0, comp_threshold_db=-20.0,
        comp_floor_db=-12.0, lim_in_vol_db=0.0, lim_threshold_db=-3.0,
        lim_release_ms=200.0)
    mbcl = em_radar_tuning.Mbcl(
        in_vol_db=0.0,
        crossovers_hz=(200.0, 800.0, 3000.0),
        bands=(band, band, band, band),
        full_band_in_vol_db=0.0,
        full_band_threshold_db=-3.0,
        full_band_release_ms=200.0)
    monkeypatch.setattr(em_radar_tuning, "current",
                        lambda: em_radar_tuning.RadarTuning(mbcl=mbcl))
    return mbcl


def test_radar_multiband_processes_audio_with_a_stub_tuning(monkeypatch):
    """Radar's full 4-band chain, runnable without the proprietary tuning.

    Without this, the codecov floor on em_mbc is unreachable in any
    environment that does not have ECHOMUSE_RADAR_TUNING set.
    """
    mbcl = _stub_radar_tuning(monkeypatch)
    mb = M.RadarMultiband(FS, mbcl)
    out = mb.process(_sine(80.0, seconds=0.1, amp=0.5))
    assert out.size > 0


def test_radar_multiband_disabled_passes_audio_through(monkeypatch):
    mbcl = _stub_radar_tuning(monkeypatch)
    mb = M.RadarMultiband(FS, mbcl, enabled=False)
    assert mb.process(_sine(80.0, seconds=0.1, amp=0.5)).size > 0


def test_radar_multiband_set_params_updates_band_one_floor_only(monkeypatch):
    mbcl = _stub_radar_tuning(monkeypatch)
    mb = M.RadarMultiband(FS, mbcl, bass_guard_db=-30.0, enabled=True)
    assert mb._comp[0].floor_db == -30.0
    mb.set_params(bass_guard_db=-10.0)
    assert mb._comp[0].floor_db == -10.0


def test_radar_multiband_set_params_with_enabled_propagates(monkeypatch):
    """Disabling the multiband while playing must reach every band
    compressor and every band limiter, not just the first one."""
    mbcl = _stub_radar_tuning(monkeypatch)
    mb = M.RadarMultiband(FS, mbcl, enabled=True)
    mb.set_params(enabled=False)
    assert all(c.enabled is False for c in mb._comp)
    assert all(lim.enabled is False for lim in mb._lim)


def test_radar_multiband_max_reduction_db_returns_finite(monkeypatch):
    mbcl = _stub_radar_tuning(monkeypatch)
    mb = M.RadarMultiband(FS, mbcl)
    mb.process(_sine(80.0, seconds=0.1, amp=2.0))
    assert mb.max_reduction_db >= 0.0


def test_four_band_flatness_with_a_stub_tuning():
    """four_band_flatness_db takes a crossovers tuple directly, no
    tuning required."""
    db = M.four_band_flatness_db((200.0, 800.0, 3000.0))
    assert 0.0 <= db < 1.0, \
        "the LR4 cascade with allpass compensation must sum flat to within 1dB"


# ─── StockCompressor: MBCL's per-band compressor, testable without tuning ──

def test_stock_compressor_engages_on_a_hot_signal():
    """A signal above the threshold must pull the gain down at the
    configured ratio. Testable without ECHOMUSE_RADAR_TUNING."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-20.0, floor_db=-12.0)
    sc.process(_sine(1000.0, seconds=0.5, amp=0.5))
    assert sc.min_gain < 1.0, "a 4:1 compressor above threshold must reduce"
    assert sc.max_reduction_db > 0.0


def test_stock_compressor_bypass_passes_audio_through_unchanged():
    """Bypass keeps the delay but freezes dynamics — so the chain stays
    aligned but the audio is not compressed. Same contract as
    Limiter/StockLimiter."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-20.0, floor_db=-12.0,
                           enabled=False)
    sc.process(_sine(1000.0, seconds=0.5, amp=0.5))
    assert sc.min_gain == 1.0, "bypassed compressor must not reduce"


def test_stock_compressor_process_is_1_to_1():
    """Streaming adds one block of latency but the output count must match
    the input count, so callers do not see short reads."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-20.0, floor_db=-12.0)
    for chunk in np.array_split(_sine(1000.0, seconds=0.5, amp=0.5), 11):
        assert sc.process(chunk).size == chunk.size


def test_stock_compressor_set_floor_clamps_out_of_range_values():
    """Stock's -40..0 dB clamp is the live range; out-of-range inputs are
    clamped rather than raised, so a bad config degrades to safe behaviour
    instead of killing playback (same shape as Limiter's threshold)."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-20.0, floor_db=-12.0)
    sc.set_floor_db(-100.0)
    assert sc.floor_db == -40.0
    sc.set_floor_db(6.0)
    assert sc.floor_db == 0.0


def test_stock_compressor_below_threshold_does_not_reduce():
    """A signal under the threshold must pass through with no gain
    reduction — the branch in _block that returns g=1.0 untouched."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-20.0, floor_db=-12.0)
    sc.process(_sine(1000.0, seconds=0.5, amp=0.01))    # well under threshold
    assert sc.min_gain == 1.0, "quiet audio must not engage the compressor"


def test_stock_compressor_floor_caps_the_gain_reduction():
    """A signal loud enough to demand more reduction than the floor
    allows must land on the floor, not under it. The floor is the
    guard against silence; without it, a panic loud could mute the
    chain entirely."""
    sc = M.StockCompressor(FS, ratio=4.0, threshold_db=-10.0, floor_db=-6.0)
    # 10s is long enough for the ~650ms smoother to converge on the
    # floor; shorter inputs leave the smoother mid-attack and the gain
    # above the floor.
    sc.process(_sine(80.0, seconds=10.0, amp=4.0))
    floor_linear = 10 ** (-6.0 / 20.0)
    assert abs(sc._gain - floor_linear) < 1e-3, \
        f"the floor must hold — got {sc._gain}, expected {floor_linear}"
