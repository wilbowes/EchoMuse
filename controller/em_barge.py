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

TWO REASONS TO GIVE UP THE TURN, AND ONLY ONE OF THEM IS A TRACEABLE EVENT
-----------------------------------------------------------------------
`barge_ceded` covers two different things: another Echo won the utterance, or
`can_serve_turn` says there is no pipeline behind this one. Only the first was
handled the way the wake path handles it, so someone talking over an answer
during an HA outage got silence, no orange flash and nothing in the Activity
tab — the barge path's version of the bug `record_dropped_wake` was written to
stop happening on the wake path.

The no-HA stand-down records and cues, for the same two reasons it does there:

  - **A barge with no trace is indistinguishable from a device that heard
    nothing.** The activity history is where an outage becomes visible
    afterwards, and an HA outage is precisely when somebody barges in.
  - **The cue reports the DEVICE's state, not a turn's outcome** — the wake word
    worked, the controller is here, HA is not.

Losing arbitration deliberately keeps only the log line. The wake path logs
there too, and a cue would be reporting a race the user has no interest in:
something in the house answered them, and they heard it.

The trigger label is therefore its own decision here rather than a format
string at the call site. Readers key on the trigger's PREFIX: `wake_word_phrase`
sent to HA and `_persist_turn`'s shadow block both test
`startswith("wakeword")`, and the wake statistics in the Activity tab are read
the same way — so a borrowed prefix files a barge inside the wake-word numbers
and names a wake word this turn never sent. "barge-in" is what a barge turn
already records as; CONVERSATION_TRIGGER exists for the same reason, so that
HA-initiated turns are not wake-word turns either.

