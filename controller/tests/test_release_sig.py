"""
Release signatures: once em_release_sig has a key, the controller installs a
GitHub release asset only if its signature verifies. A stolen GitHub login or
token can publish a release; it cannot make one the controller will install.
"""
import asyncio
import base64
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import em_firmware
import em_release_sig as sig
import em_releases

URL = "https://github.com/wilbowes/EchoMuse/releases/download/v9.9.9/server"
BIN = b"\x7fELF firmware"


def keypair():
    k = Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)
    return k, base64.b64encode(pub).decode()


def signed(k, tag="v9.9.9", name="server", data=BIN) -> bytes:
    return base64.b64encode(k.sign(sig.message(tag, name, data))) + b"\n"


KEY, PUB = keypair()


def test_nothing_is_enforced_until_a_key_ships():
    assert sig.KEYS == () or all(len(base64.b64decode(k)) == 32 for k in sig.KEYS)
    assert sig.problem(URL, BIN, None, keys=()) is None


def test_a_good_signature_verifies():
    assert sig.problem(URL, BIN, signed(KEY), keys=(PUB,)) is None


@pytest.mark.parametrize("why,data,s", [
    ("unsigned", BIN, None),
    ("tampered binary", BIN + b"x", signed(KEY)),
    ("signed for another tag", BIN, signed(KEY, tag="v1.0.0")),
    ("signed as another asset", BIN, signed(KEY, name="init")),
    ("another key", BIN, signed(keypair()[0])),
    ("garbage", BIN, b"not base64 !!\n"),
    ("oversized", BIN, b"A" * (sig.MAX_SIG_BYTES + 1)),
])
def test_anything_else_is_refused(why, data, s):
    assert sig.problem(URL, data, s, keys=(PUB,)), why


def test_a_spare_key_or_a_second_line_also_verifies():
    spare, spare_pub = keypair()
    two_lines = signed(keypair()[0]) + signed(spare)
    assert sig.problem(URL, BIN, two_lines, keys=(PUB, spare_pub)) is None


def test_a_url_that_is_not_a_release_asset_cannot_be_verified():
    assert sig.asset_of("https://example.com/server") is None
    assert sig.problem("https://example.com/server", BIN, signed(KEY), keys=(PUB,))
    assert sig.asset_of(URL) == ("v9.9.9", "server")


def test_the_signing_tool_signs_what_the_controller_verifies(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import sign_release
    assert sign_release.public_b64(KEY) == PUB
    assert set(sign_release.INSTALLABLE) == {"server", "init", "emos-payload.zip"}


# --- download(): the one path firmware and emOS inits come through ---------

@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.setattr(sig, "KEYS", (PUB,))
    files: dict[str, bytes] = {}
    hits: list[str] = []

    async def handler(request):
        hits.append(request.path)
        body = files.get(request.path)
        return web.Response(body=body) if body is not None else web.Response(status=404)

    def run(coro_fn):
        async def go():
            app = web.Application()
            app.router.add_get("/{tail:.*}", handler)
            async with TestServer(app) as server:
                base = str(server.make_url("/wilbowes/EchoMuse/releases/download/v9.9.9"))
                return await coro_fn(base)
        return asyncio.run(go())
    return files, hits, run


P = "/wilbowes/EchoMuse/releases/download/v9.9.9"


def test_an_unsigned_release_is_refused_and_says_why(served):
    files, _, run = served
    files[P + "/server"] = BIN
    url, got = run(lambda base: _dl(base + "/server"))
    assert got is None
    assert "not signed" in em_releases.why_failed(url, "fallback")
    assert em_firmware.read("v9.9.9") is None, "a refused binary must not be cached"


def test_a_signed_release_installs_and_its_signature_is_cached(served):
    files, hits, run = served
    files[P + "/server"] = BIN
    files[P + "/server.sig"] = signed(KEY)
    _, got = run(lambda base: _dl(base + "/server"))
    assert got == BIN
    assert em_firmware.read_sig("v9.9.9") == signed(KEY)  # Ed25519 is deterministic
    # Offline later: the cache hit verifies from the cached signature alone.
    files.clear(); hits.clear()
    _, again = run(lambda base: _dl(base + "/server"))
    assert again == BIN and hits == []


def test_a_cached_binary_is_still_checked(served):
    files, _, run = served
    em_firmware.write("v9.9.9", BIN + b"tampered")
    files[P + "/server.sig"] = signed(KEY)
    _, got = run(lambda base: _dl(base + "/server"))
    assert got is None


def test_without_a_key_nothing_changes(served, monkeypatch):
    files, hits, run = served
    monkeypatch.setattr(sig, "KEYS", ())
    files[P + "/server"] = BIN
    _, got = run(lambda base: _dl(base + "/server"))
    assert got == BIN and not any(h.endswith(".sig") for h in hits)


async def _dl(url):
    return url, await em_releases.download(url, "v9.9.9")
