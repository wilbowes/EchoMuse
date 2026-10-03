"""Timer state and alarm audio (em_timers).

HA owns the countdown; the satellite only tracks live timers and decides when
to ring. These pin the ring-transition reducer (which is what the async
orchestrator in em_controller keys off), the spoken-dismissal matcher, and the
duck applied to the alert while someone speaks over it.
"""
import math
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


# ── Countdown registry, clock-injected ───────────────────────────────────────
# The ring shows a live timer's remaining time, and it must keep doing that
# when HA's UPDATED events stop arriving. The deadline is converted from
# seconds_left at arrival and the injected clock counts the seconds from
# there. An UPDATED is a drift correction, not a heartbeat the display
# depends on.

class _Clock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


def _clocked():
    clock = _Clock()
    return t.TimerRegistry(clock=clock), clock


def test_registry_tracks_a_running_countdown():
    reg, clock = _clocked()
    reg.apply(t.TIMER_STARTED, "a", total_seconds=300, seconds_left=300)
    rem, tot = reg.running_countdown()
    assert (rem, tot) == (300.0, 300.0)
    clock.t += 60
    rem, tot = reg.running_countdown()
    assert tot == 300.0
    assert abs(rem - 240.0) < 1e-6


def test_updated_corrects_the_deadline_and_absence_does_not_break_it():
    reg, clock = _clocked()
    reg.apply(t.TIMER_STARTED, "a", total_seconds=300, seconds_left=300)
    clock.t += 100
    reg.apply(t.TIMER_UPDATED, "a", total_seconds=300, seconds_left=100)
    clock.t += 5
    rem, _tot = reg.running_countdown()
    assert abs(rem - 95.0) < 1e-6


def test_finished_timers_are_not_counted_down():
    # The alarm owns a finished timer; the countdown shows running ones only.
    reg, _ = _clocked()
    reg.apply(t.TIMER_STARTED, "a", total_seconds=60, seconds_left=60)
    reg.apply(t.TIMER_FINISHED, "a")
    assert reg.running_countdown() is None


def test_soonest_running_timer_wins_then_the_next_takes_over():
    reg, _ = _clocked()
    reg.apply(t.TIMER_STARTED, "long", total_seconds=600, seconds_left=600)
    reg.apply(t.TIMER_STARTED, "short", total_seconds=60, seconds_left=60)
    _rem, tot = reg.running_countdown()
    assert tot == 60
    reg.apply(t.TIMER_CANCELLED, "short")
    _rem, tot = reg.running_countdown()
    assert tot == 600


def test_unknown_length_or_expired_running_timer_is_none():
    # With total <= 0 nothing was sent with it, so no arc can be sized.
    # With remaining <= 0 FINISHED is on its way and there is nothing left
    # to count down.
    reg, clock = _clocked()
    reg.apply(t.TIMER_STARTED, "zero-total", total_seconds=0, seconds_left=0)
    assert reg.running_countdown() is None
    reg.apply(t.TIMER_STARTED, "b", total_seconds=5, seconds_left=5)
    clock.t += 5
    assert reg.running_countdown() is None


def test_clear_drops_finished_but_keeps_running():
    # Clearing is dismissal of the alarm. Cancelling it must not strand
    # every other live timer. With the registry now feeding the countdown
    # too, a clear-everything would black their arcs until HA next
    # mentioned them.
    reg, _ = _clocked()
    reg.apply(t.TIMER_STARTED, "a", total_seconds=300, seconds_left=300)
    reg.apply(t.TIMER_FINISHED, "b")
    assert reg.clear() is True
    assert reg.ringing is False
    rem, tot = reg.running_countdown()
    assert tot == 300.0 and abs(rem - 300.0) < 1e-6
    assert reg.active_count() == 1


def test_clear_of_a_quiet_registry_keeps_running_timers_too():
    reg, _ = _clocked()
    reg.apply(t.TIMER_STARTED, "a", total_seconds=60, seconds_left=60)
    assert reg.clear() is False
    assert reg.active_count() == 1


# ── Countdown geometry and eligibility ───────────────────────────────────────

def test_countdown_lit_edges():
    assert t.countdown_lit(300, 300) == 12            # full ring
    assert t.countdown_lit(299.9, 300) == 12          # ceil holds full
    assert t.countdown_lit(11 * 300 / 12, 300) == 11  # exactly on a boundary
    assert t.countdown_lit(0.01, 300) == 1            # a live timer is never off
    assert t.countdown_lit(0, 300) == 0               # nothing to show
    assert t.countdown_lit(-5, 300) == 0
    assert t.countdown_lit(400, 300) == 12            # drift above total clamps


def test_countdown_anim_shape():
    anim = t.countdown_anim(300, 300)
    assert anim["pattern"] == "solid"
    assert len(anim["colors"]) == t.COUNTDOWN_NUM_LEDS
    assert all(c == [255, 170, 0] for c in anim["colors"])
    assert "listening" not in anim  # no direction overlay, it is not listening
    assert anim["ttlSec"] >= math.ceil(300) + 15      # dead-man covers the run


def test_countdown_anim_ttls_are_dead_mans():
    # A controller that dies mid-countdown leaves the ring self-clearing a
    # quarter-minute after the countdown itself would have ended.
    assert t.countdown_anim(7, 7)["ttlSec"] >= 7 + 15
    assert t.countdown_anim(0.5, 300)["ttlSec"] >= 15


