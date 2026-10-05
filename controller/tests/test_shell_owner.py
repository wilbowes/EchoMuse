"""
The shell lock belongs to the task that acquired it.

EFF, 2026-09-04: a debloat push held its device's shell lock for 108s. The
wake-word reconcile queued behind it timed out waiting, and its `finally` ran
the holder's cleanup: it closed the websocket the debloat was writing to and
released the debloat's lock. The operator saw "could not determine active
slot".

The guard had been `Lock.locked()`, which answers "is anyone holding this".
`_shell_owner` records the task. What is pinned: a task that does not hold the
lock has no effect at all. Each test fails against the `locked()` version.
"""

import asyncio

import em_api


class FakeWS:
    """A shell websocket. Records the close so a test can assert it stayed open."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeLive:
    """The handle a caller has to a connected device."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.sent = []

    async def send_control(self, msg):
        self.sent.append(msg)


def _reset():
    """Clear every per-device dict. They are module globals that outlive a test."""
    em_api._shell_lock.clear()
    em_api._shell_owner.clear()
    em_api._shell_pending.clear()
    em_api._shell_ws.clear()
    em_api._shell_dashboard.clear()


def test_a_non_owner_cannot_release_someone_elses_lock():
    """The core invariant: releasing is gated on OWNERSHIP, not on `locked()`.

    The control assertion matters and comes first: without it, the second half
    of this test could pass because the lock was never held in the first place.
    """
    _reset()

    async def main():
        lock = asyncio.Lock()
        em_api._shell_lock["dev"] = lock

        # A real holder: acquired, and recorded as the owner.
        await lock.acquire()
        owner = asyncio.current_task()
        em_api._shell_owner["dev"] = owner

        # THE CONTROL: the lock really is held, so `lock.locked()` — the
        # predicate the old code used — is True. A test that passes because
        # nothing was locked proves nothing.
        assert lock.locked(), "control: the lock must be held for this to mean anything"

        # A different task now tries to release it. This is the waiter that
        # timed out in the incident.
        async def non_owner():
            em_api._release_shell_lock("dev")

        await asyncio.create_task(non_owner())

        # Still held. Still owned.
        assert lock.locked(), "a non-owner released a lock it did not hold"
        assert em_api._shell_owner.get("dev") is owner, "a non-owner stole the ownership record"
        return lock

    lock = asyncio.run(main())

    # And the real owner can still release it, which is what makes the lock
    # usable at all rather than merely untouchable.
    async def owner_releases():
        em_api._shell_owner["dev"] = asyncio.current_task()
        em_api._release_shell_lock("dev")

    asyncio.run(owner_releases())
    assert not lock.locked(), "the owner must be able to release its own lock"
    assert "dev" not in em_api._shell_owner, "the ownership record must be cleared on release"


def test_a_non_owner_closes_nothing_sends_nothing_and_releases_nothing():
    """`_release_shell_ws` is reached from a `finally`, so a WAITER reaches it too.

    Every action it can take is an action against another operation's session:
    closing the websocket the transfer is still using, sending `shell_close` to
    that device, and dropping its lock. All three are asserted absent, and the
    control is that a real owner does all three.
    """
    _reset()

    async def main():
        lock = asyncio.Lock()
        await lock.acquire()
        em_api._shell_lock["dev"] = lock

        ws = FakeWS()
        em_api._shell_ws["dev"] = ws
        em_api._shell_owner["dev"] = "SOME-OTHER-TASK"
        live = FakeLive("dev")

        # The waiter, running its cleanup.
        await em_api._release_shell_ws("dev", live)

        assert not ws.closed, "a non-owner closed the holder's websocket"
        assert em_api._shell_ws.get("dev") is ws, "a non-owner dropped the holder's ws record"
        assert lock.locked(), "a non-owner released the holder's lock"
        assert live.sent == [], f"a non-owner sent the device a command: {live.sent}"

        # THE CONTROL: the owner does every one of those things. Without it,
        # the four assertions above could pass because the function is a
        # no-op for everybody.
        em_api._shell_owner["dev"] = asyncio.current_task()
        await em_api._release_shell_ws("dev", live)
        assert ws.closed, "control: the owner must close the websocket"
        assert not lock.locked(), "control: the owner must release the lock"
        assert live.sent == [{"type": "shell_close"}], "control: the owner must send shell_close"

    asyncio.run(main())


