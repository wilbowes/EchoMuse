"""
Which firmware, controller and emOS releases exist, read from GitHub.

Three feeds from one repo, selected on opposite things (see "Versioning /
releases" in CLAUDE.md): device firmware is the newest `v*` release with a
`server` asset; controller releases are annotated `controller-v*` TAGS with
no Release behind them; emOS is the newest `emos-v*` release with an `init`
asset. Each has its own cache, persisted in system_config so a restart does
not blank the Updates tab, and poll_loop refreshes all three on the interval
the dashboard sets (0 = never, #159).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

import em_db as db
import em_dbwriter
import em_events
import em_emos_update
import em_firmware
from version import VERSION as CONTROLLER_VERSION
from version import compare as _compare_versions
from version import parse as _parse_version
from version import choose_firmware_release as _choose_firmware_release
from version import offers_ea_firmware as _offers_ea_firmware

log = logging.getLogger("echomuse.api")

# List endpoint, not /releases/latest: device firmware releases (v* tags with
# a `server` asset) share the repo with controller releases (controller-v*
# tags, GHCR image only). /releases/latest returns whichever was published
# most recently — fetch_firmware filters the list for the newest
# release that is actually a device firmware release.
GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases?per_page=10"

# How long to cache GitHub release info in memory (seconds).
# DB is the persistent cache; this avoids hitting the DB on every
# /api/releases/latest request.
_release_cache: dict = {}
_release_cache_ts: float = 0.0
RELEASE_CACHE_TTL = 60  # seconds

# github.com answers an asset download with a 302 whose Content-Security-Policy
# header is 9,243 bytes (measured 2026-10-24), and aiohttp refuses any header
# over 8,190 by default, so every download failed before the redirect (#1021).
DOWNLOAD_MAX_FIELD_SIZE = 64 * 1024

# Controller releases are `controller-v*` TAGS with no GitHub Release behind
# them — controller-release.yml publishes a GHCR image and nothing else (see
# "Versioning / releases" in CLAUDE.md). So the notes come from the tag's own
# annotation: matching-refs lists the tags, and an annotated tag's object
# carries the message.
#
# Deliberately NOT solved by publishing GitHub Releases for controller tags.
# The releases list is the DEVICE firmware's update feed and
# fetch_firmware scans it for the newest v* tag carrying a `server`
# asset; adding controller rows puts non-firmware entries in front of that
# scan for no gain, when the annotation we already write says the same thing.
GITHUB_TAGS_URL = (
    "https://api.github.com/repos/{repo}/git/matching-refs/tags/controller-v"
)
GITHUB_TAG_OBJECT_URL = "https://api.github.com/repos/{repo}/git/tags/{sha}"

_controller_cache: dict = {}
_controller_cache_ts: float = 0.0

# The newest emOS release's tag, as last seen. `emos_latest_version` falls
# back to the stored copy, so a restart does not blank the Updates tab.
_emos_latest: str = ""


def firmware_version() -> str | None:
    """The newest firmware release last fetched, with no network call."""
    return _release_cache.get("version")


def controller_update() -> str | None:
    """The controller version on offer when it is newer than this one, with
    no network call."""
    return _controller_cache.get("version") if _controller_cache.get("available") else None


def check_interval() -> int:
    """
    The parsed update_check_interval; <= 0 means DISABLED (#159).

    db.get_config returns a STRING, so a stored "0" is truthy and survived
    the old `or 3600` fallback into asyncio.sleep(0) — turning the obvious
    way to stop the controller contacting GitHub (#158 documents this poll
    as its only outbound connection) into a busy loop against api.github.com
    until it rate-limits. The worst available outcome for exactly the reader
    who set it carefully. A non-numeric value used to raise out of the poll
    loop and kill the task outright; it now falls back to the default with
    a line in the log.
    """
    raw = (db.get_config("update_check_interval", "3600") or "3600").strip()
    try:
        return int(raw)
    except ValueError:
        log.warning(f"[api] update_check_interval {raw!r} is not a number — using 3600")
        return 3600

async def cached_firmware() -> Optional[dict]:
    """
    Return the latest release info, using the in-memory cache if fresh.
    Falls back to the DB cache if the in-memory cache is cold.
    Triggers a background fetch if the DB cache is stale.
    """
    global _release_cache, _release_cache_ts

    # In-memory cache hit
    if _release_cache and (time.monotonic() - _release_cache_ts) < RELEASE_CACHE_TTL:
        return _release_cache

    # Load from DB cache
    stored = await asyncio.get_running_loop().run_in_executor(None, lambda: {
        key: db.get_config(key) for key in (
            "latest_version", "latest_binary_url", "last_update_check",
            "latest_notes", "latest_release_url", "latest_published_at")})
    version = stored["latest_version"]
    url     = stored["latest_binary_url"]
    last_check = stored["last_update_check"]

    if version and url:
        _release_cache = {
            "version":      version,
            "url":          url,
            "notes":        stored["latest_notes"] or "",
            "release_url":  stored["latest_release_url"] or "",
            "published_at": stored["latest_published_at"] or "",
        }
        _release_cache_ts = time.monotonic()

        # If the DB cache has aged out, AWAIT the refresh rather than firing it
        # into the background and returning the stale value.
        #
        # Returning stale here is why "there's an update" showed up
        # inconsistently and why an OTA could push the previous release: the
        # caller — dashboard or update endpoint — got the old version and the
        # fresh one only landed in the cache afterwards, for whoever asked
        # next. It cost one wrong OTA (v2.9.9 pushed while v2.9.10 was
        # current, 2026-07-30).
        #
        # The cost is a single GitHub round trip, bounded by the 10s timeout in
        # fetch_firmware, and only on the first request after the
        # interval lapses — poll_loop normally refreshes ahead of any
        # caller. A failed refresh falls through to the stale cache, which is
        # better than no answer.
        # 0 (any non-positive value) means updates are DISABLED (#159):
        # serve the cache whatever its age and make no outbound call. The
        # poll loop reads this the same way, so the knob does what the
        # privacy section implies it does.
        interval = check_interval()
        if (interval > 0
                and (not last_check
                     or (time.time() - float(last_check)) > interval)):
            fresh = await fetch_firmware()
            if fresh:
                return fresh

        return _release_cache

    # No cache at all. The disabled check has to be repeated HERE and not
    # only on the branch above (#159): that one guards a controller which
    # has successfully polled at least once, and this is the branch a fresh
    # install takes — which is exactly the install belonging to someone who
    # set the interval to 0 before the first poll ever ran. Gating only the
    # refresh left them an outbound call on every dashboard visit that reads
    # releases, forever, because a disabled poll loop never populates the
    # cache that would have stopped it.
    #
    # None is the honest answer, and callers already handle it — this
    # function returns None on any fetch failure. The dashboard shows no
    # release information, which is what "I turned update checks off" should
    # look like. "Check now" is unaffected: it calls fetch_firmware
    # directly, and a button press is a deliberate request rather than
    # background traffic.
    if check_interval() <= 0:
        return None

    return await fetch_firmware()


async def fetch_firmware(force: bool = False) -> Optional[dict]:
    """
    Poll the GitHub releases API and update the DB cache.

    Returns the release dict or None on failure.
    """
    global _release_cache, _release_cache_ts

    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(
        None, db.get_config, "github_repo", "wilbowes/EchoMuse")
    url  = GITHUB_API_URL.format(repo=repo)

    log.info(f"[api] Polling GitHub releases: {url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[api] GitHub API returned {resp.status}")
                    return None
                releases = await resp.json()

        # Newest device firmware release this controller may offer: a GA
        # controller only GA (vX.Y.Z), an EA or dev controller EA
        # (vX.Y.Z-ea.N, published as a prerelease) too. Controller releases
        # use controller-v* and ship no binary.
        release = _choose_firmware_release(releases, _offers_ea_firmware())
        if release is None:
            log.warning("[api] No device firmware release with a 'server' asset found")
            return None
        tag = release["tag_name"]
        binary = next(a for a in release["assets"] if a.get("name") == "server")

        download_url = binary["browser_download_url"]

        # Release notes, so the dashboard can show WHAT an update changes
        # rather than only that one exists. Deciding whether to push firmware
        # to a device you rely on, from a version number alone, is not a
        # decision — it is a guess. The body comes from the annotated tag (see
        # .github/workflows/release.yml), which is why tags are annotated.
        notes = (release.get("body") or "").strip()

        def _persist() -> str | None:
            previous = db.get_config("latest_version")
            db.set_config("latest_version",    tag)
            db.set_config("latest_binary_url", download_url)
            db.set_config("latest_notes",      notes)
            db.set_config("latest_release_url", release.get("html_url") or "")
            db.set_config("latest_published_at", release.get("published_at") or "")
            db.set_config("last_update_check", str(time.time()))
            return previous

        # Persist to DB
        previous_tag = await loop.run_in_executor(None, _persist)

        # Update in-memory cache
        _release_cache    = {
            "version":      tag,
            "url":          download_url,
            "notes":        notes,
            "release_url":  release.get("html_url") or "",
            "published_at": release.get("published_at") or "",
        }
        _release_cache_ts = time.monotonic()

        log.info(f"[api] Latest release: {tag}")
        if tag != previous_tag:
            # Tell any open dashboard, so a tab that is already showing the
            # Updates panel does not sit on the old version until someone
            # reloads or presses Check now.
            log.info(f"[api] Release changed {previous_tag or '(none)'} -> {tag}")
            await em_events.push({
                "type":         "release_update",
                "version":      tag,
                "notes":        notes,
                "release_url":  release.get("html_url") or "",
                "published_at": release.get("published_at") or "",
            })
        return _release_cache

    except Exception as e:
        log.error(f"[api] GitHub release fetch failed: {e}")
        return None


async def fetch_controller(force: bool = False) -> Optional[dict]:
    """
    Find the newest controller-v* tag and its annotation.

    Two requests, not one per tag: matching-refs returns every controller-v*
    ref, the newest is picked by parsed version (NOT by list order — the API
    sorts refs lexically, which puts v2.9.0 after v2.10.0), and only that one
    tag's object is dereferenced for its message.

    A lightweight tag has no annotation and no message; that is a degraded
    release, not a broken one, so it still reports the version with empty
    notes.
    """
    global _controller_cache, _controller_cache_ts

    if (not force and _controller_cache
            and (time.monotonic() - _controller_cache_ts) < RELEASE_CACHE_TTL):
        return _controller_cache

    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(
        None, db.get_config, "github_repo", "wilbowes/EchoMuse")
    headers = {"Accept": "application/vnd.github+json"}
    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                GITHUB_TAGS_URL.format(repo=repo), headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[api] Controller tag list returned {resp.status}")
                    return _controller_cache or None
                refs = await resp.json()

            newest = None
            for ref in refs or []:
                tag = (ref.get("ref") or "").removeprefix("refs/tags/")
                parsed = _parse_version(tag)
                if parsed is None:
                    continue
                if newest is None or parsed > newest[0]:
                    newest = (parsed, tag, ref.get("object") or {})

            if newest is None:
                log.info("[api] No controller-v* tags published yet")
                return None

            _, tag, obj = newest
            notes = ""
            published_at = ""
            if obj.get("type") == "tag" and obj.get("sha"):
                async with session.get(
                    GITHUB_TAG_OBJECT_URL.format(repo=repo, sha=obj["sha"]),
                    headers=headers, timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        tag_obj = await resp.json()
                        notes = (tag_obj.get("message") or "").strip()
                        published_at = (tag_obj.get("tagger") or {}).get("date", "")

        version = tag.removeprefix("controller-")
        def _persist() -> str | None:
            before = db.get_config("latest_controller_version")
            db.set_config("latest_controller_version", version)
            db.set_config("latest_controller_notes", notes)
            db.set_config("latest_controller_published_at", published_at)
            return before

        previous = await loop.run_in_executor(None, _persist)

        _controller_cache = {
            "version":      version,
            "current":      CONTROLLER_VERSION,
            "notes":        notes,
            "published_at": published_at,
            "release_url":  f"https://github.com/{repo}/releases/tag/{tag}",
            **_compare_versions(CONTROLLER_VERSION, version),
        }
        _controller_cache_ts = time.monotonic()

        log.info(f"[api] Latest controller release: {version} "
                 f"(running {CONTROLLER_VERSION}, {_controller_cache['status']})")
        if version != previous:
            # Same live-push as device releases: a dashboard left open should
            # not sit on stale information until someone reloads.
            await em_events.push({
                "type":         "controller_update",
                "version":      version,
                "notes":        notes,
                "published_at": published_at,
                **_compare_versions(CONTROLLER_VERSION, version),
            })
        return _controller_cache

    except Exception as e:
        log.error(f"[api] Controller release fetch failed: {e}")
        return _controller_cache or None


def _remember_emos(tag: str) -> None:
    global _emos_latest
    if tag and tag != _emos_latest:
        _emos_latest = tag
        em_dbwriter.submit(db.set_config, "latest_emos_version", tag)


def emos_latest_version() -> str:
    """The newest emOS release last seen, with no network call."""
    return _emos_latest or db.get_config("latest_emos_version", "") or ""


async def fetch_emos() -> Optional[dict]:
    """
    The newest emOS release this controller may install (em_emos_update.
    choose_release): EA ones only on a controller that takes EA firmware.

    Separate from `fetch_firmware` rather than a parameter on it,
    because the two select on opposite things and share no cache: firmware is
    `v*` + a `server` asset, emOS is `emos-v*` + an `init` asset. Folding them
    together would mean one cache holding whichever kind was asked for last.

    Note the tag namespaces make this safe in both directions — `emos-v0.1`
    does not `startswith("v")`, so the firmware poll can never select an emOS
    release, and this one cannot select a firmware release.
    """
    repo = await asyncio.get_running_loop().run_in_executor(
        None, db.get_config, "github_repo", "wilbowes/EchoMuse")
    url = GITHUB_API_URL.format(repo=repo)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[api] GitHub API returned {resp.status} for emOS releases")
                    return None
                releases = await resp.json()
    except Exception as e:
        log.warning(f"[api] Could not poll GitHub for emOS releases: {e}")
        return None

    # EA emOS (emos-vX.Y-ea.N) only for controllers that take EA firmware.
    data = em_emos_update.choose_release(releases, _offers_ea_firmware())
    if data is None:
        return None
    tag = data["tag_name"]
    assets = {a.get("name"): a for a in data.get("assets", [])}
    # Every asset by name. There are two inits — one per kernel architecture —
    # so picking one here would be picking for the caller.
    _remember_emos(tag)
    return {
        "version": tag,
        "assets": {n: {"url": a["browser_download_url"],
                       "size": a.get("size", 0)}
                   for n, a in assets.items() if n},
    }


async def poll_loop() -> None:
    """
    Periodically poll GitHub for new releases.

    Runs as an asyncio task started from em_controller.main().
    Interval is read from system_config each iteration so it can be
    changed at runtime without restart.
    """
    # Initial delay — let the controller finish starting up
    await asyncio.sleep(30)

    while True:
        interval = check_interval()
        if interval <= 0:
            # Disabled (#159): no fetch, no outbound connection. Re-read
            # after a fixed park so re-enabling from the dashboard takes
            # effect without a restart.
            await asyncio.sleep(60)
            continue

        try:
            await fetch_firmware()
        except Exception as e:
            log.error(f"[api] Release poll loop error: {e}")

        # Same cadence, separate failure domain: a GitHub hiccup on one must
        # not cost the other its poll.
        try:
            await fetch_controller(force=True)
        except Exception as e:
            log.error(f"[api] Controller release poll error: {e}")

        try:
            await fetch_emos()
        except Exception as e:
            log.error(f"[api] emOS release poll error: {e}")

        # Floor of 1s so even an absurd tiny positive interval cannot spin
        # the loop faster than the event loop allows.
        await asyncio.sleep(max(interval, 1))


async def download(download_url: str,
                        version: str = "") -> Optional[bytes]:
    """
    The release binary, from disk if we already have it.

    A published tag never changes what it points at, so the download is worth
    doing once per release rather than once per device — a fleet update used to
    pull the same ~10MB for every Dot, and the provisioning wizard again for
    every device it set up.

    `version` is optional so a caller with only a URL still works; it simply
    does not get the cache. Nothing here can fail an update: a cache miss, an
    unwritable directory or a corrupt entry all end in an ordinary download.
    """
    if version:
        cached = await asyncio.get_event_loop().run_in_executor(
            None, em_firmware.read, version
        )
        if cached is not None:
            return cached

    log.info(f"[api] Fetching binary: {download_url}")
    try:
        async with aiohttp.ClientSession(
            max_field_size=DOWNLOAD_MAX_FIELD_SIZE,
        ) as session:
            async with session.get(
                download_url,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    log.error(f"[api] Binary download failed: HTTP {resp.status}")
                    return None
                data = await resp.read()
    except Exception as e:
        log.error(f"[api] Binary download exception: {e}")
        return None

    if version and data:
        # Off the event loop: hashing and writing 10MB blocks it for long
        # enough to delay speaker frames, and this runs during an OTA.
        await asyncio.get_event_loop().run_in_executor(
            None, em_firmware.write, version, data
        )
    return data
