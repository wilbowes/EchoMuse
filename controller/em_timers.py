"""
em_timers.py — voice-assistant timer state and alarm audio (pure logic)
========================================================================

Home Assistant owns the timer: "set a timer for one minute" is resolved by
HA's Assist intent, HA counts it down, and HA pushes state to the satellite
as VoiceAssistantTimerEventResponse events (STARTED / UPDATED / CANCELLED /
FINISHED — see esphome/vendor/api_pb2.py). The satellite's only job is to
advertise the TIMERS feature so those intents route to it, track which timers
are live, and RING when one finishes.

This module is the pure half of that, kept out of em_esphome for the same
reason as em_button / em_shadow / em_turnclock — the controller test suite
imports it without pulling in aiohttp/openwakeword:

  * TimerRegistry reduces the event stream into "is a finished timer waiting
    to be dismissed?" and "what is the soonest running timer's remaining
    time?" and reports the ring transitions (start / stop) that the async
    orchestrator in em_controller acts on. Remaining time is kept as a
    deadline converted from HA's seconds_left at arrival, so the display
    does not depend on HA's event cadence (see running_countdown).
  * The countdown_* helpers turn a (remaining, total) pair into the shrinking
    amber arc the ring shows while a timer runs, and decide when the
    countdown may own the ring at all.
  * attenuate() ducks the alert while someone speaks over it. The alert audio
    itself is the bundled Home Assistant Voice PE sound (ALARM_SOUND_FILE),
    decoded by em_controller — it is already 48kHz mono, the format the device
    speaker plane expects, so the alert needs no firmware support.

Dismissal of a RINGING alarm is always local, because HA discards a timer the
moment it finishes and can no longer cancel it: a dot-button tap, or a spoken
dismissal recognised from the transcript (is_dismissal, below). A CANCELLED
event still dismisses too — HA sends one when a timer is cancelled while it is
still counting down — so the registry handles both. Either way the registry is
cleared and the ring stops; a safety cap bounds a ring nobody ever answers.
"""

from __future__ import annotations

import math
import os
import time

import numpy as np

# ── ESPHome VoiceAssistantTimerEvent values ─────────────────────────────────
# Mirrors the api_pb2.VoiceAssistantTimerEvent enum. Reproduced as plain ints
# so this module has no protobuf dependency and stays trivially importable by
# the test environment (numpy/scipy only). Cross-check against
# api_pb2.VoiceAssistantTimerEvent if the vendored protobufs are regenerated.
TIMER_STARTED   = 0
TIMER_UPDATED   = 1
TIMER_CANCELLED = 2
TIMER_FINISHED  = 3

# Ring transitions returned by TimerRegistry.apply().
RING_START = "start"
RING_STOP  = "stop"
RING_NONE  = "none"

# ── Alarm audio ─────────────────────────────────────────────────────────────
# The alert is the Home Assistant Voice PE timer sound (CC BY 4.0 — see
# sounds/LICENSE.md), so a finished timer sounds like the one people already
# know from HA's own hardware. It ships as 48kHz mono FLAC, which is already
# the wire format, so decoding is a straight ffmpeg call with no resample.
# em_controller decodes and caches it; this module stays free of subprocesses
# so the test suite can import it.
ALARM_SOUND_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sounds", "timer_finished.flac"
)

# The sound IS the alert — there is no synthesised fallback. A missing or
# undecodable file is a broken build, and a build that rings with a different
# sound than every other one is harder to support than a build that says so.

# Safety cap: how long a finished timer keeps ringing if nobody dismisses it.
# HA leaves a finished timer ringing indefinitely; the room should not. 15
# minutes is Voice PE's (`delay: 15min` then disable_repeat): a timer rings for
# as long as it needs to, and one that stops early is one that can be missed
# (Wil, 2026-09-29, declining #667's shorter setting). It was 120s.
MAX_RING_S  = 900.0
# Silence the orchestrator inserts between looped bursts (seconds).
BURST_GAP_S = 0.6

# How far the chime is attenuated once a wake word has been heard over it, so
# the command that follows ("dismiss") reaches STT over the alarm rather than
# under it. Matches the playback duckDb default — same job, same taste call.
DUCK_DB = -18.0
# How long the duck holds with no turn taking over. A wake word that starts no
# turn (a false accept on the chime itself) must not leave the alarm quiet for
# the rest of its ring — the ring is a safety feature.
DUCK_HOLD_S = 12.0

# LED cue while ringing — a distinct pulse, not one of the reserved status
# colours (red=mute, orange=link, cyan=volume). Amber pulse reads as an alert
# without a legend; ttlSec is a dead-man so a controller stall self-clears the
# ring (same contract as em_scenes).
TIMER_ANIM = {
    "pattern":  "pulse",
    "colors":   [[255, 170, 0]],
    "periodMs": 500,
    "ttlSec":   5,
}


