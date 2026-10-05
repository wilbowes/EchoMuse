"""
WAV header parsing for TTS passthrough, tested from the format's edges rather
than from the one header ffmpeg happens to write: placeholder sizes on a
stream, chunks before data, RIFF's pad byte on odd sizes, the 16/18/40-byte
fmt variants (PCM, PCM with cbSize, WAVE_FORMAT_EXTENSIBLE), and bytes
arriving one at a time.
"""

import struct

import pytest

import em_wav as W


def fmt_chunk(af=1, ch=1, rate=48000, bits=16, extra=b""):
    body = struct.pack("<HHIIHH", af, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits) + extra
    return b"fmt " + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def chunk(cid, body):
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def wav(*chunks, data=b"\x01\x02\x03\x04", data_size=0xFFFFFFFF):
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"".join(chunks)
            + b"data" + struct.pack("<I", data_size) + data)


# The first 78 bytes ffmpeg writes streaming 48kHz mono S16 to a pipe: sizes
# are 0xFFFFFFFF and a LIST/INFO chunk sits between fmt and data.
FFMPEG_PIPE = (
    b"RIFF\xff\xff\xff\xffWAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80\xbb\x00\x00"
    b"\x00w\x01\x00\x02\x00\x10\x00LIST\x1a\x00\x00\x00INFOISFT\x0e\x00\x00\x00"
    b"Lavf58.76.100\x00data\xff\xff\xff\xff"
)


def test_ffmpegs_streamed_header():
    s = W.WavStream()
    assert s.push(FFMPEG_PIPE + b"\xe3\x00\xdf\x01") == b"\xe3\x00\xdf\x01"
    assert s.format == W.Format(1, 1, 48000, 16)
    assert W.is_wire_pcm(s.format)
    assert s.push(b"\xb7\x02") == b"\xb7\x02", "after the header, bytes pass straight through"


def test_one_byte_at_a_time():
    s = W.WavStream()
    blob = wav(fmt_chunk(), chunk(b"LIST", b"INFOx"), data=b"ABCD")
    out = b"".join(s.push(blob[i:i + 1]) for i in range(len(blob)))
    assert out == b"ABCD"


def test_odd_sized_chunk_is_padded():
    """RIFF pads a chunk of odd length with one byte that its size omits."""
    s = W.WavStream()
    assert s.push(wav(fmt_chunk(), chunk(b"junk", b"abc"), data=b"PCM!")) == b"PCM!"


@pytest.mark.parametrize("extra,af", [
    (b"", 1),                                   # 16-byte PCM
    (b"\x00\x00", 1),                           # 18 bytes, cbSize 0
    (b"\x16\x00" + b"\x10\x00" + b"\x04\x00\x00\x00" + b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71", 0xFFFE),
])
def test_fmt_variants(extra, af):
    s = W.WavStream()
    s.push(wav(fmt_chunk(af=af, extra=extra)))
    assert s.format.audio_format == af and W.is_wire_pcm(s.format)


@pytest.mark.parametrize("fmt", [
    W.Format(1, 2, 48000, 16),   # stereo
    W.Format(1, 1, 22050, 16),   # a provider rate HA did not convert
    W.Format(1, 1, 48000, 24),
    W.Format(3, 1, 48000, 32),   # float
    None,
])
def test_anything_else_goes_through_the_decoder(fmt):
    assert not W.is_wire_pcm(fmt)


def test_real_sizes_are_accepted_too():
    s = W.WavStream()
    assert s.push(wav(fmt_chunk(), data=b"\x00\x01", data_size=2)) == b"\x00\x01"


@pytest.mark.parametrize("blob", [b"fLaC\x00\x00\x00\x22" + b"\0" * 8, b"RIFF\0\0\0\0AVI LIST"])
def test_not_a_wav_is_refused(blob):
    with pytest.raises(W.NotWav):
        W.WavStream().push(blob)


def test_data_before_fmt_is_refused():
    with pytest.raises(W.NotWav):
        W.WavStream().push(b"RIFF\xff\xff\xff\xffWAVEdata\xff\xff\xff\xff\0\0")


def test_a_header_that_never_reaches_data_is_bounded():
    s = W.WavStream()
    s.push(b"RIFF\xff\xff\xff\xffWAVE" + fmt_chunk() + b"junk" + struct.pack("<I", 10 ** 6))
    with pytest.raises(W.NotWav):
        s.push(b"\0" * (W.MAX_HEADER + 1))


def test_short_input_waits():
    s = W.WavStream()
    assert s.push(b"RIFF") == b"" and s.format is None


def test_tts_is_requested_as_wire_wav():
    """Passthrough only happens if HA is asked for WAV at the wire rate; asking
    for FLAC again silently puts the decoder back in the path."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "em_esphome.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "dict"
             and any(k.arg == "format" for k in n.keywords)]
    assert calls, "supported_formats declaration not found"
    for c in calls:
        kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
        assert kw["format"] == "'wav'" and kw["num_channels"] == "1" and kw["sample_bytes"] == "2"
