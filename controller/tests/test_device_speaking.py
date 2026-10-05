"""
The `speaking` flag and the dashboard push are one operation.

`stream_speaker` set the flag and nothing pushed the change, so a turn read
listening -> thinking -> idle and showed Speaking only if the dashboard's 5s
poll landed mid-playback. `_set_speaking` is now the single writer and pushes.

Starting to speak also clears `thinking`: both reach the dashboard and
`speaking` outranks it, so a stale `thinking` reappears when speaking ends.

`test_thinking_transition.py` guards the same thing from the source. This
drives the code.
"""

import asyncio

import pytest

import _oww_stub

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub


@pytest.fixture(autouse=True)
def pushed():
    """
    em_controller is imported once per session, and these tests replace
    `_push_device_state` on the module. Without restoring it, every later test
    in the session would push to a list this file created.
    """
    original = em_controller._push_device_state
    pushed: list = []
    em_controller._push_device_state = lambda device: _record(pushed, device)
    yield pushed
    em_controller._push_device_state = original
    # em_controller is already imported and holds its own reference to the stub
    # Model, so the stub itself is no longer needed — and sys.modules is global.
    # Four tests in test_oww_assets.py and test_oww_models.py call
    # `pytest.importorskip("openwakeword")` to skip when the real package is
    # absent; leaving the stub installed makes that check succeed, they stop
    # skipping, and they fail on the missing MODELS table. That failure is four
    # steps removed from anything to do with openwakeword, which is the worst
    # kind of test failure to hand someone.
    _oww_stub.uninstall()


async def _record(pushed, device):
    pushed.append({
        "device_id": device.device_id,
        "speaking": device.speaking,
        "thinking": device.thinking,
        "listening": device.listening,
    })


def _device(device_id="dev1"):
    """A Device with nothing connected to it. The state methods under test touch
    no socket — they set fields and push — so a bare instance is the whole
    fixture. Everything else stays at its initialised value."""
    return em_controller.Device(
        device_id,
        "10.0.0.1",      # ip — never sent anywhere by the paths under test
        [],              # capabilities
        None,            # control_ws — the state methods write no frames
    )


# ── The flag and the push are the same operation ─────────────────────────────

def test_setting_speaking_pushes_the_transition(pushed):
    """
    THE bug. Before `_set_speaking`, nothing pushed this edge, so the dashboard
    learned about speaking only by polling and usually missed it.

    The assertion is on the PUSH, not the flag — the flag was always correct.
    That is the whole shape of the bug: a thing set and a thing told, drifting
    apart, where only one of them was being tested.
    """
    d = _device()

    async def main():
        await d._set_speaking(True)
        await d._set_speaking(False)

    asyncio.run(main())

    assert [p["speaking"] for p in pushed] == [True, False], (
        f"the speaking transitions were not pushed in order: {pushed}"
    )
    assert all(p["device_id"] == "dev1" for p in pushed), pushed


def test_a_repeated_set_does_not_push(pushed):
    """
    Idempotent. The push is a socket write to every open dashboard tab, and
    turn end pushes the same state moments later — so a redundant edge is
    traffic on the control plane for no information.
    """
    d = _device()

    async def main():
        await d._set_speaking(True)
        await d._set_speaking(True)
        await d._set_speaking(True)

    asyncio.run(main())
    assert len(pushed) == 1, f"three identical sets pushed {len(pushed)} times: {pushed}"


# ── Mutual exclusion ─────────────────────────────────────────────────────────

def test_starting_to_speak_clears_thinking(pushed):
    """
    The stale-thinking case: `thinking` set, then speech starts. The tile would
    otherwise fall BACK to Thinking the moment speaking cleared, which is what
    made an early clear look like the device had started thinking again
    mid-response.
    """
    d = _device()
    d.thinking = True

    async def main():
        await d._set_speaking(True)

    asyncio.run(main())

    assert d.thinking is False, (
        "thinking survived the start of speech — when speaking clears, the tile "
        "will read Thinking again mid-response"
    )
    assert pushed[-1]["thinking"] is False, (
        f"the push still carried thinking=true: {pushed[-1]}"
    )


def test_clearing_speaking_leaves_thinking_alone(pushed):
    """
    Only the RISING edge clears thinking. Turn end clears speaking, and turn end
    follows thinking — so clearing thinking there would wipe a state the turn is
    legitimately in, and #370 is precisely about the ring being held through the
    STT window because a route forgot to enter it.
    """
    d = _device()

    async def main():
        await d._set_speaking(True)
        d.thinking = True          # the STT/intent phase begins
        await d._set_speaking(False)

    asyncio.run(main())
    assert d.thinking is True, (
        "clearing speaking wiped thinking — turn end lands after the thinking "
        "phase begins, and this is the #370 shape"
    )


# ── A failed push must not fail the speaker ──────────────────────────────────

def test_a_failing_push_does_not_raise(pushed):
    """
    The push is wrapped in `except BaseException`, and has to be.
    `_set_speaking` runs in `stream_speaker`'s finally, which a barge-in reaches
    by cancelling the task, and `except Exception` does not catch CancelledError.
    """
    d = _device()

    async def main(raiser):
        em_controller._push_device_state = raiser
        try:
            await d._set_speaking(True)
            await d._set_speaking(False)
        finally:
            em_controller._push_device_state = lambda device: _record(pushed, device)

    # An ordinary error.
    async def boom(device):
        raise RuntimeError("dashboard websocket is gone")

    asyncio.run(main(boom))
    assert d.speaking is False, "the flag must still be set even when the push fails"

    # And a cancellation, which is the case that reaches a speaker stream.
    async def cancelled(device):
        raise asyncio.CancelledError()

    d2 = _device("dev2")
    em_controller._push_device_state = cancelled
    try:
        asyncio.run(d2._set_speaking(True))
    except asyncio.CancelledError:
        pytest.fail(
            "a CancelledError from the dashboard push escaped _set_speaking — "
            "stream_speaker's finally is reached when barge-in cancels the task, "
            "so except Exception would not catch this"
        )
    finally:
        em_controller._push_device_state = lambda device: _record(pushed, device)

    assert d2.speaking is True, "the flag must be set even when the push is cancelled"


# ── The flag is not the buffer ───────────────────────────────────────────────

def test_speaker_busy_is_counted_and_is_not_speaking(pushed):
    """
    `speaking` clears when the socket writes finish, while the Echo still has
    seconds of audio buffered. Anything that must not write over live audio tests
    `speaker_busy`, a count of streams playing or draining. A second writer
    trusting `speaking` interleaves frames and stutters, with no error.
    """
    d = _device()

    async def main():
        await d._set_speaking(True)
        await d._set_speaking(False)

    asyncio.run(main())

    assert d.speaking is False, "control: speaking should be clear"
    assert d.speaker_busy == 0, (
        "control: nothing incremented speaker_busy, so it is 0 — and that is "
        "exactly the divergence this documents"
    )