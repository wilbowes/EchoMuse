"""
The wizard's boot image crosses in pieces (#800).

nginx refuses a request body over 1MB by default, and the ~9MB image went in
one POST: #769 and #1001 both stopped at a 413 the controller never saw. The
store's rules are what keep a reassembled image byte-identical to the escrow,
and the md5 the build checks is over that reassembly.
"""

import asyncio
import hashlib

import pytest

import em_chunked
from em_chunked import CHUNK_MAX, ChunkError, ChunkStore

ID = "0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _send(store, data, upload_id=ID):
    for off in range(0, len(data), CHUNK_MAX):
        store.put(upload_id, off, len(data), data[off:off + CHUNK_MAX])


def test_pieces_reassemble_byte_for_byte():
    data = bytes(range(256)) * 9000  # 2.2MB, last piece short
    s = ChunkStore()
    _send(s, data)
    assert s.take(ID) == data


def test_an_upload_is_taken_once():
    s = ChunkStore()
    _send(s, b"x" * 10)
    assert s.take(ID) == b"x" * 10
    assert s.take(ID) is None


def test_an_incomplete_upload_is_not_handed_over():
    s = ChunkStore()
    s.put(ID, 0, 20, b"x" * 10)
    assert s.take(ID) is None


def test_a_retried_piece_is_accepted_when_it_matches():
    # A response lost on the network: the wizard sends the same piece again.
    s = ChunkStore()
    s.put(ID, 0, 20, b"a" * 10)
    assert s.put(ID, 0, 20, b"a" * 10) == 10
    s.put(ID, 10, 20, b"b" * 10)
    assert s.take(ID) == b"a" * 10 + b"b" * 10


@pytest.mark.parametrize("offset, data, why", [
    (0, b"z" * 10, "a retry whose bytes differ"),
    (15, b"b" * 5, "a gap"),
])
def test_a_piece_that_does_not_follow_is_refused(offset, data, why):
    s = ChunkStore()
    s.put(ID, 0, 20, b"a" * 10)
    with pytest.raises(ChunkError):
        s.put(ID, offset, 20, data)


def test_an_upload_must_start_at_zero():
    with pytest.raises(ChunkError):
        ChunkStore().put(ID, 10, 20, b"x" * 10)


@pytest.mark.parametrize("total, data", [
    (0, b""),
    (em_chunked.UPLOAD_MAX + 1, b"x"),
    (CHUNK_MAX + 1, b"x" * (CHUNK_MAX + 1)),
    (5, b"x" * 6),
])
def test_sizes_outside_the_limits_are_refused(total, data):
    with pytest.raises(ChunkError):
        ChunkStore().put(ID, 0, total, data)


def test_the_total_cannot_change_mid_upload():
    s = ChunkStore()
    s.put(ID, 0, 20, b"x" * 10)
    with pytest.raises(ChunkError):
        s.put(ID, 10, 30, b"x" * 10)


@pytest.mark.parametrize("bad", ["", "short", "../../etc", "A" * 32, "0" * 65])
def test_ids_are_hex_and_dashes(bad):
    with pytest.raises(ChunkError):
        ChunkStore().put(bad, 0, 1, b"x")


def test_an_unclaimed_upload_expires():
    clock = Clock()
    s = ChunkStore(now=clock)
    _send(s, b"x" * 10)
    clock.t += em_chunked.TTL_S + 1
    assert s.take(ID) is None


def test_the_store_holds_a_bounded_number_of_uploads():
    clock = Clock()
    s = ChunkStore(now=clock)
    ids = [f"{i:08x}" for i in range(em_chunked.MAX_UPLOADS + 1)]
    for i in ids:
        clock.t += 1
        _send(s, b"x", i)
    assert s.take(ids[0]) is None  # the oldest went
    assert all(s.take(i) == b"x" for i in ids[1:])


def test_the_build_takes_what_the_chunks_carried(monkeypatch):
    """End to end through both routes: chunks in, the build sees the bytes."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import em_routes_provision as rp

    monkeypatch.setattr(rp, "_chunks", ChunkStore())
    seen = {}

    def fake_build(reference, init_bin, version, build_id, sbin, system_part, board=None):
        seen.update(reference=reference, init=init_bin)
        return {"image": b"IMG", "md5": "m", "sha256": "s", "reference_md5": "r",
                "size": 3, "reference_size": len(reference)}

    monkeypatch.setattr(rp.em_emos_build, "build_emos_image", fake_build)
    reference = bytes(range(256)) * 5000  # 1.28MB: three pieces
    init = b"\x7fELF" + b"i" * 700_000

    async def upload(c, data, upload_id):
        for off in range(0, len(data), CHUNK_MAX):
            r = await c.post(f"/chunk?id={upload_id}&offset={off}&total={len(data)}",
                             data=data[off:off + CHUNK_MAX])
            assert r.status == 200, await r.text()

    async def run():
        app = web.Application()
        app.router.add_post("/chunk", rp._post_provision_upload_chunk.__wrapped__)
        app.router.add_post("/build", rp._post_provision_emos_image.__wrapped__)
        async with TestClient(TestServer(app)) as c:
            too_big = await c.post(f"/chunk?id={ID}&offset=0&total={2 * CHUNK_MAX}",
                                   data=b"x" * (CHUNK_MAX + 1))
            await upload(c, reference, "aaaaaaaa")
            await upload(c, init, "bbbbbbbb")
            # A browser's FormData is always multipart; aiohttp's only is
            # once a field has a content type.
            form = aiohttp.FormData()
            form.add_field("reference_upload", "aaaaaaaa", content_type="text/plain")
            form.add_field("reference_md5", hashlib.md5(reference).hexdigest())
            form.add_field("init_upload", "bbbbbbbb")
            built = await c.post("/build", data=form)
            again = aiohttp.FormData()
            again.add_field("reference_upload", "aaaaaaaa", content_type="text/plain")
            again.add_field("init_upload", "bbbbbbbb")
            reused = await c.post("/build", data=again)
            return too_big.status, built.status, await built.read(), reused.status

    too_big, status, body, reused = asyncio.run(run())
    assert too_big == 413
    assert status == 200 and body == b"IMG"
    assert seen == {"reference": reference, "init": init}
    assert reused == 400  # taken once; a second build cannot reuse it