class TimerRegistry:
    """
    Per-device timer state, driven by VoiceAssistantTimerEventResponse events.

    Not thread-safe; drive it from a single asyncio task (the satellite's
    message handler). A timer is keyed by HA's timer_id and is either
    "running" or "finished"; the registry is "ringing" while any finished
    timer has not been dismissed. A running timer also carries its total and
    a monotonic deadline, so the ring can show a countdown without ever
    asking HA again (see running_countdown).
    """

    def __init__(self, clock=time.monotonic) -> None:
        # timer_id -> (state, total_seconds, deadline)
        #
        # The deadline is converted from HA's seconds_left the moment an
        # event arrives, against the injected clock. HA's UPDATED cadence is
        # then only a drift correction. If the events stop arriving the arc
        # keeps moving on the injected clock, and an UPDATED that does
        # arrive rewrites the deadline. Constructor-injected like
        # em_turnclock so the tests can drive time.
        self._timers: dict[str, tuple[str, float, float]] = {}
        self._clock = clock

    @property
    def ringing(self) -> bool:
        return any(state == "finished" for state, _t, _d in self._timers.values())

    def apply(self, event_type: int, timer_id: str = "",
             total_seconds: float = 0, seconds_left: float = 0) -> str:
        """
        Fold one event into the registry.

        Returns RING_START when this event begins a ring (first finished timer
        after a quiet registry), RING_STOP when it ends one (the last finished
        timer was dismissed/cancelled), or RING_NONE otherwise.

        total_seconds/seconds_left default to 0 so existing callers that
        only care about the ring transition keep working; the countdown
        simply sees those timers as unknown-length and skips them.
        """
        was = self.ringing

        if event_type == TIMER_FINISHED:
            # No deadline kept. The alarm owns the ring from here, so the
            # countdown never sees a finished timer.
            self._timers[timer_id] = ("finished", 0.0, 0.0)
        elif event_type == TIMER_CANCELLED:
            # A CANCELLED for a still-running timer must NOT affect the ring,
            # and a CANCELLED for a finished one is exactly how a spoken "stop"
            # dismisses it — pop covers both.
            self._timers.pop(timer_id, None)
        elif event_type in (TIMER_STARTED, TIMER_UPDATED):
            self._timers[timer_id] = ("running", float(total_seconds),
                                      self._clock() + float(seconds_left))
        # Unknown event types are ignored (degrade to old behaviour).

        now = self.ringing
        if now and not was:
            return RING_START
        if was and not now:
            return RING_STOP
        return RING_NONE

    def clear(self) -> bool:
        """
        Drop FINISHED timers (local dismissal), keeping running ones.
        Returns whether it was ringing, so a caller can tell "I stopped an
        alarm" from "nothing was ringing".

        It used to clear everything, which was harmless while the registry
        only fed the ring decision. With a countdown it is not. A dismissal
        would strand every other live timer (no deadline held, nothing to
        paint) until HA next mentioned them, and HA only mentions a timer
        at its own events. Both clear_timers call sites mean exactly "drop
        the finished ones".
        """
        was = self.ringing
        for timer_id in [tid for tid, (state, _t, _d) in self._timers.items()
                         if state == "finished"]:
            self._timers.pop(timer_id)
        return was

    def running_countdown(self, now: float | None = None) -> "tuple[float, float] | None":
        """
        (remaining, total) for the soonest-finishing RUNNING timer, or None.

        None when nothing is running, when total <= 0 (its length is unknown,
        so no arc can be sized), or when remaining <= 0 (FINISHED is on its
        way and a timer that has run out has nothing left to count down;
        holding the last LED until the event lands is the caller's job).
        """
        if now is None:
            now = self._clock()
        best: "tuple[float, float, float] | None" = None  # (deadline, remaining, total)
        for state, total, deadline in self._timers.values():
            if state != "running" or total <= 0:
                continue
            remaining = deadline - now
            if remaining <= 0:
                continue
            if best is None or deadline < best[0]:
                best = (deadline, remaining, total)
        return (best[1], best[2]) if best else None

    def active_count(self) -> int:
        return len(self._timers)


# ── Countdown ring ────────────────────────────────────────────────────────────
# While an HA timer runs, the ring shows a shrinking amber arc on the Echo the
# timer was set from. Controller-only. The arc is an ordinary `solid` led_anim
# spec with per-LED colours, which every led_anim device (v2.9+) already
# renders (paletteFrame, device/internal/server/animator.go). The countdown is
# a static arc where the alarm is a pulse, so no new pattern was needed and
# the reserved status colours (red=mute, orange=link, cyan=volume) are
# untouched; the amber is the established timer amber from TIMER_ANIM above.

