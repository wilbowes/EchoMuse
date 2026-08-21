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

import array
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
# HA leaves a finished timer ringing indefinitely; the room should not.
MAX_RING_S  = 120.0
# Silence the orchestrator inserts between looped bursts (seconds).
BURST_GAP_S = 0.6

# How far the chime is attenuated once a wake word has been heard over it, so
# the command that follows ("dismiss") reaches STT over the alarm rather than
# under it. Matches the playback duckDb default — same job, same taste call.
DUCK_DB = -18.0
# How long the duck holds with no turn taking over. A wake word that starts no
# turn (a false accept on the chime itself) must not leave the alarm quiet for
# the rest of its 120s cap — the ring is a safety feature.
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
    """
    if not text:
        return False
    # Normalise: lowercase, DROP apostrophes (so "I'm" is one word, not "i m"
    # — the straight and curly forms both, since STT emits either), turn the
    # rest of the punctuation into spaces, collapse runs.
    lowered = text.lower().replace("'", "").replace("’", "")
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in lowered)
    padded  = f" {' '.join(cleaned.split())} "
    if not padded.strip():
        return False
    return any(f" {w} " in padded for w in _DISMISS_WORDS)


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
    out   = array.array("h", pcm[: count * 2])
    for i, v in enumerate(out):
        out[i] = int(max(-32768, min(32767, v * gain)))
    return out.tobytes()
