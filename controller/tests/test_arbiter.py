import asyncio

from em_arbiter import WakeArbiter

WINDOW = 0.2


def run(coro):
    return asyncio.run(coro)


def test_solo_device_wins_immediately():
    """The winner must not wait. The old design awaited the full window on
    every wake (~364ms measured in the field, on every device) — that is
    the regression this redesign exists to remove."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        won = arb.claim("office", WINDOW)
        return won, loop.time() - t0
    won, elapsed = run(main())
    assert won == "office"
    assert elapsed < 0.01


def test_second_device_in_window_loses():
    async def main():
        arb = WakeArbiter()
        first = arb.claim("office", WINDOW)
        second = arb.claim("lounge", WINDOW)
        return first, second
    first, second = run(main())
    assert first == "office"
    assert second == "office"   # lounge told to stand down


def test_three_way_only_first_answers():
    """The 2026-07-20 field case: three devices within 184ms."""
    async def main():
        arb = WakeArbiter()
        return [arb.claim(d, WINDOW) for d in ("office", "lounge", "retreat")]
    assert run(main()) == ["office"] * 3


def test_loser_never_waits_either():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", WINDOW)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        arb.claim("lounge", WINDOW)
        return loop.time() - t0
    assert run(main()) < 0.01


def test_claim_expires_after_window():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.05)
        await asyncio.sleep(0.08)
        return arb.claim("lounge", 0.05)
    assert run(main()) == "lounge"


def test_same_device_rewaking_is_not_suppressed():
    """A device answering twice in a row is a real second utterance in the
    room it already serves — it must never be told it lost to itself."""
    async def main():
        arb = WakeArbiter()
        arb.claim("office", WINDOW)
        return arb.claim("office", WINDOW)
    assert run(main()) == "office"


def test_release_frees_the_claim_early():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 10.0)   # long window
        arb.release("office")
        return arb.claim("lounge", 10.0)
    assert run(main()) == "lounge"


def test_release_by_non_holder_is_ignored():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 10.0)
        arb.release("lounge")       # not the holder — must be a no-op
        return arb.claim("retreat", 10.0)
    assert run(main()) == "office"


def test_window_rearms_on_each_win():
    """Successive utterances in the same room keep extending suppression,
    so a distant device echoing the winner's TTS stays quiet."""
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.15)
        await asyncio.sleep(0.10)
        arb.claim("office", 0.15)   # re-arms from now
        await asyncio.sleep(0.10)
        return arb.claim("lounge", 0.15)
    assert run(main()) == "office"


def test_zero_window_suppresses_nothing():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.0)
        return arb.claim("lounge", 0.0)
    assert run(main()) == "lounge"
