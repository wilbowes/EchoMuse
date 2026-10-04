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
    to be dismissed?" and reports the ring transitions (start / stop) that
    the async orchestrator in em_controller acts on.
  * DismissListen decides whether someone spoke after a wake word heard
    while a timer rings, which is what stops it by voice.

The alert audio is the bundled Home Assistant Voice PE sound
(ALARM_SOUND_FILE), decoded by em_controller — it is already 48kHz mono, the
format the device speaker plane expects, so the alert needs no firmware
support.

Dismissal of a RINGING alarm is always local, because HA discards a timer the
moment it finishes and can no longer cancel it: a dot-button tap, or the wake
word followed by anything spoken (DismissListen, below). A CANCELLED event
still dismisses too — HA sends one when a timer is cancelled while it is
still counting down — so the registry handles both. Either way the registry is
cleared and the ring stops; a safety cap bounds a ring nobody ever answers.
"""

from __future__ import annotations

import os

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
    timer has not been dismissed.
    """

    def __init__(self) -> None:
        # timer_id -> "running" | "finished"
        self._timers: dict[str, str] = {}

    @property
    def ringing(self) -> bool:
        return any(state == "finished" for state in self._timers.values())

    def apply(self, event_type: int, timer_id: str = "") -> str:
        """
        Fold one event into the registry.

        Returns RING_START when this event begins a ring (first finished timer
        after a quiet registry), RING_STOP when it ends one (the last finished
        timer was dismissed/cancelled), or RING_NONE otherwise.
        """
        was = self.ringing

        if event_type == TIMER_FINISHED:
            self._timers[timer_id] = "finished"
        elif event_type == TIMER_CANCELLED:
            # A CANCELLED for a still-running timer must NOT affect the ring,
            # and a CANCELLED for a finished one is exactly how a spoken "stop"
            # dismisses it — pop covers both.
            self._timers.pop(timer_id, None)
        elif event_type in (TIMER_STARTED, TIMER_UPDATED):
            self._timers[timer_id] = "running"
        # Unknown event types are ignored (degrade to old behaviour).

        now = self.ringing
        if now and not was:
            return RING_START
        if was and not now:
            return RING_STOP
        return RING_NONE

    def clear(self) -> bool:
        """
        Drop all timers (local dismissal). Returns whether it was ringing —
        so a caller can tell "I stopped an alarm" from "nothing was ringing".
        """
        was = self.ringing
        self._timers.clear()
        return was

    def active_count(self) -> int:
        return len(self._timers)


# ── Stopping a ringing timer by voice ───────────────────────────────────────
# HA DISCARDS a timer the moment it finishes: cancelling works while one is
# counting down, but once it fires HA's timer manager no longer knows about it
# and answers "there are no timers" to a spoken stop (measured 2026-08-13).
# That is HA's design: it hands ringing to the satellite and expects the
# satellite to own dismissal, as its own Voice PE does.
#
# The rule (Wil, 2026-10-04): a wake word heard while any timer rings PAUSES
# the ring, anything spoken after it STOPS the ring, and silence lets it
# resume. Only whether someone spoke is asked, never what they said, so it
# works in every language and "<wake word>, stop timer" behaves as people
# expect from other assistants. A wake alone does not stop it: a false wake
# would then silence an alarm nobody answered.
#
# It replaced a list of English stop words matched against the transcript
# (#167). That needed a list per language (#737), and the chime garbled the
# transcript it depended on. The cost of this rule is that a command spoken
# over a ringing timer stops the timer and is not sent to Home Assistant.

# How long after the wake to wait for speech before the ring resumes.
DISMISS_LISTEN_S = 4.0
# Consecutive 80ms frames at or above the speech probability that count as
# someone speaking. Two (160ms) is shorter than any word and longer than a
# click.
DISMISS_SPEECH_FRAMES = 2
# em_speechgate's operating point for the same detector: speech measured
# 0.72-1.00 and non-speech 0.02-0.03 on our recordings.
DISMISS_SPEECH_PROB = 0.5
# The ring stops the moment speech is heard, but the listening ring stays lit
# until the person has finished, so it looks listened to rather than cut off
# (Wil, 2026-10-04). Finished is this many quiet frames in a row (400ms, a
# pause between sentences), or DISMISS_TAIL_S at the longest.
DISMISS_END_FRAMES = 5
DISMISS_TAIL_S = 4.0


class DismissListen:
    """
    Whether someone spoke in the frames after a wake word.

    Fed one speech probability per 80ms frame, in order. The first
    `preroll_frames` are ignored: they carry the tail of the wake word
    itself, which is speech and must not count as the answer.
    """

    def __init__(self, preroll_frames: int,
                 speech_frames: int = DISMISS_SPEECH_FRAMES,
                 threshold: float = DISMISS_SPEECH_PROB,
                 end_frames: int = DISMISS_END_FRAMES) -> None:
        self._skip = max(0, preroll_frames)
        self._need = max(1, speech_frames)
        self._end = max(1, end_frames)
        self._threshold = threshold
        self._run = 0
        self._quiet = 0
        self.frames = 0
        self.peak = 0.0
        self.spoke = False
        # Speech was heard and has since stopped.
        self.finished = False

    def push(self, prob: float) -> bool:
        """Take one frame's probability; returns whether speech was heard."""
        self.frames += 1
        if self.frames <= self._skip:
            return False
        if self.spoke:
            self._quiet = self._quiet + 1 if prob < self._threshold else 0
            if self._quiet >= self._end:
                self.finished = True
            return True
        self.peak = max(self.peak, prob)
        self._run = self._run + 1 if prob >= self._threshold else 0
        if self._run >= self._need:
            self.spoke = True
        return self.spoke
