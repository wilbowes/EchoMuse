"""Microphone queue for the wake listener.

A frame is 80ms. ``mic_queue`` used to hold 64 of them and, once full, drop
only the oldest. The queue therefore stayed full, and the listener scored
audio that was 64 × 80ms ≈ 5.1s old. ``offer`` keeps the last half-second
of audio when the wake queue is backed up. Markers (a VAD sentinel, a
listen-mode ping) stay in the queue.

Wake inference uses its own thread pool. The default pool also serves
database work, and a ``predict`` waiting there is what fills the queue.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

FRAME_S = 0.08
KEEP_S = 0.5
# 6 × 80ms = 480ms, the last half-second of the wake stream.
KEEP_FRAMES = max(1, round(KEEP_S / FRAME_S))

_executor: concurrent.futures.ThreadPoolExecutor | None = None


def wake_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Threads for wake-word inference, not the process-wide default pool."""
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="oww-wake")
    return _executor


def _is_audio(item) -> bool:
    return isinstance(item, (bytes, bytearray))


def _drain(q: asyncio.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return items


def _flush_audio(q: asyncio.Queue, keep_audio: int) -> int:
    """Drop oldest audio until ``keep_audio`` frames remain. Markers stay."""
    items = _drain(q)
    drop_n = max(0, sum(_is_audio(item) for item in items) - max(0, keep_audio))
    dropped = 0
    for item in items:
        if dropped < drop_n and _is_audio(item):
            dropped += 1
            continue
        q.put_nowait(item)
    return dropped


def offer(q: asyncio.Queue, item, *, keep: int | None = None) -> int:
    """Enqueue ``item``.

    ``keep`` is a frame cap for the wake queue: once the queue is at least
    that deep, oldest audio is dropped until the new item fits inside the
    cap. ``keep=None`` is the voice queue: drop a single oldest item, and
    only when the queue is already full.
    """
    dropped = 0
    if keep is not None and keep >= 1 and q.qsize() >= keep:
        dropped += _flush_audio(q, keep - 1)
    try:
        q.put_nowait(item)
        return dropped
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            dropped += 1
            q.put_nowait(item)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
        return dropped
