"""
The output chain has to be able to report what it is set to and what it did.

Both halves are needed and they answer different questions. Settings say
whether a config change reached the audio path; max reduction says whether the
law ever engaged. A guard that is enabled and reduced nothing is being fed
content with no bass — a different fault from one that never got the setting,
and from a listening seat the two are identical.

That matters because loudness is not a usable cue here: the guard is worth
~7.7dB of overall level at a modest EQ and ~0dB under a heavy boost, since the
limiter gives back what the guard takes. Judging by ear at the wrong operating
point reads as "this control does nothing" for a chain that is working
perfectly.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
import em_eq, em_mbc, em_limiter

FS = 48000


def _guard(enabled=True, depth=-30.0):
    return em_mbc.BassGuard(FS, bass_guard_db=depth, enabled=enabled)


def _limiter(enabled=True, thr=-1.0, rel=150.0):
    return em_limiter.Limiter(FS, threshold_db=thr, release_ms=rel,
                              enabled=enabled)


def _bassy(seconds=1.0, peak_db=-9.0):
    t = np.arange(int(FS * seconds)) / FS
    x = np.sin(2 * np.pi * 48 * t) + 0.3 * np.sin(2 * np.pi * 1000 * t)
    x /= np.abs(x).max()
    return (x * 10 ** (peak_db / 20) * 32767).astype(np.int16).tobytes()


def test_settings_are_reported_for_both_disabled_shapes():
    """
    A stage is disabled two ways and both occur: None from for_stream() on the
    one-shot path, and an instance with enabled=False on the streaming path,
    which keeps its filter state so a mid-track toggle cannot click.
    """
    assert "guard=off" in em_eq.describe_chain([0.0] * 8, False, None, None)
    assert "limiter=off" in em_eq.describe_chain([0.0] * 8, False, None, None)

    line = em_eq.describe_chain([0.0] * 8, False,
                                _limiter(enabled=False), _guard(enabled=False))
    assert "guard=off" in line and "limiter=off" in line


def test_settings_name_the_values_not_just_on():
    """
    "on" would not have caught the bug this exists for. The depth and the
    ceiling are what a saved change moves, so the line has to carry them.
    """
    line = em_eq.describe_chain([0.0] * 8, False,
                                _limiter(thr=-6.0, rel=400.0), _guard(depth=-25.0))
    assert "guard=-25dB" in line
    assert "limiter=-6dB/400ms" in line


def test_flat_eq_says_flat_and_a_shaped_one_lists_bands():
    assert "eq=flat" in em_eq.describe_chain([0.0] * 8, False)
    line = em_eq.describe_chain([3.0] + [0.0] * 7, True)
    assert "eq=+3/0/0/0/0/0/0/0" in line
    assert "speech_boost=on" in line


def test_activity_separates_off_from_on_but_idle():
    """
    The distinction the whole module exists for: `n/a` is a stage that was
    bypassed, `0.00dB` is one that ran and never engaged.
    """
    off = em_eq.describe_activity(_limiter(enabled=False), _guard(enabled=False))
    assert "guard_reduction=n/a" in off

    idle_guard = _guard()
    em_eq.StreamingEQ(FS, [0.0] * 8, False, guard=idle_guard).process(
        (np.zeros(4096, dtype=np.int16)).tobytes()
    )
    assert "guard_reduction=0.00dB" in em_eq.describe_activity(None, idle_guard)


def test_activity_reports_real_work_on_bass():
    """An enabled guard fed real bass must report a non-zero reduction."""
    guard = _guard()
    eq = em_eq.StreamingEQ(FS, [0.0] * 8, False, limiter=_limiter(), guard=guard)
    pcm = _bassy()
    for i in range(0, len(pcm), 4096):
        eq.process(pcm[i:i + 4096])
    assert guard.max_reduction_db > 10.0
    assert "guard_reduction=n/a" not in em_eq.describe_activity(eq.limiter, eq.guard)


def test_a_live_toggle_is_visible_as_a_settings_change():
    """
    update() returning True is what the music feed logs on, so a dashboard
    change has to make it return True — and a no-op must not, or the log
    fills at ~23 lines a second.
    """
    eq = em_eq.StreamingEQ(FS, [0.0] * 8, False,
                           limiter=_limiter(), guard=_guard())
    kw = dict(bands=[0.0] * 8, loudness=False, limiter_enabled=True,
              limiter_threshold=-1.0, limiter_release=150.0,
              guard_enabled=True, guard_db=-30.0)
    assert eq.update(**kw) is True          # first call always lands
    assert eq.update(**kw) is False         # steady state is silent
    assert eq.update(**{**kw, "guard_enabled": False}) is True
    assert "guard=off" in em_eq.describe_chain([0.0] * 8, False,
                                               eq.limiter, eq.guard)


def test_limiter_reports_the_release_it_was_given():
    """
    The limiter converts release to a per-sample slew and used to keep no
    copy of the original, so the setting could not be reported at all.
    """
    lim = _limiter(rel=400.0)
    assert lim.release_ms == 400.0
    lim.set_params(release_ms=250.0)
    assert lim.release_ms == 250.0
    assert "limiter=-1dB/250ms" in em_eq.describe_chain([0.0] * 8, False, lim)
