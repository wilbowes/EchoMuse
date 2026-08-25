"""
#331: the limiter's look-ahead tail can push the final buffer back over a
device period, and the pad that follows it must not go negative.

`Device.stream_speaker_chunks` drains `pending` in whole periods, then
appends `stream_eq.flush()` — the limiter's held look-ahead — and pads what
is left up to one period. It padded straight from there, and

    chunk += bytes(SPEAKER_BYTES - len(chunk))

raises `ValueError: negative count` the moment the flush pushes the
remainder past a period. Measured on the 2026-08-25 soak as one turn ending
`tts_error` with `tts_bytes=0`, ~1.7s after playback had started.

The suite cannot import `em_controller`, so the numbers come from the
importable half of the chain and the ordering is pinned against the source.
"""

import re
from pathlib import Path

import numpy as np

import em_eq
import em_limiter

CONTROLLER = Path(__file__).resolve().parents[1]
SRC = (CONTROLLER / "em_controller.py").read_text()

SPEAKER_PERIOD = int(re.search(r"^SPEAKER_PERIOD\s*=\s*(\d+)", SRC, re.M).group(1))
SPEAKER_BYTES = SPEAKER_PERIOD * 2


def _stream_body() -> str:
    """
    stream_speaker_chunks with comments stripped.

    The comments there necessarily quote the expression they exist to warn
    about, so a source search that includes them matches the warning rather
    than the code — which is how the first version of this test failed.
    """
    body = SRC[SRC.index("async def stream_speaker_chunks"):]
    body = body[:body.index("\n# The live device registry")]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _tail_bytes() -> int:
    """The real flush tail, measured off a real chain rather than derived."""
    eq = em_eq.StreamingEQ(48000, limiter=em_limiter.Limiter(48000))
    pcm = (np.random.default_rng(0).integers(-8000, 8000, 48000)
           .astype(np.int16).tobytes())
    eq.process(pcm)
    return len(eq.flush())


def test_the_limiter_actually_holds_a_tail():
    """If this ever became zero the bug would vanish for the wrong reason."""
    assert _tail_bytes() > 0


def test_the_tail_can_push_the_remainder_past_a_period():
    """
    The arithmetic that makes the bug reachable, stated as a fact about the
    real constants rather than a comment that can go stale.

    The drain loop leaves 0..SPEAKER_BYTES-1 bytes. Anything above
    SPEAKER_BYTES - tail overflows once the tail is appended, which for a
    uniform remainder is tail/SPEAKER_BYTES of all responses.
    """
    tail = _tail_bytes()
    largest_remainder = SPEAKER_BYTES - 1
    assert largest_remainder + tail > SPEAKER_BYTES, (
        "the tail can no longer overflow a period — if that is deliberate, "
        "this test should be deleted along with the second drain"
    )


def test_the_flush_is_drained_before_the_remainder_is_padded():
    """
    The fix, and the thing that must not be tidied away: a period has to be
    drained AFTER the flush. Padding straight from the flush is the bug, and
    it fails as a ValueError rather than as bad audio.
    """
    body = _stream_body()
    flush = body.index("stream_eq.flush()")
    pad = body.index("bytes(SPEAKER_BYTES - len(")
    between = body[flush:pad]
    assert "drain_whole_periods()" in between, (
        "the limiter tail must be drained into whole periods before the "
        "remainder is padded, or the pad length goes negative"
    )


def test_the_pad_can_never_be_negative_by_construction():
    """
    The padded value must be the remainder AFTER the drain, so it is < one
    period by construction. Padding `len(chunk)` where chunk already includes
    an undrained tail is what went negative.
    """
    body = _stream_body()
    pad_line = re.search(r"bytes\(SPEAKER_BYTES - len\((\w+)\)\)", body)
    assert pad_line, "the final pad has moved — re-pin this test"
    padded = pad_line.group(1)
    assert padded == "pending", (
        f"the pad is measured against `{padded}`; it must be `pending`, which "
        "the drain above has just reduced below one period"
    )
