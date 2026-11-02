"""
#1090: the newest GA release must be found behind any number of newer EA
prereleases. With `per_page=10`, two EA releases pushed emos-v0.10 to 12th on
GitHub's list and every GA emOS install failed at Build emOS.

fetch_emos runs against a fake GitHub that honours `per_page`.
"""

import asyncio
import types
from urllib.parse import parse_qs, urlparse

import em_releases


def _rel(tag, prerelease, assets):
    return {"tag_name": tag, "prerelease": prerelease, "draft": False,
            "assets": [{"name": a, "browser_download_url": f"https://x/{tag}/{a}",
                        "size": 1} for a in assets]}


def _github(releases):
    """A stand-in for aiohttp that serves one page of `releases`."""
    class Resp:
        status = 200
        def __init__(self, url):
            self.per_page = int(parse_qs(urlparse(url).query).get("per_page", ["30"])[0])
        async def json(self):
            return releases[:self.per_page]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Session:
        def get(self, url, **kw): return Resp(url)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    return types.SimpleNamespace(ClientSession=Session,
                                 ClientTimeout=lambda **kw: None)


def test_ga_emos_release_found_behind_many_ea_releases(monkeypatch):
    # The list as it stood on 2026-11-02, then 40 more EA releases on top.
    releases = [_rel(f"v2.{30 + i}.0-ea.1", True, ["server"]) for i in range(40)]
    releases += [
        _rel("v2.19.0-ea.3", True, ["server"]),
        _rel("emos-v0.11-ea.2", True, ["init", "emos-payload.zip"]),
        _rel("v2.19.0-ea.2", True, ["server"]),
        _rel("emos-v0.11-ea.1", True, ["init", "emos-payload.zip"]),
        _rel("v2.19.0-ea.1", True, ["server"]),
        _rel("v2.18.1", False, ["server"]),
        _rel("v2.18.1-ea.1", True, ["server"]),
        _rel("v2.18.0", False, ["server"]),
        _rel("v2.18.0-ea.3", True, ["server"]),
        _rel("v2.18.0-ea.2", True, ["server"]),
        _rel("v2.18.0-ea.1", True, ["server"]),
        _rel("emos-v0.10", False, ["init", "emos-payload.zip"]),
    ]
    monkeypatch.setattr(em_releases, "aiohttp", _github(releases))
    monkeypatch.setattr(em_releases.db, "get_config", lambda k, d=None: d)
    monkeypatch.setattr(em_releases, "_offers_ea_firmware", lambda: False)
    monkeypatch.setattr(em_releases, "_remember_emos", lambda tag: None)
    got = asyncio.run(em_releases.fetch_emos())
    assert got is not None and got["version"] == "emos-v0.10"


def test_page_is_githubs_maximum():
    # Larger is refused by GitHub; smaller is how #1090 happened.
    query = parse_qs(urlparse(em_releases.GITHUB_API_URL).query)
    assert query["per_page"] == ["100"]
