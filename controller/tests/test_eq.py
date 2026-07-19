import math

import numpy as np

import em_eq

RATE = 48000


def _sine(freq: float, seconds: float = 0.5, amp: float = 0.25) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    pcm = (np.sin(2 * math.pi * freq * t) * amp * 32767).astype(np.int16)
    return pcm.tobytes()


def _rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def test_flat_bands_are_transparent():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0] * 8)
    assert len(out) == len(pcm)
    # 0 dB everywhere should be within a fraction of a dB of identity.
    ratio = _rms(out) / _rms(pcm)
    assert 0.97 < ratio < 1.03


def test_none_bands_default_to_flat():
    pcm = _sine(1000)
    assert abs(_rms(em_eq.apply(pcm, RATE)) - _rms(pcm)) / _rms(pcm) < 0.03


def test_band_boost_raises_its_own_frequency_only():
    # 60 Hz sits below the 125 Hz shelf corner (full +6 dB); at the corner
    # itself a shelf only delivers half its gain.
    low = _sine(60)
    high = _sine(8000)
    bands = [6.0, 0, 0, 0, 0, 0, 0, 0]  # +6 dB low shelf
    low_gain = _rms(em_eq.apply(low, RATE, bands=bands)) / _rms(low)
    high_gain = _rms(em_eq.apply(high, RATE, bands=bands)) / _rms(high)
    assert low_gain > 1.7          # ~+6 dB ≈ ×2
    assert 0.9 < high_gain < 1.1   # shelf must not leak into the top band


def test_cut_reduces_level():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0, 0, 0, -12.0, 0, 0, 0, 0])
    assert _rms(out) / _rms(pcm) < 0.5


def test_short_and_empty_input_pass_through():
    assert em_eq.apply(b"", RATE) == b""
    assert em_eq.apply(b"\x01", RATE) == b"\x01"


def test_wrong_band_count_still_returns_audio():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0, 0.0])  # padded internally
    assert len(out) == len(pcm)


def test_streaming_eq_matches_batch_apply():
    pcm = _sine(1000, seconds=0.4)
    bands = [3.0, 0, -2.0, 0, 0, 4.0, 0, 1.0]
    want = em_eq.apply(pcm, RATE, bands=bands)
    eq = em_eq.StreamingEQ(RATE, bands=bands)
    out = b""
    for i in range(0, len(pcm), 4096):
        out += eq.process(pcm[i:i + 4096])
    # Filter state carries across chunks — output must match the
    # whole-buffer path (same filters, same float32 pipeline).
    got = np.frombuffer(out, dtype=np.int16).astype(np.int32)
    ref = np.frombuffer(want, dtype=np.int16).astype(np.int32)
    assert np.abs(got - ref).max() <= 1  # ±1 LSB float rounding


def test_streaming_eq_flat_is_passthrough():
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8)
    chunk = _sine(500, seconds=0.05)
    assert eq.process(chunk) == chunk