def test_a_caller_that_times_out_waiting_becomes_no_ones_owner():
    """
    The ownership record is claimed AFTER a successful acquire, never before.

    `_get_device_shell_ws` waits 20s for the lock and raises on timeout. If the
    record were written before the acquire, that caller would answer "yes, I am
    the owner" to every cleanup path in the module while holding nothing — the
    exact shape of the incident, one step earlier than it happened.
    """
    _reset()

    async def main():
        lock = asyncio.Lock()
        em_api._shell_lock["dev"] = lock
        # Held by a task that never registers as owner, standing in for a
        # transfer that is mid-write.
        await lock.acquire()

        live = FakeLive("dev")

        # The real 20.0s, shortened so the test is not 20 seconds long. The
        # timeout VALUE is not what is under test; the ordering is.
        real_wait_for = asyncio.wait_for

        async def instant_timeout(awaitable, timeout):
            awaitable.close() if hasattr(awaitable, "close") else None
            raise asyncio.TimeoutError()

        asyncio.wait_for = instant_timeout
        try:
            try:
                await em_api._get_device_shell_ws(live)
                raise AssertionError("expected the acquire to time out")
            except RuntimeError as e:
                assert "timed out" in str(e), f"unexpected error: {e}"
        finally:
            asyncio.wait_for = real_wait_for

        # It did not acquire, so it must not be able to answer for the holder.
        assert lock.locked(), "the timed-out caller released a lock it never took"
        assert em_api._shell_owner.get("dev") is None, (
            "a caller that timed out waiting claimed ownership of the lock — "
            "every cleanup path would then answer yes for it"
        )

    asyncio.run(main())


def test_a_caller_that_times_out_waiting_for_the_device_releases_its_own_lock():
    """
    The other timeout: the lock WAS acquired but the device never answered.

    This one must clean up, because this task genuinely is the owner — it took
    the lock and is abandoning it. Dropping it here would strand the device's
    shell for ever, and the next caller would sit out the full 20s and then
    fail. The two timeouts have OPPOSITE obligations and the incident was
    caused by treating them the same way.
    """
    _reset()

    async def main():
        lock = asyncio.Lock()
        em_api._shell_lock["dev"] = lock
        live = FakeLive("dev")

        real_wait_for = asyncio.wait_for

        # Fail only the SECOND wait_for — the 15s wait for the device's
        # answer — and let the 20s lock acquire succeed. Distinguishing them by
        # call count is exactly how this reads in the source: acquire first,
        # then the future.
        calls = {"n": 0}

        async def counting_wait_for(awaitable, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_wait_for(awaitable, timeout)
            awaitable.close() if hasattr(awaitable, "close") else None
            raise asyncio.TimeoutError()

        asyncio.wait_for = counting_wait_for
        try:
            try:
                await em_api._get_device_shell_ws(live)
                raise AssertionError("expected the device wait to time out")
            except asyncio.TimeoutError:
                pass
        finally:
            asyncio.wait_for = real_wait_for

        assert not lock.locked(), (
            "the acquire succeeded and the device never answered — the lock "
            "must be released or the device's shell is stranded"
        )
        assert "dev" not in em_api._shell_owner, "ownership must be cleared"
        assert "dev" not in em_api._shell_pending, "the pending future must be dropped"
        assert "dev" not in em_api._shell_ws, "no ws may be left recorded"
        # The device WAS asked to open a shell, so it must be told to stop.
        assert live.sent == [{"type": "shell_open"}], live.sent

    asyncio.run(main())


def test_programmatic_sessions_do_not_mark_the_dashboard_flag():
    """
    `_get_device_shell_ws` deliberately leaves `_shell_dashboard` alone.

    The flag is how a cleanup path tells a person's open console apart from a
    programmatic transfer, and setting it here would make a wizard push look
    like a human watching — so a disconnect could close somebody's terminal.
    Present as a guard because it is a comment saying "we chose not to", and a
    choice recorded only in prose is one edit away from being wrong.
    """
    _reset()

    async def main():
        lock = asyncio.Lock()
        em_api._shell_lock["dev"] = lock
        live = FakeLive("dev")

        async def answer_the_open():
            # The device's handle_shell resolves the pending future.
            await asyncio.sleep(0)
            fut = em_api._shell_pending.get("dev")
            if fut is not None and not fut.done():
                fut.set_result(FakeWS())

        t = asyncio.create_task(answer_the_open())
        ws = await em_api._get_device_shell_ws(live)
        await t

        assert ws is em_api._shell_ws["dev"], "the resolved ws must be recorded"
        assert "dev" not in em_api._shell_dashboard, (
            "a programmatic session must not mark the dashboard flag"
        )

    asyncio.run(main())