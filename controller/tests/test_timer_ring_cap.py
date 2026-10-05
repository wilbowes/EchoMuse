"""
How long an unanswered timer rings.

`MAX_RING_S` shipped at 120s until 2026-09-29 (#694). None of the 21 tests in
test_timers.py mentioned it, so the value could change under a green suite. A
cap that is too short fails quietly: the alarm stops and nobody is told.
"""

import em_timers as t


def test_the_cap_is_voice_pes_fifteen_minutes():
    """Voice PE rings a finished timer for 15 minutes; an Echo that stops
    sooner can be missed. The exact value is a product decision (#667)."""
    assert t.MAX_RING_S == 900.0, (
        f"MAX_RING_S is {t.MAX_RING_S}s; Voice PE's cap is 900s and changing "
        f"it is a product decision, not a refactor."
    )


def test_the_cap_is_a_ceiling_and_not_a_silence():
    """The ring loop is `while ... < MAX_RING_S`, so a non-positive cap means
    the alarm never rings."""
    assert t.MAX_RING_S > 0
