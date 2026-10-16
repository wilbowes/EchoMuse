"""em_radar_tuning reads a Radar's stock tuning the way the device does
(device/internal/outchain/radartuning.go). tests/radar_tuning is a made-up
tuning in that shape; none of its numbers are stock's."""

import os
import shutil

import pytest

import em_radar_tuning as T

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "radar_tuning")


def test_reads_what_afe_lists():
    t = T.load(FIXTURE)
    assert t.fir_bands == [[1.0, -0.25, 0.125, 0.0], [0.5, 0.25, 0.0, 0.0]]
    assert t.fir_bounds == [40.0, 100.0]
    assert t.peq == [("LOW_SHELF", 120.0, 0.7, 3.0), ("PEAK", 90.0, 1.2, 1.0)]
    assert t.trim_db == 1.5
    m = t.mbcl
    assert m.in_vol_db == 2.0 and m.crossovers_hz == (80.0, 300.0, 4000.0)
    assert m.bands[2] == T.MbclBand(2.0, 2.0, -12.0, -30.0, 1.0, -5.0, 50.0)
    assert (m.full_band_in_vol_db, m.full_band_threshold_db, m.full_band_release_ms) == (0.0, -2.0, 30.0)


def test_skips_a_stage_afe_does_not_list(tmp_path):
    for f in ("EQ_quiet.cfg", "EQ_loud.cfg", "PEQ.cfg", "MBCL.cfg"):
        shutil.copy(os.path.join(FIXTURE, f), tmp_path)
    (tmp_path / "AFE.cfg").write_text(
        '{"Path Definition": {"Playback": {"Algorithms": {"MBCL": "MBCL"}}},'
        ' "Algorithm Definition": {"MBCL": {"External Config": ["MBCL.cfg"]}}}')
    t = T.load(str(tmp_path))
    assert t.mbcl is not None and not t.fir_bands and not t.peq and t.trim_db == 0.0


def test_a_missing_listed_file_fails_the_load(tmp_path):
    for f in ("AFE.cfg", "EQ_quiet.cfg", "PEQ.cfg", "MBCL.cfg"):
        shutil.copy(os.path.join(FIXTURE, f), tmp_path)
    with pytest.raises(OSError):
        T.load(str(tmp_path))


def test_current_follows_the_environment(monkeypatch):
    monkeypatch.setenv(T.ENV, "")
    assert T.current() is None
    monkeypatch.setenv(T.ENV, FIXTURE)
    assert T.current().trim_db == 1.5
    monkeypatch.setenv(T.ENV, "/does/not/exist")
    assert T.current() is None


def test_chain_takes_the_tuning(monkeypatch):
    import em_eq
    import em_limiter
    import em_mbc
    monkeypatch.setenv(T.ENV, FIXTURE)
    assert isinstance(em_mbc.build_guard(48000, "radar"), em_mbc.RadarMultiband)
    lim = em_limiter.build_limiter(48000, "radar", threshold_db=-1.0, release_ms=150.0)
    assert isinstance(lim, em_limiter.StockLimiter)
    assert em_limiter.params_for("radar", -1.0, 150.0) == (-2.0, 30.0)
    assert em_limiter.params_for("biscuit", -1.0, 150.0) == (-1.0, 150.0)
    eq = em_eq.StreamingEQ(48000, stock_curve=True, volume_gain=1.0)
    assert eq._fir is not None and eq._fir_bounds == [40.0, 100.0]
    assert eq._peq_sos.shape[0] == 2
