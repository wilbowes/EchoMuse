"""The wake queue keeps the last half-second when it is backed up."""

import asyncio

import em_micqueue


def test_a_full_wake_queue_keeps_the_last_half_second():
    """64 frames is the old cap: 64 × 80ms ≈ 5.1s. After a burst the reader
    sees six frames, 480ms, and they are the newest ones."""
    q = asyncio.Queue(maxsize=64)
    for i in range(64):
        em_micqueue.offer(q, f"f{i}".encode(), keep=em_micqueue.KEEP_FRAMES)
    assert em_micqueue.KEEP_FRAMES == 6
    assert em_micqueue.KEEP_FRAMES * em_micqueue.FRAME_S == 0.48
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert got == [f"f{i}".encode() for i in range(58, 64)]


def test_a_marker_survives_the_flush():
    q = asyncio.Queue(maxsize=64)
    for i in range(8):
        em_micqueue.offer(q, f"a{i}".encode(), keep=6)
    em_micqueue.offer(q, "END", keep=6)
    for i in range(8):
        em_micqueue.offer(q, f"b{i}".encode(), keep=6)
    items = [q.get_nowait() for _ in range(q.qsize())]
    audio = [x for x in items if isinstance(x, bytes)]
    assert "END" in items
    assert len(audio) == 6
    assert audio[-1] == b"b7"


def test_the_voice_queue_still_drops_one_when_full():
    q = asyncio.Queue(maxsize=3)
    for i in range(5):
        em_micqueue.offer(q, f"f{i}".encode(), keep=None)
    assert [q.get_nowait() for _ in range(3)] == [b"f2", b"f3", b"f4"]


def test_wake_executor_is_dedicated_and_stable():
    pool = em_micqueue.wake_executor()
    assert pool is em_micqueue.wake_executor()
    assert pool._max_workers == 4
