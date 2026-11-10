"""
The firmware download cache.

`_fetch_binary` used to open a fresh HTTP session and pull the whole release
asset on every call, from both OTA paths — so a fleet update downloaded the
same ~10MB once per device, and the provisioning wizard again for every device
it set up. A published tag never changes what it points at, so that download is
worth doing once per release.

Two rules carry the risk, and neither is visible at the call site:

  * **md5 decides a hit, not the file existing.** A truncated download leaves a
    file of plausible size, and the OTA's device-side verification cannot catch
    it — that check confirms the device received what the controller SENT, so a
    corrupt entry verifies perfectly all the way onto the device. A corrupt
    binary and a genuinely broken one then produce the same observable: three
    fast exits and a rollback.
  * **A cache failure is never an update failure.** Every write path degrades
    to returning the bytes anyway. This is the opposite of the rule for the DB
    backup taken before a migration, and deliberately so: there, refusing is
    the safe action; here, refusing costs the user the thing they asked for and
    protects nothing.
"""

import os

import pytest

import em_firmware


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A data directory of the shape the controller actually has."""
    db = tmp_path / "echomuse.db"
    db.write_bytes(b"")
    monkeypatch.setenv("DB_PATH", str(db))
    return tmp_path / "firmware"


# ── where it lives ───────────────────────────────────────────────────────────


def test_cache_sits_beside_the_database(cache, tmp_path):
    """
    Same convention as recordings/, oww_models/ and tls/, so one persisted
    volume carries everything — and so the add-on's fixed /data covers it.
    """
    assert em_firmware.firmware_dir() == tmp_path / "firmware"


def test_a_tag_that_is_not_a_path_component_is_refused():
    """
    The tag arrives over the network from the GitHub API and is used as a
    filename. Validated rather than trusted.
    """
    for bad in ("../../etc/passwd", "v1/../..", "", "a" * 200, "v 1.0", "/abs"):
        assert em_firmware.safe_version(bad) is None, bad
    for good in ("v2.11.0", "v2.12.0-rc.1", "v1.0.0+build.2"):
        assert em_firmware.safe_version(good) == good


def test_an_unusable_tag_caches_nothing_rather_than_raising(cache):
    assert em_firmware.write("../evil", b"x" * 10) is False
    assert em_firmware.read("../evil") is None


# ── round trip ───────────────────────────────────────────────────────────────


def test_what_goes_in_comes_back_out(cache):
    data = os.urandom(4096)
    assert em_firmware.write("v2.11.0", data) is True
    assert em_firmware.read("v2.11.0") == data


def test_a_miss_is_just_none(cache):
    assert em_firmware.read("v9.9.9") is None


def test_nothing_is_cached_for_empty_bytes(cache):
    assert em_firmware.write("v2.11.0", b"") is False
    assert em_firmware.read("v2.11.0") is None


# ── md5 is the definition of a hit ───────────────────────────────────────────


def test_a_corrupted_entry_is_a_miss_not_a_bad_binary(cache):
    """
    The one that matters. Without this the truncated file is served, passes
    the device-side md5 (which only proves the device got what we sent), and
    the device crash-loops into a rollback with nothing naming the cause.
    """
    em_firmware.write("v2.11.0", b"A" * 4096)
    (cache / "server-v2.11.0").write_bytes(b"A" * 2048)   # truncated
    assert em_firmware.read("v2.11.0") is None


def test_a_corrupted_entry_is_removed_so_it_cannot_be_re_read(cache):
    em_firmware.write("v2.11.0", b"A" * 4096)
    (cache / "server-v2.11.0").write_bytes(b"A" * 2048)
    em_firmware.read("v2.11.0")
    assert not (cache / "server-v2.11.0").exists()
    assert not (cache / "server-v2.11.0.md5").exists()


def test_a_payload_with_no_digest_beside_it_is_a_miss(cache):
    """
    An interrupted write, or a file someone dropped in by hand. Trusting it
    would be trusting a filename.
    """
    em_firmware.write("v2.11.0", b"A" * 4096)
    (cache / "server-v2.11.0.md5").unlink()
    assert em_firmware.read("v2.11.0") is None


def test_an_interrupted_write_leaves_nothing_readable(cache):
    """
    The payload lands at .part and is renamed only after the digest exists,
    so a crash mid-write cannot produce an entry read() would trust.
    """
    (cache).mkdir(parents=True, exist_ok=True)
    (cache / "server-v2.11.0.part").write_bytes(b"A" * 4096)
    assert em_firmware.read("v2.11.0") is None


# ── a cache failure must never fail an update ────────────────────────────────


def test_an_unwritable_directory_reports_false_rather_than_raising(cache, tmp_path):
    """
    A full or read-only disk must not stop a device being updated — the caller
    goes on to use the bytes it already has in hand.
    """
    (tmp_path / "firmware").write_bytes(b"")   # a FILE where the dir should be
    assert em_firmware.write("v2.11.0", b"A" * 4096) is False


def test_an_unreadable_cache_is_a_miss_rather_than_an_error(cache, tmp_path):
    (tmp_path / "firmware").write_bytes(b"")
    assert em_firmware.read("v2.11.0") is None


# ── pruning ──────────────────────────────────────────────────────────────────


def test_pruning_keeps_the_most_recent(cache):
    for i, v in enumerate(("v1.0.0", "v2.0.0", "v3.0.0", "v4.0.0")):
        em_firmware.write(v, b"x" * 64)
        os.utime(cache / f"server-{v}", (1000 + i * 100, 1000 + i * 100))

    em_firmware.prune(keep=2)
    left = sorted(p.name for p in cache.glob("server-v*") if p.suffix != ".md5")
    assert left == ["server-v3.0.0", "server-v4.0.0"]


def test_a_read_counts_as_use(cache):
    """
    mtime is the recency signal, and the read path touches it — so the release
    a fleet is mid-rollout on cannot be pruned out from under it by a newer one
    arriving. No controller-side bookkeeping to lose across a restart.
    """
    em_firmware.write("v1.0.0", b"x" * 64)
    os.utime(cache / "server-v1.0.0", (1000, 1000))
    em_firmware.write("v2.0.0", b"y" * 64)
    os.utime(cache / "server-v2.0.0", (2000, 2000))

    em_firmware.read("v1.0.0")          # used, so newest again
    em_firmware.prune(keep=1)
    assert (cache / "server-v1.0.0").exists()
    assert not (cache / "server-v2.0.0").exists()


def test_pruning_deletes_only_what_this_module_wrote(cache):
    """
    The directory sits in the user's data volume beside their database. A
    cache is not a licence to delete things it does not recognise.
    """
    em_firmware.write("v1.0.0", b"x" * 64)
    stranger = cache / "please-do-not-delete.txt"
    stranger.write_text("mine")
    em_firmware.write("v2.0.0", b"y" * 64)
    em_firmware.write("v3.0.0", b"z" * 64)

    em_firmware.prune(keep=1)
    assert stranger.exists()


def test_a_digest_goes_with_the_payload_it_belongs_to(cache):
    em_firmware.write("v1.0.0", b"x" * 64)
    em_firmware.write("v2.0.0", b"y" * 64)
    os.utime(cache / "server-v1.0.0", (1000, 1000))
    em_firmware.prune(keep=1)
    assert not (cache / "server-v1.0.0.md5").exists(), (
        "an orphaned digest would make a later re-download look cached"
    )


def test_pruning_an_absent_directory_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "nope" / "echomuse.db"))
    assert em_firmware.prune() == []


def test_a_download_follows_a_redirect_with_a_long_header(cache):
    # github.com's redirect to the asset carries a 9,243-byte CSP header,
    # over aiohttp's default 8,190-byte field limit (#1021).
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    import em_releases

    async def redirect(request):
        raise web.HTTPFound("/asset", headers={"Content-Security-Policy": "x" * 9243})

    async def asset(request):
        return web.Response(body=b"server-binary")

    async def run():
        app = web.Application()
        app.router.add_get("/download", redirect)
        app.router.add_get("/asset", asset)
        async with TestServer(app) as server:
            return await em_releases.download(str(server.make_url("/download")), "v9.9.9")

    assert asyncio.run(run()) == b"server-binary"
    assert em_firmware.read("v9.9.9") == b"server-binary"
