import asyncio

from em_arbiter import WakeArbiter, STRAGGLER_WINDOW_S

# The arbiter is timing-based; tests use short real windows (50ms) —
# generous relative to loop scheduling, quick enough for CI.
WINDOW = 0.05


def run(coro):
    return asyncio.run(coro)


def test_solo_contender_wins_after_window():
    async def main():
        arb = WakeArbiter()
        return await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
    assert run(main()) == "office"


def test_higher_snr_wins():
    async def main():
        arb = WakeArbiter()
        a = asyncio.create_task(arb.submit("office", snr=2.0, score=0.95, window_s=WINDOW))
        await asyncio.sleep(WINDOW / 5)
        b = asyncio.create_task(arb.submit("lounge", snr=8.0, score=0.40, window_s=WINDOW))
        return await asyncio.gather(a, b)
    assert run(main()) == ["lounge", "lounge"]  # SNR beats score


def test_score_breaks_snr_tie():
    async def main():
        arb = WakeArbiter()
        a = asyncio.create_task(arb.submit("office", snr=4.0, score=0.6, window_s=WINDOW))
        b = asyncio.create_task(arb.submit("lounge", snr=4.0, score=0.9, window_s=WINDOW))
        return await asyncio.gather(a, b)
    assert run(main()) == ["lounge", "lounge"]


def test_straggler_after_resolution_loses_to_winner():
    async def main():
        arb = WakeArbiter()
        first = await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
        # Round resolved; a late detection of the same utterance must not
        # open a fresh round and double-answer — even with a better SNR.
        late = await arb.submit("lounge", snr=50.0, score=0.99, window_s=WINDOW)
        return first, late
    first, late = run(main())
    assert first == "office"
    assert late == "office"


def test_separate_wake_after_straggler_window_gets_own_round():
    async def main():
        arb = WakeArbiter()
        # Simulate time passing by rewinding the recorded round start —
        # sleeping STRAGGLER_WINDOW_S for real would slow the suite.
        await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
        arb._last.started -= STRAGGLER_WINDOW_S + 0.001
        return await arb.submit("lounge", snr=1.0, score=0.5, window_s=WINDOW)
    assert run(main()) == "lounge"


def test_winner_itself_resubmitting_is_not_a_straggler_loss():
    async def main():
        arb = WakeArbiter()
        await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
        # Same device again straight away (its turn ended instantly, e.g.
        # error outcome): it must be allowed to wake again, not be told it
        # lost to itself.
        return await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
    assert run(main()) == "office"


def test_window_is_actually_waited():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await arb.submit("office", snr=5.0, score=0.9, window_s=WINDOW)
        return loop.time() - t0
    assert run(main()) >= WINDOW * 0.9


def test_three_way_round():
    async def main():
        arb = WakeArbiter()
        tasks = [
            asyncio.create_task(arb.submit("office",  snr=3.0, score=0.8, window_s=WINDOW)),
            asyncio.create_task(arb.submit("lounge",  snr=6.0, score=0.7, window_s=WINDOW)),
            asyncio.create_task(arb.submit("retreat", snr=1.0, score=0.9, window_s=WINDOW)),
        ]
        return await asyncio.gather(*tasks)
    assert run(main()) == ["lounge"] * 3