COUNTDOWN_NUM_LEDS = 12  # pinned against em_scenes.NUM_LEDS and the Go count

# (255, 170, 0), the TIMER_ANIM amber, reused from the pulse rather than
# restated as a new colour.
_COUNTDOWN_COLOR = [255, 170, 0]


def countdown_lit(remaining: float, total: float,
                 num_leds: int = COUNTDOWN_NUM_LEDS) -> int:
    """
    How many LEDs the arc lights. The rule is ceil(num_leds * remaining /
    total), floor 1.

    A live timer does not read as "off". The last LED holds until FINISHED
    takes the ring. 0 only answers "nothing to show" (remaining or total
    non-positive), which running_countdown already excludes before this is
    called. Clamped at the top for the drift case where a clock correction
    leaves remaining above total.
    """
    if total <= 0 or remaining <= 0:
        return 0
    return max(1, min(num_leds, math.ceil(num_leds * remaining / total)))


def countdown_anim(remaining: float, total: float) -> dict:
    """
    The arc as a led_anim spec. It lights LEDs 0..lit-1 amber with the rest
    black, in the same orientation as the volume arc
    (device/internal/server/volume.go). Not `listening: true`; the direction
    overlay belongs to listening.

    ttlSec = ceil(remaining) + 15, the dead-man every other spec carries. A
    controller that dies mid-countdown leaves the ring self-clearing a
    quarter-minute after the countdown itself would have ended.
    """
    lit = countdown_lit(remaining, total)
    colors = [list(_COUNTDOWN_COLOR)] * lit + [[0, 0, 0]] * (COUNTDOWN_NUM_LEDS - lit)
    return {
        "pattern": "solid",
        "colors":  colors,
        "ttlSec":  max(15, math.ceil(remaining) + 15),
    }


def countdown_step_delta(remaining: float, total: float,
                         num_leds: int = COUNTDOWN_NUM_LEDS) -> float:
    """
    Seconds until `lit` next decrements.

    The repaint sleeper waits on this rather than polling. The LED count
    only changes at multiples of total/num_leds of remaining, so waking
    just past each boundary (the caller adds its own margin) keeps the arc
    honest with one paint per visible change. Once one LED is left the next
    change is expiry, so the answer is the remaining time itself.
    """
    lit = countdown_lit(remaining, total, num_leds)
    if lit <= 1:
        return max(0.0, remaining)
    return remaining - (lit - 1) * total / num_leds


def countdown_should_paint(capable: bool, enabled: bool, has_running: bool,
                           alarm_ringing: bool, turn_active: bool) -> bool:
    """
    Whether the countdown may own the ring right now. Pure, like em_button.decide.

    Off for firmware that cannot render a led_anim arc, for a device with the
    kill switch off (timerRing), with nothing running, while an alarm owns
    the ring (alarm = pulse, and the pulse outranks the arc), and while a
    voice turn owns it (listening/spin/meter replace it by the animator's
    generation counter, and painting now would stamp over the ring
    mid-reply).
    """
    return bool(capable and enabled and has_running
                and not alarm_ringing and not turn_active)


def countdown_may_clear(was_shown: bool, paint: bool,
                        alarm_ringing: bool, turn_active: bool) -> bool:
    """
    Whether the ring may be sent `off`.

    Only when we own it (we are the one that showed it) and nobody else is
    painting. Never stomp a live listening/meter ring or an alarm pulse.
    When `paint` is true the countdown repaints rather than clears, so this
    is the else-branch's guard, and it fails safe. An owner we forgot about
    keeps its ring, and a countdown arc we never showed has nothing to
    clear.
    """
    return bool(was_shown and not paint and not alarm_ringing and not turn_active)


# ── Spoken dismissal ────────────────────────────────────────────────────────
# HA DISCARDS a timer the moment it finishes: cancelling works while one is
# counting down, but once it fires HA's timer manager no longer knows about it
# and answers "there are no timers" to a spoken stop (measured 2026-08-13 —
# 'Stop.' and 'Cancel the timer.' both reached HA correctly over the ringing
# chime and neither produced a CANCELLED event). That is HA's design, not a
# fault: it hands ringing to the satellite and expects the satellite to own
# dismissal, which is how HA's own Voice PE behaves.
#
# So the dismissal is recognised HERE, from the transcript HA already sends us,
# and applied locally. The registry's CANCELLED-while-finished path stays — an
# HA that does send one (or a cancel of a still-running timer) is still handled
# — this is the case HA structurally cannot answer.
_DISMISS_WORDS = (
    "stop", "cancel", "dismiss", "silence", "quiet", "enough",
    "shut up", "turn it off", "turn off", "shut it off", "off",
    "okay okay", "ok ok", "alright already", "im up",
)

