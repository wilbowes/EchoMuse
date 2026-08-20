"""
em_barge.py — when a wake-word score during a response counts as a barge-in.

Pure, so it can be tested: the suite cannot import em_controller, and this
decision shipped untested and wrong. See em_linkauth for the same reasoning.

WHY A SINGLE FRAME IS NOT ENOUGH
--------------------------------
The watcher scores 80ms frames of the device's own microphone while the
device is playing a response, at `bargeInThreshold` — a bar roughly ten times
lower than the normal wake threshold, because the echo at the mic is ~25dB
louder than a person talking over it, so speech-over-TTS scores are depressed.

A low bar plus one frame is a poor combination, and it stayed hidden because
responses used to be short. Measured 2026-08-20 on Test Device 01, asking a
conversational agent for a story with a raised token limit:

    turn        frames scored   peak score   outcome
    short reply       353          0.029      fine, under the bar
    story             336          0.091      FALSE BARGE at 8s
    story             604          0.184      FALSE BARGE at 24s

Long-form narration both scores higher — continuous synthetic speech offers
far more phoneme sequences that resemble a wake word — and gets hundreds more
chances at it. Both stories were cut off mid-sentence, and each was followed
by a phantom interrupting turn that heard nothing and ran 20-46s, during which
the device is deaf because oww_paused covers the whole turn. From the outside
that reads as "it stopped talking and then ignored me".

So the playback branch requires TWO CONSECUTIVE frames over the bar. The
thinking branch already worked this way, which is the part worth noticing:
the careful rule guarded the phase with the HIGH threshold and no audio
playing, while the phase with the low threshold, scoring against the
assistant's own continuous speech, had no debounce at all.

This does not make a false barge impossible; it makes it need two adjacent
frames rather than one, which the observed events — isolated transients well
above a much lower mean — do not look like. The threshold is the other lever
and they are complementary: the default moved to 0.25 the same day, measured
against real speech-over-TTS scores of 0.3-0.5.

The cost is one frame of latency, 80ms, on a genuine barge. A real wake word
holds a high score across several consecutive hops as the phrase completes,
so it is not close.
"""

from __future__ import annotations

from typing import NamedTuple


class BargeDecision(NamedTuple):
    fired: bool
    # Why, for the log line. Empty when nothing fired, so a caller cannot
    # accidentally log a reason for a decision that did not happen.
    note: str


def decide(*,
           score: float,
           prev_score: float,
           in_playback: bool,
           barge_threshold: float,
           wake_threshold: float) -> BargeDecision:
    """
    Whether this frame should cancel the response in progress.

    `prev_score` is the previous frame's score, and must be 0.0 for the first
    frame of a watcher run — a carried-over value from an earlier turn would
    let one frame of the new turn fire on evidence from the old one.

    Two phases, because they are not the same problem:

    * **playback** — the response is audible, so the microphone is dominated
      by the device's own speech. Low bar, two consecutive frames.
    * **thinking** — nothing is playing yet, so the bar is the normal wake
      threshold. One frame clears it outright; two consecutive frames at a
      low tier also count, because a person repeating themselves into a
      silent device is a strong signal and the cost of missing it is a turn
      that ignores them.
    """
    if in_playback:
        if score >= barge_threshold and prev_score >= barge_threshold:
            return BargeDecision(
                True,
                f"scores {prev_score:.3f}/{score:.3f} — two consecutive "
                f"frames >= {barge_threshold:.2f}",
            )
        return BargeDecision(False, "")

    if score >= wake_threshold:
        return BargeDecision(True, f"score={score:.3f} >= {wake_threshold:.2f}")

    low_tier = low_tier_for(wake_threshold)
    if score >= low_tier and prev_score >= low_tier:
        return BargeDecision(
            True,
            f"scores {prev_score:.3f}/{score:.3f} — two consecutive frames "
            f">= low tier {low_tier:.2f}",
        )
    return BargeDecision(False, "")


def low_tier_for(wake_threshold: float) -> float:
    """
    The lower bar used during thinking, when two frames agree.

    Floored at 0.2 so that lowering the wake threshold cannot drag this down
    to somewhere noise reaches — the floor is what stops a sensitivity
    setting quietly becoming a self-trigger setting.
    """
    return max(0.2, 0.4 * wake_threshold)