A BARGE DURING THINKING IS HELD, NOT TAKEN (#195)
------------------------------------------------
`decide` says a wake word arrived; it does not say the person meant it. The
abort it used to trigger the instant the wake word scored is the point of no
return — HA's pipeline has no resume, so the answer in flight is lost — and a
wake word spoken over a response by mistake, or thought better of, threw the
answer away and started a turn that heard nothing and ran 20-46s with the
device deaf throughout. Bounding that damage is what #195 is.

So during THINKING the interruption is deferred: playback stops (there is
none yet — the thinking spinner is all that is on the screen), HA's run is
left alone, the follow-up audio is held, and `FollowUpWindow` decides whether
somebody is actually talking. Only if they are is HA aborted and the
interrupting turn started — a second run is never started before the first is
aborted, which is the invariant #194 established, so this defers the abort
rather than weakening it.

The window is a TASTE PARAMETER, the add-on option `barge_confirm_ms`
(`EM_BARGE_CONFIRM_MS`), for the same reason `duckDb` and the LED meter curve
are: the right value for one room is a person talking immediately after their
wake word, and for another it is leaving room for a breath. A default tuned at
a desk would be a guess.

Why the tail matters more than the bar: the frames just after a fire are the
wake word's OWN audio, several frames above any speech bar, so a rule of
"speech within 1.5s" fires instantly on an accidental barge and does nothing
at all. The wake word outlives the frame that scored it — the crossing comes
as the phrase completes — which is the same fact `VOICE_PREROLL_DISCARD`
(240ms) exists for on the turn path. Speech is therefore only counted AFTER
that tail, and what the window is really asking is "did they keep talking?".

Fails towards leaving things alone. If the room is above the bar inside the
window the interrupting turn is taken (the old behaviour), and if the answer
starts playing while the window is open, or the frames stop before it is
answered — the stream dying, the turn unwinding — the barge is dropped and
nobody was disturbed: the answer HA was already making still arrives.

Not covered here: a barge during PLAYBACK. HA's run has already finished by
then, so there is nothing to leave alone — stopping the speaker is the whole
of it, and resuming discarded TTS is a different problem.
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


class CedeVerdict(NamedTuple):
    # Give up the interrupting turn: playback has already stopped and stays
    # stopped, and this device does not run the turn. Both reasons below set
    # it, and they differ in everything else.
    ceded: bool
    # There is no pipeline behind this device — `can_serve_turn` refused, so
    # nothing could have answered on it. The one reason that leaves a record
    # and a cue; arbitration loss leaves neither, on purpose.
    no_ha: bool
    # The label to record the dropped wake under, and "" on every outcome that
    # must not leave a record — so a caller cannot file a no_ha turn for a
    # barge that lost a race.
    trigger_label: str


def cede(*, serves: bool, won_by: str, device_id: str,
         score: float) -> CedeVerdict:
    """
    Whether a barge-in gives up the turn, and what it leaves behind.

    `serves` is `em_esphome.can_serve_turn`, read BEFORE any claim is taken —
    an Echo with no HA behind it must never take an arbitration window away
    from one that could answer. `won_by` is the arbiter's answer, which is the
    device's own id when it won (and when arbitration did not run at all).

    The two failures are not the same event and must not share a handler:

    * **no HA** — nothing on this device could ever have answered. Record it
      and cue it, exactly as the wake listener does for the same stand-down.
      Playback stopping is not the news; a missing pipeline is.
    * **lost arbitration** — another Echo has the utterance and is answering
      it. Log only. A cue would report a race nobody asked about, and the
      record would put a second "wake heard" in the history for a turn the
      neighbour is running.

    Split out of `_barge_watcher` and `_private_barge` for the reason
    `em_linkauth` and `em_turnclock` were: the suite cannot import
    em_controller, so this was a decision with no coverage — and a copy at
    each of those two call sites is two copies that can disagree.
    """
    if not serves:
        return CedeVerdict(True, True, f"barge-in({score:.3f})")
    if won_by != device_id:
        return CedeVerdict(True, False, "")
    return CedeVerdict(False, False, "")


# ─── The confirmation window (#195) ──────────────────────────────────────────

# Milliseconds a person has, after a thinking-phase crossing, to still be
# talking. Long enough to leave room for a breath between the wake word and
# the command, short enough that a genuine barge is not left hanging; the
# issue proposed ~1.5s and the whole point of the knob is that a desk cannot
# decide it.
CONFIRM_DEFAULT_MS = 1500.0

# The ceiling. Past a couple of seconds this stops being a confirmation and
# becomes "the answer wins unless you say another wake word", and every
# millisecond of it is latency added to a real interruption. The cap is only
# here so a typo cannot turn the behaviour into something nobody chose.
CONFIRM_MAX_MS = 5000.0

# The wake word's own tail, in milliseconds. The crossing does not arrive on
# the frame the phrase started — it arrives as the phrase completes — so the
# frames right after it are loud with the wake word itself. Same 240ms as
# em_esphome.VOICE_PREROLL_DISCARD (3 frames of 80ms), which drops the same
# audio from the start of the interrupting turn.
CONFIRM_TAIL_MS = 240.0


def confirm_ms(raw, default=CONFIRM_DEFAULT_MS, maximum=CONFIRM_MAX_MS):
    """The window in milliseconds, from the raw environment string.

    Read at import with whatever an operator typed into the add-on box, so
    this follows em_turnclock.tts_wait_seconds: it must never raise, and it
    must never return something that behaves as "off". Anything that is not a
    positive number is the default — 0 is not "take the barge at once",
    because the caller has no such state and a window of zero would drop
    every barge instead.
    """
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def speech_bar(floor: float) -> float:
    """The RMS a frame has to clear to be somebody talking.

    Three times the room's measured noise floor, with an absolute lower
    bound for the floor-not-yet-warmed case. The turn path applies the same
    rule to the same 80ms frames (em_esphome's `_is_speech`) — it is the one
    reading of "speech" this controller has, and the barge window must not
    invent a second that can disagree with it.
    """
    return max(3.0 * floor, 0.004)


class FollowUpWindow:
    """
    Whether the person who barged in is still talking (#195).

    Opened on the frame that crossed during THINKING — the decision that
    cannot be undone, because HA's pipeline has no resume — and fed every
    frame after it. `feed` answers "take" once the room is talking, "drop"
    when the window closes with nobody there, and "" in between so the caller
    keeps watching and leaves HA's run alone.

    `opened` and `at` are the frames' CAPTURE times (em_listen.captured). The
    question is what the room was doing; a link's retransmits must not spend
    the window that the person's speech was supposed to have, which is the
    same reason the turn clock measures its no-speech window from the first
    real frame (#139).

    The tail is skipped for the reason CONFIRM_TAIL_MS exists: the wake
    word's own audio arrives after the crossing, well above any speech bar,
    so counting speech from the crossing makes the window fire on the
    accidental barge it is here to catch — and a rule that always fires is
    not a rule.

    Not used by a privately listening Echo (`_private_barge`): its crossings
    arrive as a wake the device decided on, with no audio here to confirm
    with, and the device's own listen gate already drops a re-trigger while
    its session is open.
    """

    def __init__(self, opened: float, confirm_ms: float,
                 floor: float = 0.0, tail_ms: float = CONFIRM_TAIL_MS):
        self.opened = opened
        self.confirm_ms = confirm_ms
        self._confirm = confirm_ms / 1000.0
        self._tail = tail_ms / 1000.0
        self._floor = floor

    def feed(self, rms: float, at: float) -> str:
        """One frame: "take", "drop", or "" to keep waiting."""
        since = at - self.opened
        if since <= self._tail:
            return ""
        if since >= self._confirm:
            return "drop"
        if rms >= speech_bar(self._floor):
            return "take"
        return ""
