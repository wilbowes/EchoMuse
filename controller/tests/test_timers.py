"""Timer state and alarm audio (em_timers).

HA owns the countdown; the satellite only tracks live timers and decides when
to ring. These pin the ring-transition reducer (which is what the async
orchestrator in em_controller keys off), the spoken-dismissal matcher, and the
duck applied to the alert while someone speaks over it.
"""
import struct
import pytest

import em_timers as t


# ── TimerRegistry ────────────────────────────────────────────────────────────

def test_finished_starts_the_ring():
    reg = t.TimerRegistry()
    assert reg.apply(t.TIMER_STARTED, "a") == t.RING_NONE
    assert reg.ringing is False
    assert reg.apply(t.TIMER_FINISHED, "a") == t.RING_START
    assert reg.ringing is True


def test_cancel_of_finished_stops_the_ring():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    # A spoken "stop" dismisses a finished timer as a CANCELLED event.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_STOP
    assert reg.ringing is False


def test_cancel_of_running_timer_never_rings():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_STARTED, "a")
    # Cancelling a timer that was still counting down must not ring anything.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_NONE
    assert reg.ringing is False


def test_updated_does_not_ring():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_STARTED, "a")
    assert reg.apply(t.TIMER_UPDATED, "a") == t.RING_NONE
    assert reg.ringing is False


def test_second_finished_while_ringing_is_no_transition():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    # Already ringing — a second finished timer does not restart the ring.
    assert reg.apply(t.TIMER_FINISHED, "b") == t.RING_NONE
    assert reg.ringing is True
    # ...and the ring only stops once BOTH finished timers are dismissed.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_NONE
    assert reg.ringing is True
    assert reg.apply(t.TIMER_CANCELLED, "b") == t.RING_STOP
    assert reg.ringing is False


def test_cancel_of_unknown_timer_is_harmless():
    # HA sends CANCELLED for a timer we cleared locally (button dismissal);
    # it must not error or spuriously stop.
    reg = t.TimerRegistry()
    assert reg.apply(t.TIMER_CANCELLED, "ghost") == t.RING_NONE
    assert reg.ringing is False


def test_unknown_event_type_is_ignored():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    assert reg.apply(999, "a") == t.RING_NONE
    assert reg.ringing is True


def test_clear_reports_whether_it_was_ringing():
    reg = t.TimerRegistry()
    assert reg.clear() is False
    reg.apply(t.TIMER_FINISHED, "a")
    assert reg.clear() is True
    assert reg.ringing is False
    assert reg.active_count() == 0


# ── Spoken dismissal ─────────────────────────────────────────────────────────
# HA discards a timer when it finishes, so a spoken "stop" over a ringing alarm
# reaches HA and is answered "there are no timers" — no CANCELLED is ever sent
# (measured 2026-08-13). The dismissal is therefore recognised from the
# transcript, and only ever while an alarm is actually ringing.

@pytest.mark.parametrize("text", [
    "stop",
    "Stop.",
    " Stop. ",
    "cancel the timer",
    "Cancel the timer.",
    "dismiss",
    "turn it off",
    "shut up",
    "that's enough",
    "ok ok",
    "I'm up",
])
def test_dismissal_phrases_are_recognised(text):
    assert t.is_dismissal(text) is True


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "what's the weather",
    "set a timer for five minutes",
    "how much time is left",
    "turn on the kitchen light",
    "play some jazz",
])
def test_non_dismissals_reach_ha(text):
    # A real command spoken over a ringing alarm must still go to HA — the
    # matcher is generous, not indiscriminate.
    assert t.is_dismissal(text) is False


def test_dismissal_matches_whole_words_only():
    # "stopwatch" and "offer" contain dismissal words as substrings; matching
    # on substrings would eat ordinary commands.
    assert t.is_dismissal("start the stopwatch") is False
    assert t.is_dismissal("what's on offer") is False


# ── attenuate() — ducking the alert ──────────────────────────────────────────
# The alert audio is the bundled Voice PE sound, decoded by em_controller
# (ffmpeg), so these work on synthetic PCM rather than the file: the duck is
# pure arithmetic on S16_LE and must not need an audio decoder to test.

def _pcm(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def _samples(pcm: bytes):
    assert len(pcm) % 2 == 0, "S16_LE must be an even number of bytes"
    return list(struct.unpack(f"<{len(pcm)//2}h", pcm))


def test_attenuate_scales_by_the_requested_gain():
    duck = _samples(t.attenuate(_pcm(30000, -30000, 1000), t.DUCK_DB))
    # −18dB ≈ 0.126×
    assert 3600 < duck[0] < 3900
    assert -3900 < duck[1] < -3600
    assert 100 < duck[2] < 140


def test_attenuate_preserves_length():
    # The ring swaps between full and ducked mid-alarm; a length change would
    # shift the cadence of the loop.
    full = _pcm(*([12000] * 500))
    assert len(t.attenuate(full, t.DUCK_DB)) == len(full)


def test_attenuate_never_boosts():
    # The alert is mastered near full scale, so a positive gain would clip.
    full = _pcm(30000, -30000)
    assert t.attenuate(full, 6.0) == full
    assert t.attenuate(full, 0.0) == full


def test_attenuated_alert_stays_audible():
    # Ducked, not muted: it must still read as ringing while the user speaks
    # over it, or the duck is indistinguishable from a dismissal.
    duck = _samples(t.attenuate(_pcm(*([28000] * 100)), t.DUCK_DB))
    assert max(abs(x) for x in duck) > 1500


def test_attenuate_handles_empty_and_odd_input():
    assert t.attenuate(b"", t.DUCK_DB) == b""
    # An odd trailing byte cannot be a whole S16 sample — dropped, not crashed.
    assert len(t.attenuate(b"\x00\x10\x7f", t.DUCK_DB)) == 2


def test_alert_sound_ships_with_its_licence():
    # CC BY 4.0 requires the attribution to travel with the audio, and there is
    # no synthesised fallback any more — a build without the file cannot ring,
    # so both the sound and its licence are load-bearing.
    import os
    assert os.path.exists(t.ALARM_SOUND_FILE), "the alert sound must ship"
    licence = os.path.join(os.path.dirname(t.ALARM_SOUND_FILE), "LICENSE.md")
    assert os.path.exists(licence), "bundled sound must ship with LICENSE.md"
    text = open(licence, encoding="utf-8").read()
    assert "CC BY" in text or "Creative Commons Attribution" in text
