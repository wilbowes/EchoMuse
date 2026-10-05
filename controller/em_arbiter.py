"""
em_arbiter.py — multi-device wake arbitration
==============================================

When one utterance wakes more than one Echo (open-plan rooms, hallways),
every device that scored above threshold starts its own voice turn. The
result is not merely duplicated answers: each device then hears the
others' TTS through the room, transcribes it as a follow-up, and HA's
`continue_conversation` reopens the mic — a self-feeding loop that ran
for ~70 seconds on 2026-07-20 before a no-speech timeout broke it.

**First detector wins.** The first device to cross threshold claims the
utterance immediately and answers with zero added latency. Any other
device detecting within `window_s` of that claim loses and stands down.
No one ever waits.

**Only devices that can serve a turn get here.** Detection order is a
proximity proxy and says nothing about whether Home Assistant is connected
to that device, so an Echo with no pipeline behind it would otherwise win
on nearness and then fail — silencing one that was ready. The caller
(`em_controller.wake_word_listener`) stands such a device down before it
claims, via `em_esphome.can_serve_turn`. That check is deliberately NOT
repeated here: this module knows nothing of HA, and a second copy of the
rule is one that can disagree with the first.

Why not "best SNR", which this module did until 2026-07-20: it forced
every wake to wait out the whole window (~364ms measured, on every
device, because the gate was "2+ devices *connected*" rather than "in
earshot"), and the field data showed the metric was wrong anyway. On the
three-way trigger that exposed all this, SNR at detection was
0.9 / 1.15 / 0.93 — statistically indistinguishable — and the SNR winner
(Lounge) produced "What's the technique?" while the *first* detector
(Office) produced the correct "What's the weather like today?". Sound
reaches the nearer microphone sooner and louder, so it crosses threshold
sooner; detection order is a better proximity proxy than a ratio of two
noisy RMS estimates, and it is free.

`window_s` is now purely a suppression window, not a wait: it bounds how
long a claim silences other devices. It should comfortably exceed the
spread between devices hearing one utterance (~200ms observed, driven by
the device's 160ms mic batching) without being so long that a genuinely
separate wake in another room gets swallowed.

**Claims are compared by when the audio was HEARD, not when the claim
arrived** (docs/listening.md, "Arbitration"). An Echo detecting its own wake
reports how long ago it captured the audio, and a message can spend a second
or more in a TCP retransmit on this fleet's links (#139). Compared by arrival,
the near Echo's late claim fell outside the winner's window, read as a new
utterance, and started a second answer. So each claim carries `heard_at`, a
claim loses if it was heard within `window_s` of the winner's, and the
winner's claim is held for `window_s + slack_s` so a late claim still finds
it. A granted claim is never revoked — that would cut off a turn already
listening. Without `heard_at` a claim is heard on arrival, which is exactly
the old behaviour.

**A mixed fleet holds its claims** (Wil, 2026-09-24). When some Echoes
detect the wake word themselves and others are scored here, the two paths
reach this module at different speeds, so first-to-arrive is not
first-to-hear: on 2026-09-24 an Echo 10m away, detecting on the device,
claimed 16ms before the one a metre from the speaker, which was scored here
as a barge-in, and took the turn. `contest()` holds the first claim until
`hold_s` after it was heard, collects every claim heard within the window by
then, and grants the one heard earliest. Nothing is revoked: nobody holds the
utterance until the contest is decided. A fleet whose Echoes all detect the
same way races on equal terms, so it does not wait — `contest()` with
`hold_s=0` is exactly `claim()`.

**Every contest is held, and the clearly loudest Echo wins it** (Wil,
2026-10-05, #747). First-to-hear turned out to be decided by timing noise at
close range: the scorer works in 80ms frames, and in one house's twelve
contested wakes the nearest Echo heard the word 54 to 164ms AFTER the winner
in four of them, while being 11 to 26dB louder than every other Echo in all
twelve. So whenever two or more Echoes can claim, the first claim is held
`hold_s` from when it was heard, and `pick()` chooses: an Echo at least
`LOUDER_MARGIN_DB` louder than every other takes the turn; otherwise the
earliest heard does, as before.

Loudness is `peak` from em_wakelevel: post-AEC, AGC never runs on the wake
stream, and the mic gain setting is divided out. It only overrides time when
it can be trusted, and falls back to earliest-heard when any claim has no
level (older firmware), when a claim was heard over that Echo's own playback
(the reading includes the reply or the music), or when the Echoes' MICPGA and
digital gain settings differ (those are not divided out). A weak microphone
is NOT caught here: it reads quiet and would lose to a louder Echo further
away. The answer to that is the per-Echo check against its other six mics
(#731), not a wider margin.

This is not the best-SNR-after-a-wait design removed on 2026-07-20. That one
waited on every wake in every fleet and ranked by a ratio that could not tell
Echoes apart (0.9/1.15/0.93). This one measures something that separates them
by 11dB or more, and keeps time as the answer whenever it does not.

Pure asyncio, no imports from the rest of the controller — unit-tested
in tests/test_arbiter.py.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

# How much louder than every other claimant an Echo must be for loudness to
# override capture time. Healthy units have not been measured against each
# other yet; this is a starting point, well under the 11dB smallest lead in
# the data that prompted it.
LOUDER_MARGIN_DB = 6.0


class Claim(NamedTuple):
    heard: float
    device_id: str
    # em_wakelevel's peak for this wake, dBFS; None when the Echo sent none.
    peak: float | None = None
    # The gain settings the reading still contains (MICPGA, digital gain).
    gains: tuple | None = None
    # Heard over this Echo's own speaker output.
    over_playback: bool = False


def pick(claims: list, margin_db: float = LOUDER_MARGIN_DB) -> tuple:
    """
    (winner, why) among claims for one utterance.

    The Echo at least `margin_db` louder than every other wins. Without a
    clear, trustworthy lead the earliest heard wins, and a tie on time goes
    to the claim that arrived first. `why` is for the log line.
    """
    earliest = min(claims, key=lambda c: c.heard)
    if len(claims) < 2:
        return earliest, "only claim"
    if any(c.peak is None for c in claims):
        return earliest, "earliest heard; a level is missing"
    if any(c.over_playback for c in claims):
        return earliest, "earliest heard; one was heard over playback"
    if len({c.gains for c in claims}) > 1:
        return earliest, "earliest heard; gain settings differ"
    ranked = sorted(claims, key=lambda c: c.peak, reverse=True)
    lead = ranked[0].peak - ranked[1].peak
    if lead >= margin_db:
        return ranked[0], f"loudest by {lead:.0f}dB"
    return earliest, f"earliest heard; levels within {lead:.0f}dB"


class WakeArbiter:
    """
    Tracks the in-flight claim on the current utterance.

    Deliberately tiny and synchronous: claim() decides with no await, so
    the winner's turn starts on the same event-loop tick as its wake
    detection — that is the whole point of the redesign.
    """

    def __init__(self) -> None:
        self._winner: str | None = None
        self._heard_at: float = 0.0
        # The open contest, if a held claim is waiting: the heard time that
        # opened it, the Claims so far, and the future every claimant awaits.
        self._contest: tuple[float, list, asyncio.Future] | None = None
        # Why the last contest went the way it did, for the caller's log.
        self.last_why: str = ""

    def claim(self, device_id: str, window_s: float,
              heard_at: float | None = None, slack_s: float = 0.0) -> str:
        """
        Try to claim the current utterance. Returns the winning device_id
        — equal to device_id if this device won and should answer, or
        another device's id if this detection is a duplicate to discard.

        `heard_at` is when this claim's audio was captured, in the event
        loop's clock (em_listen.heard_at); None means now. `slack_s` extends
        how long the winning claim is held, to cover claims still in flight.

        Returns immediately; there is no waiting on either path.
        """
        now = asyncio.get_running_loop().time()
        heard = now if heard_at is None else min(heard_at, now)
        held = (
            self._winner is not None
            and self._winner != device_id
            and abs(heard - self._heard_at) < window_s
            and now - self._heard_at < window_s + max(0.0, slack_s)
        )
        if held:
            return self._winner
        # Either nothing is claimed, the claim has expired, this was heard
        # outside the window (a separate utterance), or this is the same
        # device waking again (a genuinely new utterance in the room that
        # already answered). Re-arm from this claim.
        self._winner = device_id
        self._heard_at = heard
        return device_id

    async def contest(self, device_id: str, window_s: float,
                      heard_at: float | None = None, slack_s: float = 0.0,
                      hold_s: float = 0.0, peak: float | None = None,
                      gains: tuple | None = None, over_playback: bool = False,
                      margin_db: float = LOUDER_MARGIN_DB) -> str:
        """
        claim(), held so that every Echo that heard the utterance is counted
        and `pick()` chooses among them: the clearly loudest, else the one
        that HEARD it first. See the module docstring.

        Waits until `hold_s` after the first claim of the utterance was heard
        (less any time it already spent getting here), then returns the
        winner to every claimant. A claim arriving after the decision meets
        the granted winner exactly as in claim(). `hold_s <= 0` does not wait.
        """
        if hold_s <= 0:
            return self.claim(device_id, window_s, heard_at, slack_s)
        loop = asyncio.get_running_loop()
        now = loop.time()
        heard = now if heard_at is None else min(heard_at, now)

        if self._contest is not None:
            opened, entries, decided = self._contest
            if abs(heard - opened) < window_s:
                entries.append(Claim(heard, device_id, peak, gains, over_playback))
                return await asyncio.shield(decided)

        # No contest to join. A granted winner still holds this utterance.
        if (self._winner is not None and self._winner != device_id
                and abs(heard - self._heard_at) < window_s
                and now - self._heard_at < window_s + max(0.0, slack_s)):
            return self._winner

        entries = [Claim(heard, device_id, peak, gains, over_playback)]
        decided: asyncio.Future = loop.create_future()
        self._contest = (heard, entries, decided)
        try:
            await asyncio.sleep(max(0.0, heard + hold_s - now))
        finally:
            won, self.last_why = pick(entries, margin_db)
            winner = won.device_id
            if self._contest is not None and self._contest[2] is decided:
                self._contest = None
            self._winner, self._heard_at = winner, won.heard
            if not decided.done():
                decided.set_result(winner)
        return winner

    def release(self, device_id: str) -> None:
        """
        Drop a claim once its turn is over, so an immediate follow-up from
        another device isn't suppressed by a stale window. Ignores calls
        from a device that doesn't hold the claim.
        """
        if self._winner == device_id:
            self._winner = None
            self._heard_at = 0.0
