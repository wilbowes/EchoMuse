"""
em_chunked.py — uploads sent in pieces small enough for any proxy (#800).

The wizard's boot image is ~9MB, and whatever sits in front of Home Assistant
decides whether one request that size arrives: nginx refuses anything over
1MB by default (#769, #1001), with a 413 the controller never sees. So the
wizard sends it in CHUNK_MAX pieces and the build request names the upload.

Held in memory only, for the reason the build route keeps nothing: it is the
user's own boot partition. An upload is taken once, by the request that uses
it, and anything left unclaimed is dropped after TTL_S.

Pure, so the rules are tested without aiohttp.
"""

from __future__ import annotations

import re
import time

# Under nginx's 1MB default with room for headers.
CHUNK_MAX = 512 * 1024
# A whole 16MB boot partition plus margin; the wizard sends less.
UPLOAD_MAX = 24 * 1024 * 1024
TTL_S = 600
MAX_UPLOADS = 4

_ID = re.compile(r"^[0-9a-f-]{8,64}$")


class ChunkError(ValueError):
    """A chunk refused, with a message for the person running the wizard."""


class ChunkStore:
    def __init__(self, now=time.monotonic):
        self._now = now
        self._uploads: dict[str, tuple[int, bytearray, float]] = {}

    def put(self, upload_id: str, offset: int, total: int, data: bytes) -> int:
        """Add one chunk; returns the bytes held so far.

        Chunks arrive in order. A chunk at an offset already held is a retry
        after a lost response, accepted when its bytes match what is there.
        """
        if not _ID.match(upload_id or ""):
            raise ChunkError("upload id must be 8-64 characters of hex and dashes")
        if not 0 < total <= UPLOAD_MAX:
            raise ChunkError(f"an upload must be 1 to {UPLOAD_MAX} bytes, not {total}")
        if len(data) > CHUNK_MAX:
            raise ChunkError(f"a chunk may be at most {CHUNK_MAX} bytes, not {len(data)}")
        self._prune()
        held = self._uploads.get(upload_id)
        if held is None:
            if offset != 0:
                raise ChunkError("this upload has expired or never started; start again")
            while len(self._uploads) >= MAX_UPLOADS:
                del self._uploads[min(self._uploads, key=lambda k: self._uploads[k][2])]
            held = (total, bytearray(), self._now())
            self._uploads[upload_id] = held
        want, buf, started = held
        if total != want:
            raise ChunkError(f"total changed from {want} to {total} mid-upload")
        if offset + len(data) > total:
            raise ChunkError("chunk runs past the declared total")
        if offset == len(buf):
            buf += data
        elif offset + len(data) > len(buf) or buf[offset:offset + len(data)] != data:
            raise ChunkError(f"chunk at {offset} does not follow the {len(buf)} bytes held")
        return len(buf)

    def take(self, upload_id: str) -> bytes | None:
        """The whole upload, removed from the store; None if absent or incomplete."""
        self._prune()
        held = self._uploads.get(upload_id)
        if held is None or len(held[1]) != held[0]:
            return None
        del self._uploads[upload_id]
        return bytes(held[1])

    def _prune(self) -> None:
        cutoff = self._now() - TTL_S
        for k in [k for k, v in self._uploads.items() if v[2] < cutoff]:
            del self._uploads[k]
