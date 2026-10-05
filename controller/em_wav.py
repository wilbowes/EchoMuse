"""
Incremental WAV header parsing, so HA's TTS stream reaches the device with no
decoder in between.

HA transcodes TTS to the format our media player declares (supported_formats).
That was FLAC, copied from Voice PE, which decodes on the device to save its
link. Here it cost a decoder on the controller, and ffmpeg's FLAC decoder
holds audio back: ~1.7s with default frame threading on an 8-core host, still
~0.9s with one thread, measured feeding a FLAC at real time. Audio held inside
the decoder when HA pauses between sentences (streaming TTS behind an LLM)
reaches the device only when the NEXT sentence's bytes arrive — so a pause
HA makes becomes a longer gap the Echo plays as silence, mid-answer. WAV at the
wire format (48kHz mono S16LE) is the PCM itself behind a header: nothing to
hold back.

A streamed WAV cannot know its length, so the RIFF and data sizes are
placeholders (ffmpeg writes 0xFFFFFFFF on a pipe) and are ignored; the data
chunk runs to the end of the stream. Chunks before it (LIST, fact) are skipped,
with RIFF's pad byte on odd sizes.

Pure, so it is tested; em_esphome only feeds it bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    audio_format: int   # 1 = PCM; 0xFFFE = WAVE_FORMAT_EXTENSIBLE
    channels: int
    sample_rate: int
    bits: int


PCM = 1
EXTENSIBLE = 0xFFFE

# Give up on a header that has not reached its data chunk by here; a WAV from
# HA's proxy carries a few dozen bytes of metadata at most.
MAX_HEADER = 64 * 1024


class NotWav(ValueError):
    pass


class WavStream:
    """Feed bytes; once the header is parsed, `format` is set and push()
    returns PCM from the data chunk onward."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.format: Format | None = None
        self._in_data = False

    @property
    def ready(self) -> bool:
        """The header is parsed and push() now returns PCM."""
        return self._in_data

    def push(self, data: bytes) -> bytes:
        if self._in_data:
            return data
        self._buf += data
        pcm = self._parse()
        if pcm is None and len(self._buf) > MAX_HEADER:
            raise NotWav("no data chunk within the header limit")
        return pcm or b""

    def _parse(self) -> bytes | None:
        b = self._buf
        if len(b) < 12:
            return None
        if b[0:4] != b"RIFF" or b[8:12] != b"WAVE":
            raise NotWav("not a RIFF/WAVE stream")
        pos = 12
        while True:
            if len(b) < pos + 8:
                return None
            cid = bytes(b[pos:pos + 4])
            size = struct.unpack_from("<I", b, pos + 4)[0]
            body = pos + 8
            if cid == b"data":
                if self.format is None:
                    raise NotWav("data chunk before fmt")
                self._in_data = True
                pcm = bytes(b[body:])
                self._buf = bytearray()
                return pcm
            end = body + size + (size & 1)
            if len(b) < end:
                return None
            if cid == b"fmt ":
                if size < 16:
                    raise NotWav("short fmt chunk")
                af, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", b, body)
                self.format = Format(af, ch, rate, bits)
            pos = end


def is_wire_pcm(fmt: Format | None, rate: int = 48000) -> bool:
    """Whether the data chunk is already what the device plays: mono S16LE at
    the wire rate. Anything else goes through the decoder."""
    return (fmt is not None and fmt.audio_format in (PCM, EXTENSIBLE)
            and fmt.channels == 1 and fmt.sample_rate == rate and fmt.bits == 16)