def test_countdown_anim_orientation_matches_the_volume_arc():
    # lit LEDs are ids 0..lit-1, the same orientation the device's volume
    # arc uses (device/internal/server/volume.go), so both arcs drain from
    # the same end of the ring.
    anim = t.countdown_anim(95, 120)  # ceil(12 * 95/120) = 10
    assert anim["colors"][:10] == [[255, 170, 0]] * 10
    assert anim["colors"][10:] == [[0, 0, 0]] * 2


def _walk_down(total: float, margin: float):
    """Simulate the stepper by sleeping delta + margin and reading the new
    LED count."""
    rem, lits = total, []
    while rem > 0:
        delta = t.countdown_step_delta(rem, total)
        assert delta > 0, "a non-positive delta would spin the stepper"
        rem -= delta + margin
        lits.append(t.countdown_lit(rem, total))
    return lits


def test_step_delta_lands_just_past_each_boundary_120s():
    # A 120 s timer loses one LED every 10 s; each wake lands past the next
    # boundary so the count drops by exactly one, all the way to expiry.
    assert _walk_down(120.0, 0.2) == list(range(11, -1, -1))


def test_step_delta_lands_just_past_each_boundary_7s():
    # A 7 s timer steps every 7/12 s. The margin is what clears the
    # boundary. Computed exactly onto it, floating point can ceil back to
    # the same LED count and the stepper repaints nothing forever.
    lits = _walk_down(7.0, 0.2)
    assert lits[0] == 11
    assert all(a >= b for a, b in zip(lits, lits[1:]))  # monotone down
    assert lits[-1] == 0


def test_step_delta_at_one_led_is_the_remaining_time():
    # With one LED left the next change is expiry, not a boundary.
    assert t.countdown_step_delta(2.5, 120) == 2.5


def test_countdown_should_paint_truth_table():
    ok = dict(capable=True, enabled=True, has_running=True,
              alarm_ringing=False, turn_active=False)
    assert t.countdown_should_paint(**ok) is True
    assert t.countdown_should_paint(**{**ok, "capable": False}) is False
    assert t.countdown_should_paint(**{**ok, "enabled": False}) is False
    assert t.countdown_should_paint(**{**ok, "has_running": False}) is False
    assert t.countdown_should_paint(**{**ok, "alarm_ringing": True}) is False
    assert t.countdown_should_paint(**{**ok, "turn_active": True}) is False


def test_countdown_may_clear_never_stomps_a_live_ring():
    assert t.countdown_may_clear(True, False, False, False) is True
    # A ring we never showed is not ours to black. Mute red, volume cyan
    # and link orange are device-sourced.
    assert t.countdown_may_clear(False, False, False, False) is False
    assert t.countdown_may_clear(True, True, False, False) is False   # repaints
    assert t.countdown_may_clear(True, False, True, False) is False   # alarm
    assert t.countdown_may_clear(True, False, False, True) is False   # turn


def test_countdown_num_leds_matches_the_ring_everywhere_else():
    # 12 is a fact about the hardware, stated in three languages now; the
    # Go count is what the device actually paints.
    import em_scenes
    assert t.COUNTDOWN_NUM_LEDS == em_scenes.NUM_LEDS
    from pathlib import Path
    go = (Path(__file__).resolve().parents[2]
          / "device" / "internal" / "server" / "volume.go").read_text()
    assert "numLEDs    = 12" in go or "numLEDs = 12" in go


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


# ── Dismissal-only: which utterances also lose HA's reply ────────────────────
# Stopping the ring and suppressing HA's spoken reply are DIFFERENT questions,
# and they rode on one match until 2026-08-21. A command that happens to
# contain a dismissal word ("turn off the kitchen light") should stop the ring
# AND still be answered — the light does turn off, so silence leaves the user
# unable to tell whether it worked.

@pytest.mark.parametrize("text", [
    "stop",
    "Stop.",
    "cancel the timer",
    "turn it off",
    "turn off the alarm",
    "stop the alarm please",
    "shut up",
    "that's enough",
    "ok ok",
    "I'm up",
    "okay okay, stop",
])
def test_pure_dismissals_suppress_the_reply(text):
    assert t.is_dismissal_only(text) is True


@pytest.mark.parametrize("text", [
    "turn off the kitchen light",
    "stop the music",
    "quiet the bedroom fan",
    "turn off the lights downstairs",
    "cancel my 7am alarm on the phone",
])
def test_commands_carrying_a_dismissal_keep_their_reply(text):
    # These stop the ring — the generous match is right about that — but HA
    # acts on them too, so its confirmation must survive.
    assert t.is_dismissal(text) is True
    assert t.is_dismissal_only(text) is False


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "what's the weather",
    "set a timer for five minutes",
    "turn on the kitchen light",
])
def test_non_dismissals_are_not_dismissal_only(text):
    # Nothing to suppress if nothing was dismissed.
    assert t.is_dismissal_only(text) is False


def test_dismissal_only_consumes_every_repetition():
    # " stop stop " must lose BOTH — a single str.replace pass leaves the
    # second one behind (the first eats the space between them) and the
    # leftover reads as an unrecognised word.
    assert t.is_dismissal_only("stop stop") is True
    assert t.is_dismissal_only("stop, stop, stop!") is True


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