# Words that can pad a dismissal without making it a command: articles, the
# nouns a person uses for the thing that is ringing, and ordinary politeness.
# Used ONLY by is_dismissal_only — the generous matcher above does not care
# what else is in the sentence.
_FILLER_WORDS = frozenset({
    "a", "the", "this", "that", "thats", "it", "its", "please", "now",
    "just", "already", "im",
    "alarm", "alarms", "timer", "timers", "ringing", "sound", "noise", "thing",
    "ok", "okay", "yeah", "yes", "alright", "thanks", "thank", "you", "hey",
})

# Longest phrases first, so "turn off" is consumed before the bare "off" can
# leave "turn" behind as an unexplained word.
_DISMISS_PHRASES = tuple(sorted(
    _DISMISS_WORDS, key=lambda w: (-len(w.split()), -len(w))
))


def _normalise(text: str) -> str:
    """
    Space-padded, punctuation-free lowercase, so a match is whole-word.

    Apostrophes are DROPPED rather than turned into spaces (so "I'm" is one
    word, not "i m"), in both the straight and curly forms since STT emits
    either.
    """
    if not text:
        return ""
    lowered = text.lower().replace("'", "").replace("’", "")
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in lowered)
    joined  = " ".join(cleaned.split())
    return f" {joined} " if joined else ""


def is_dismissal(text: str) -> bool:
    """
    Whether an utterance spoken OVER a ringing alarm means "make it stop".

    Deliberately generous, because it is only ever consulted while an alarm is
    actually ringing — in that context almost anything a person says is about
    the alarm, and the cost of a miss (the alarm keeps going and HA answers
    "there are no timers") is worse than the cost of a false positive (an
    alarm stops that was going to be stopped seconds later anyway).

    It is NOT generous enough to eat a real command, though: "set a timer for
    five minutes" while one rings must still reach HA, which is why this
    matches words rather than simply treating every utterance as a dismissal.

    Use this to STOP THE RING and nothing else. Suppressing HA's spoken reply
    needs is_dismissal_only — see there for why the two cannot be the same
    question.
    """
    padded = _normalise(text)
    if not padded:
        return False
    return any(f" {w} " in padded for w in _DISMISS_PHRASES)


def is_dismissal_only(text: str) -> bool:
    """
    Whether the utterance is a dismissal AND NOTHING ELSE.

    The generosity above is right for stopping the ring and wrong for
    swallowing HA's reply, and those rode on one match until 2026-08-21.
    "Turn off the kitchen light" spoken over an alarm is a dismissal by the
    rule above — reasonably, since the alarm should stop — but it is also a
    real command that HA answers. Suppressing that answer left the light
    switched off and the user with silence, unable to tell whether anything
    had happened. A false positive here is not a spare stop; it is a lost
    reply.

    So the reply is only suppressed when what remains after removing every
    dismissal phrase is filler: articles, a word for the thing that is
    ringing, politeness. Anything else — a room, a device, a noun this module
    has never heard of — means HA has something to say and must be allowed to
    say it, while the ring still stops.
    """
    padded = _normalise(text)
    if not padded:
        return False

    matched = False
    changed = True
    while changed:
        changed = False
        for w in _DISMISS_PHRASES:
            needle = f" {w} "
            if needle in padded:
                # Replace with a single space so the surrounding word
                # boundaries survive — " stop stop " must lose both.
                padded  = padded.replace(needle, " ", 1)
                matched = changed = True
                break

    if not matched:
        return False
    return all(word in _FILLER_WORDS for word in padded.split())


def attenuate(pcm: bytes, gain_db: float) -> bytes:
    """
    Scale S16_LE PCM by gain_db (<= 0 — a duck never boosts).

    Ducks the alert while a wake word is heard over it, so the command that
    follows reaches STT over the alarm rather than under it. Positive gain is
    clamped to 0 rather than amplifying: the alert is already mastered near
    full scale, so a boost would clip.
    """
    if gain_db >= 0.0 or not pcm:
        return pcm
    gain  = 10.0 ** (gain_db / 20.0)
    count = len(pcm) // 2
    # An odd trailing byte cannot be a whole sample, so it is dropped rather
    # than crashing frombuffer. Vectorised because this runs over every burst
    # of a ring that can last 15 minutes — numpy is already a controller dependency.
    samples = np.frombuffer(pcm[: count * 2], dtype="<i2")
    scaled  = np.clip(np.rint(samples * gain), -32768, 32767).astype("<i2")
    return scaled.tobytes()
