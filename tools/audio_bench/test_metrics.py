"""The bench's own arithmetic. The wake scorer is a stand-in: these check the
rules around it, not openWakeWord."""

import numpy as np

import metrics as M


def _raw(tmp_path, samples):
    """Write [9, n] int samples (24-bit range) as capture_mics does."""
    x = np.asarray(samples, dtype=np.int32).T          # [n, 9]
    u = x & 0xFFFFFF
    b = np.stack([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF], axis=-1).astype(np.uint8)
    p = tmp_path / "c.raw"
    b.tofile(p)
    return p


def test_raw_capture_round_trips_sign_and_scale(tmp_path):
    s = np.zeros((9, 4), dtype=np.int32)
    s[0] = [0, 1, -1, 8388607]
    s[6] = [-8388608, 4194304, -4194304, 0]
    x = M.load_raw(_raw(tmp_path, s))
    assert x.shape == (9, 4)
    assert np.allclose(x[0], [0, 1 / 8388608, -1 / 8388608, 8388607 / 8388608])
    assert np.allclose(x[6], [-1.0, 0.5, -0.5, 0])


def test_a_short_trailing_frame_is_dropped(tmp_path):
    p = _raw(tmp_path, np.ones((9, 3), dtype=np.int32))
    with open(p, "ab") as f:
        f.write(b"\x01\x02")
    assert M.load_raw(p).shape == (9, 3)


def test_levels_report_clipping_and_ignore_a_single_click():
    x = np.full((9, M.RATE), 1e-4)
    x[6, 100] = 1.0
    lv = M.mic_levels(x)
    assert lv[6]["clipped"] == 1 and lv[0]["clipped"] == 0
    assert abs(lv[0]["floor_db"] + 80) < 0.1
    assert lv[6]["floor_db"] < -79          # one click does not move the floor


def test_full_bar_needs_one_frame_and_the_refractory_holds():
    s = np.zeros(100)
    s[[10, 12, 40]] = 0.9                    # 12 is 160 ms after 10
    hits = M.detect(s, np.zeros(100, bool))
    assert [round(t, 2) for t, _, _ in hits] == [0.8, 3.2]


def test_low_bar_over_playback_needs_two_frames_running():
    low = np.ones(100, bool)
    s = np.zeros(100)
    s[10] = 0.3                              # alone: no
    s[[50, 51]] = 0.3                        # two running: yes, on the second
    hits = M.detect(s, low)
    assert [(round(t, 2), lb) for t, _, lb in hits] == [(4.08, True)]
    assert M.detect(s, np.zeros(100, bool)) == []      # under the full bar


def test_the_low_bar_outlasts_playback_by_the_hold():
    ref = np.zeros(M.CHUNK * 60, np.int16)
    ref[M.CHUNK * 5:M.CHUNK * 6] = 100
    active, low = M.playing_mask(ref, 60)
    assert active.sum() == 1
    assert low[5] and low[5 + 24] and not low[5 + 26] and not low[4]


def test_wake_is_scored_at_every_phase():
    seen = []

    def score(pcm):
        seen.append(len(pcm))
        s = np.zeros(len(pcm) // M.CHUNK)
        s[3] = 0.9 if len(seen) % 2 else 0.1          # fires on alternate phases
        return s

    pcm = np.zeros(M.CHUNK * 20, np.int16)
    w = M.wake(pcm, pcm, score)
    assert len(seen) == M.PHASES and len(set(seen)) == M.PHASES
    assert w["detections"] == 0.5 and w["detections_min"] == 0 and w["detections_max"] == 1


def test_echo_removed_counts_only_seconds_with_playback():
    n = M.RATE * 4
    rng = np.random.default_rng(0)
    mic = (rng.standard_normal(n) * 1000).astype(np.int16)
    out = mic.copy()
    out[M.RATE:2 * M.RATE] //= 10            # 20 dB off in second 1
    ref = np.zeros(n, np.int16)
    ref[M.RATE:2 * M.RATE] = 500
    e = M.echo_removed(mic, out, ref)
    assert e["seconds"] == 1 and abs(e["median_db"] - 20) < 0.5
    assert M.echo_removed(mic, out, np.zeros(n, np.int16)) is None


def _res(recall=1.0, echo=20.0, clipped=0):
    return {"wake": {"recall": recall, "detections": 8 * recall},
            "echo": {"median_db": echo, "first_second_db": echo},
            "levels": {"centre_floor_db": -80.0, "clipped": clipped}}


def _verdicts(base, new):
    return {(r["capture"], r["measure"]): r["verdict"] for r in M.compare(base, new)}


def test_compare_names_what_got_worse_and_what_got_better():
    v = _verdicts({"a": _res()}, {"a": _res(recall=0.8, echo=25.0)})
    assert v[("a", "wake.recall")] == "worse"
    assert v[("a", "echo.median_db")] == "better"
    assert v[("a", "levels.centre_floor_db")] == "same"


def test_compare_holds_within_tolerance():
    v = _verdicts({"a": _res(echo=20.0)}, {"a": _res(echo=19.2)})
    assert set(v.values()) == {"same"}


def test_any_new_clipping_is_worse():
    assert _verdicts({"a": _res()}, {"a": _res(clipped=1)})[("a", "levels.clipped")] == "worse"


def test_a_capture_or_measure_in_one_run_only_is_missing_not_a_pass():
    assert _verdicts({"a": _res(), "b": _res()}, {"a": _res()})[("b", "*")] == "missing"
    new = _res()
    new["echo"] = None
    assert _verdicts({"a": _res()}, {"a": new})[("a", "echo.median_db")] == "missing"


def test_a_floor_that_moves_either_way_is_flagged():
    quieter = _res()
    quieter["levels"]["centre_floor_db"] = -86.0
    assert _verdicts({"a": _res()}, {"a": quieter})[("a", "levels.centre_floor_db")] == "worse"


PROBE = """
== phase 2: cost at the real 12.5Hz duty cycle, 60s ==
  frames     750 over 60.0s (2 overran their 80ms slot)
  latency    p50 28.9ms  p95 31.2ms  max 84.0ms  (budget 80ms)
  CPU        38.0% of one core  (22.80s of CPU in 60.0s wall)
  RSS        41234 KB peak
"""


def test_a_device_probe_report_is_read():
    c = M.parse_probe(PROBE)
    assert c == {"pct_of_core": 38.0, "p50_ms": 28.9, "p95_ms": 31.2, "max_ms": 84.0,
                 "frames": 750, "seconds": 60.0, "late_frames": 2, "rss_kb": 41234}


def test_a_report_with_no_cpu_figure_is_refused():
    import pytest
    with pytest.raises(ValueError):
        M.parse_probe("VERDICT: FAIL\n")


def test_cpu_cost_is_compared_like_any_other_measure():
    base = {"cpu:wake": {"cpu": {"pct_of_core": 38.0, "p95_ms": 31.0, "late_frames": 0}}}
    v = _verdicts(base, {"cpu:wake": {"cpu": {"pct_of_core": 12.0, "p95_ms": 9.0, "late_frames": 0}}})
    assert v[("cpu:wake", "cpu.pct_of_core")] == "better" and v[("cpu:wake", "cpu.p95_ms")] == "better"
    v = _verdicts(base, {"cpu:wake": {"cpu": {"pct_of_core": 41.0, "p95_ms": 31.0, "late_frames": 1}}})
    assert v[("cpu:wake", "cpu.pct_of_core")] == "worse" and v[("cpu:wake", "cpu.late_frames")] == "worse"
    assert _verdicts(base, {})[("cpu:wake", "*")] == "missing"
