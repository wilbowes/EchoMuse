"""
DTLN framing and the suppression floor.

The models are vendored into the image and are not in the tree, and
onnxruntime is not in the test environment — so the sessions are stubbed and
what is tested is the part that was actually wrong: the overlap-add
alignment, and whether the output can still reach digital silence.
"""

import numpy as np
import pytest

import em_ns


class _Stub:
    """Stands in for one ONNX session. fn(data) -> data of the same shape."""

    def __init__(self, fn):
        self._fn = fn

    def run(self, _outputs, feeds):
        data  = next(v for k, v in feeds.items() if k == "data")
        state = next(v for k, v in feeds.items() if k == "state")
        return [self._fn(data), state]


def _sessions(mask_fn, post_fn):
    m1 = (_Stub(mask_fn), "data", "state", "data_out", "state_out")
    m2 = (_Stub(post_fn), "data", "state", "data_out", "state_out")
    return (m1, m2)


def _passthrough():
    """
    A DTLN that suppresses nothing. The mask is all ones, and the post-net
    returns the block scaled by 1/overlap so the overlap-add reconstructs
    exactly — window 512 over hop 128 sums four copies of every sample.
    """
    overlap = em_ns.BLOCK_LEN // em_ns.BLOCK_SHIFT
    return _sessions(
        lambda mag: np.ones_like(mag),
        lambda blk: (blk / overlap).astype(np.float32),
    )


def _silencer():
    """A DTLN that gates everything away — the failure mode being floored."""
    return _sessions(
        lambda mag: np.zeros_like(mag),
        lambda blk: np.zeros_like(blk),
    )


def _tone(n=4096, freq=440.0, amp=0.3):
    t = np.arange(n, dtype=np.float32) / 16000.0
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (x * 32768.0).astype(np.int16)


def _pcm(b):
    return np.frombuffer(b, dtype=np.int16)


def test_passthrough_reconstructs_the_input_delayed_by_the_ola_latency():
    """
    The output sample written at index i is the OLDEST hop of the accumulator,
    so it corresponds to input BLOCK_LEN - BLOCK_SHIFT earlier. If that is
    wrong the floor blend mixes a signal against a shifted copy of itself and
    comb-filters it, which is worse than the artefact being fixed.
    """
    src = _tone()
    d = em_ns.StreamingDenoiser(sessions=_passthrough())
    out = _pcm(d.process(src.tobytes()))

    assert out.size == src.size
    d_ = em_ns.OLA_DELAY
    # Everything after the priming delay must be the input, sample for sample.
    np.testing.assert_allclose(out[d_:], src[:-d_], atol=2)


def test_a_total_gate_lands_on_the_floor_instead_of_silence():
    src = _tone()
    d = em_ns.StreamingDenoiser(sessions=_silencer())
    out = _pcm(d.process(src.tobytes())).astype(np.float64)

    expected = src[: -em_ns.OLA_DELAY].astype(np.float64) * (
        10.0 ** (em_ns.NS_FLOOR_DB / 20.0)
    )
    got = out[em_ns.OLA_DELAY:]
    np.testing.assert_allclose(got, expected, atol=2)

    # The point of the floor: a fully-gated stream is attenuated, never
    # zeroed. Exact zeros may only occur where the floored input rounds
    # below one LSB.
    audible = np.abs(expected) >= 1.0
    assert np.count_nonzero(got[audible] == 0) == 0


def test_the_floor_does_not_change_speech_that_the_model_passes():
    """
    The blend is convex, so where the model returns the input untouched the
    output is the input — the floor must not add level to speech.
    """
    src = _tone()
    d = em_ns.StreamingDenoiser(sessions=_passthrough())
    out = _pcm(d.process(src.tobytes())).astype(np.float64)

    ref = src[: -em_ns.OLA_DELAY].astype(np.float64)
    got = out[em_ns.OLA_DELAY:]
    rms_ref = float(np.sqrt(np.mean(ref**2)))
    rms_got = float(np.sqrt(np.mean(got**2)))
    assert rms_got == pytest.approx(rms_ref, rel=0.01)


def test_frames_split_across_calls_are_carried_not_dropped():
    """80ms frames are exactly ten hops, but nothing guarantees the caller."""
    src = _tone(n=2048)
    whole = em_ns.StreamingDenoiser(sessions=_passthrough())
    ragged = em_ns.StreamingDenoiser(sessions=_passthrough())

    a = _pcm(whole.process(src.tobytes()))
    raw = src.tobytes()
    b = b""
    for cut in range(0, len(raw), 300):        # not a multiple of the hop
        b += ragged.process(raw[cut:cut + 300])
    b = _pcm(b)

    n = min(a.size, b.size)
    np.testing.assert_allclose(a[:n], b[:n], atol=2)


def test_zero_report_separates_a_chewed_stream_from_a_silent_room():
    src = _tone()

    gated = em_ns.StreamingDenoiser(sessions=_silencer())
    gated.process(src.tobytes())
    assert gated.samples == src.size
    assert gated.in_zeros / gated.samples < 0.02

    silence = np.zeros(4096, dtype=np.int16)
    quiet = em_ns.StreamingDenoiser(sessions=_passthrough())
    quiet.process(silence.tobytes())
    assert quiet.in_zeros == quiet.samples
    assert "in=100.0%" in quiet.zero_report()

    assert em_ns.StreamingDenoiser(sessions=_passthrough()).zero_report() == "no samples"
