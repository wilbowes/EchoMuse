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
    * **thinking** — nothing is playing yet, so there is no echo to talk
      over and no reason to lower the bar. One frame at the normal wake
      threshold, and nothing else.

      There used to be a second, lower tier here: two consecutive frames at
      `max(0.2, 0.4 * wake_threshold)`. Removed in #337, and the reason it
      existed is the reason it had to go.

      It was added for one observation (2026-07-12): a GENUINE barge
      plateauing at 0.240/0.242 against a threshold of 0.50, missed, and the
      unwanted answer played in full. But that reading was depressed by the
      watcher scoring a COLD model — it resets per turn, and the first
      FEATURE_WINDOW chunks score reset noise as much as the room. That
      cause was fixed separately by `em_oww_warmup`, which gates the
      watcher until its window is clean. The compensation was never removed.

      What was left is a bar sitting in the warm-noise band. Measured across
      505 near-misses and 30 real detections on a live fleet: **no genuine
      wake word scored below 0.502, and noise reached 0.462.** On 2026-08-25
      two trusted, warm frames at 0.206/0.238 cancelled a real request. A
      person actually repeating themselves says the wake word, scores like
      it, and is caught by the branch above.

      The cost of getting this wrong is not a missed barge. A false barge
      while thinking discards the request before speech recognition has run
      (`outcome=barged`, `text=''`), reopens the microphone at someone who
      did not ask for it, and starts a phantom turn that keeps the device
      deaf for its whole length.
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
    return BargeDecision(False, "")

