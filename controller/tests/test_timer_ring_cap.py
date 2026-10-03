"""
An unanswered alarm must outlast the longest timer anyone can set.

`MAX_RING_S` shipped at 120s until 2026-09-29 (#694, Wil declining #667's
proposal to shorten it). Twenty-one tests in test_timers.py covered the
registry's transitions and both dismissal matchers, and not one of them
mentioned this constant — a value change that walks straight through a green
suite, which is exactly what happened.

The failure is directional and that is what makes it worth a test. A cap that is
too SHORT does not fail loudly: the alarm rings, stops, and the person who set a
17-minute oven timer is told nothing. They find out when they come back to a
silent kitchen. Every other kind of mistake in this module announces itself.

So the invariant is not "the number is 900" — that is shape, and it would be
edited right alongside any deliberate change to the value, at which point it
asserts nothing. The invariant is that the cap cannot be set below the longest
timer a person can plausibly ask for. Below that ceiling the alarm is a
notification that can expire before it has been heard.
"""

import em_timers as t


def test_the_cap_is_voice_pes_fifteen_minutes():
    """
    THE invariant, and the assertion that failed at 120s.

    The reference is Voice PE, which rings a finished timer for 15 minutes
    (`delay: 15min` then `disable_repeat`). The cap exists because HA leaves a
    timer ringing indefinitely and the room should not, and Voice PE is the
    reference for "as long as it needs" because it is the speaker people already
    own. So the ceiling is Voice PE's, and going BELOW it is the bug: at 120s an
    Echo stopped ringing while Voice PE was still going, which is the "one that
    stops early is one that can be missed" case Wil declined #667's shorter
    setting over.

    Note what this deliberately does NOT assert: that the cap covers the longest
    timer anyone can set. It does not, and it is not supposed to — a person who
    sets a two-hour timer and gets fifteen minutes of alarm has been told
    something, and the alternative (ringing for two hours) is the thing the cap
    was introduced to prevent. Voice PE's number is the contract, not a derived
    one, so that is what gets pinned.
    """
    voice_pe = 900.0
    assert t.MAX_RING_S >= voice_pe, (
        f"MAX_RING_S is {t.MAX_RING_S}s, below Voice PE's {voice_pe:.0f}s. An "
        f"Echo that stops ringing before Voice PE does is an alarm that can be "
        f"missed, which is the whole reason the cap is not shorter."
    )
    # And the exact figure, because "at least" alone would let 3600 drift in
    # unnoticed and this is a value a maintainer chose deliberately.
    assert t.MAX_RING_S == voice_pe, (
        f"MAX_RING_S is {t.MAX_RING_S}s; Voice PE's cap is {voice_pe:.0f}s and "
        f"changing it is a product decision, not a refactor."
    )


def test_the_cap_is_a_ceiling_and_not_a_silence():
    """
    It bounds the ring; it must not cut it off. Zero, or a negative value, or
    something non-positive from a bad edit, reads as "never ring" rather than
    "ring briefly" — and `while loop.time() - t0 < MAX_RING_S` in
    `_ring_timer_alarm` would then never enter the loop at all, so the alarm
    would be SILENT with no error anywhere.
    """
    assert t.MAX_RING_S > 0, (
        f"MAX_RING_S is {t.MAX_RING_S!r}. The ring loop is `while ... < MAX_RING_S`, "
        f"so a non-positive cap means the alarm never rings at all."
    )