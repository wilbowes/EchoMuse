"""
Deployment-shape guards — not logic tests. The controller Dockerfile
COPYs each module explicitly, so a new em_*.py that works fine on bare
metal crash-loops the container at import time if the COPY line is
forgotten (bitten by em_scenes.py 2026-07-10 and em_oww_models.py
2026-07-19).
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_every_controller_module():
    dockerfile = (CONTROLLER / "Dockerfile").read_text()
    copied = set(re.findall(r"^COPY\s+(\S+\.py)\s", dockerfile, re.M))
    modules = {p.name for p in CONTROLLER.glob("em_*.py")} | {"version.py"}
    missing = sorted(modules - copied)
    assert not missing, (
        f"Dockerfile is missing COPY lines for {missing} — the container "
        f"will crash-loop at import time"
    )


def test_dashboard_bundle_is_cache_busted():
    """
    /dashboard must not hand the browser a bare /static/dashboard.js URL.

    aiohttp's add_static sends Last-Modified and ETag but no Cache-Control, so
    browsers apply heuristic freshness and serve a stale bundle without
    revalidating. That failure is invisible server-side — deploy correct, file
    correct, compiled bundle correct, browser showing the previous UI — so it
    reads as "my change didn't work" and sends you hunting in the wrong place.
    Asserted at the source level because the alternative is starting an aiohttp
    app, which this suite deliberately does not do.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    handler = src[src.index("async def _serve_dashboard"):]
    handler = handler[:handler.index("\nasync def ", 1)]

    assert "dashboard.js?v=" in handler, \
        "the bundle URL must carry a cache-busting token"
    assert "no-cache" in handler, \
        "dashboard.html itself must be revalidated, or the new URL is never seen"
    # A version-string token would not change between two local "dev" builds;
    # mtime changes on every rebuild.
    assert "st_mtime" in handler, \
        "cache-bust on the bundle's mtime, not on a version string"


def test_dashboard_paths_are_ingress_safe():
    """Ingress is mounted below a generated path, so root URLs bypass it."""
    static = CONTROLLER / "static"
    index = (static / "index.html").read_text()
    dashboard = (static / "dashboard.html").read_text()
    jsx = (static / "dashboard.jsx").read_text()
    api = (CONTROLLER / "em_api.py").read_text()
    config = (CONTROLLER / "config.yaml").read_text()

    assert 'href="/static/' not in index
    assert "url('/static/" not in index
    assert 'href="/static/' not in dashboard
    assert 'src="/static/' not in dashboard
    assert "url('/static/" not in dashboard
    assert "function ingressPath(path)" in jsx
    assert "function ingressWebSocketUrl(path)" in jsx
    assert "document.baseURI" in jsx
    assert "fetch(ingressPath(path)" in jsx
    assert '"static/dashboard.js"' in api
    assert 'web.HTTPFound(".")' in api
    assert 'request.headers.get("X-Ingress-Path", "")' in api
    assert "ECHOMUSE_HOME_ASSISTANT_INGRESS" in config


def test_release_notes_survive_the_whole_relay():
    """
    Release notes have to make it through four places to be useful: captured
    from the GitHub response, persisted, re-read into the cache after a
    restart, and rendered. Miss any one and the dashboard shows a version
    number with no way to judge it — which is the state this replaced.

    The restart path is the one worth pinning: the in-memory cache is
    populated from the DB when cold, so notes omitted there would appear on
    first poll and silently vanish on every controller restart until the next
    one.
    """
    from pathlib import Path
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()

    fetch = api[api.index("async def _fetch_latest_release"):]
    fetch = fetch[:fetch.index("\nasync def ", 1)]
    assert 'release.get("body")' in fetch or '.get("body")' in fetch, \
        "the GitHub release body must be captured"
    assert 'set_config("latest_notes"' in fetch, "notes must be persisted"

    cached = api[api.index("    # Load from DB cache"):]
    cached = cached[:cached.index("\nasync def ", 1)] if "\nasync def " in cached else cached
    assert 'get_config("latest_notes"' in cached, \
        "the DB-cache path must restore notes, or they vanish on restart"

    jsx = (Path(__file__).resolve().parent.parent / "static" / "dashboard.jsx").read_text()
    assert "release.notes" in jsx, "the dashboard must render the notes"


def test_release_workflow_publishes_the_tag_annotation():
    """
    The notes shown in the dashboard come from the annotated tag, so the
    workflow must publish that rather than only GitHub's generated commit
    list. If this drifts, every future release silently shows a commit dump to
    whoever is deciding whether to update.
    """
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent.parent
          / ".github" / "workflows" / "release.yml").read_text()
    assert "body_path:" in wf, "the release must publish notes from a file"
    assert "%(contents)" in wf, "notes must come from the tag annotation"


def test_every_device_payload_has_an_update_path():
    """
    A payload installed only at provisioning drifts forever.

    This has now bitten twice: start_server.sh (Lounge was a revision behind
    Office, 2026-07-11) and the debloat pair (round 2 added a package and every
    fielded device needed a manual push, 2026-07-30). Both fixes were the same
    shape — an md5-compared sync riding the OTA — so this asserts that every
    file in device_payloads/ is named by a sync function, and fails when a
    fourth payload is added without one.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    api = (root / "em_api.py").read_text()
    payloads = sorted(p.name for p in (root / "device_payloads").iterdir() if p.is_file())
    assert payloads, "device_payloads/ is empty — has it moved?"

    for name in payloads:
        assert name in api, (
            f"{name} has no update path: nothing in em_api.py references it. "
            f"A payload installed only by the provisioning wizard drifts on every "
            f"device already in the field."
        )


def test_debloat_sync_reconciles_both_halves():
    """
    The debloat is a boot script AND a pm-hide list. Round 2 added a *package*,
    so a sync that only refreshed the script would have looked like it worked
    and changed nothing on any device.
    """
    from pathlib import Path
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    fn = api[api.index("async def _sync_debloat"):]
    fn = fn[:fn.index("\nasync def ", 1)] if "\nasync def " in fn[1:] else fn

    assert "echomuse-debloat.sh" in fn, "the boot script half must be synced"
    assert "_debloat_packages()" in fn, "the pm-hide half must be reconciled"
    assert "pm hide" in fn, "drifted packages must actually be hidden"
    # Rename-based replacement: the running shell keeps the old inode.
    assert "mv " in fn and ".new" in fn, \
        "the script must be replaced by rename, not written in place"
    # md5 both before (skip when in sync) and after (verify the transfer).
    assert fn.count("md5") >= 2, "sync must md5-compare and md5-verify"


def test_debloat_reachable_without_an_ota():
    """
    The OTA-time sync cannot reach a device already on the latest firmware —
    which is the exact case that exposed the gap. A manual trigger is required,
    not a nicety.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    api = (root / "em_api.py").read_text()
    assert '"/api/devices/{id}/debloat"' in api, "no manual debloat endpoint registered"
    jsx = (root / "static" / "dashboard.jsx").read_text()
    assert "/debloat`" in jsx, "the dashboard must be able to trigger it"


def test_stale_release_cache_is_not_returned_when_it_has_aged_out():
    """
    _get_cached_release used to fire a refresh into the background and return
    the STALE value. Two consequences, both seen on 2026-07-30: the dashboard
    reported "there's an update" only after someone pressed Check now, and an
    OTA pushed v2.9.9 while v2.9.10 was the current release.

    The refresh is now awaited when the DB cache has aged past the check
    interval, falling back to the stale value only if the fetch fails.
    """
    from pathlib import Path
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    fn = api[api.index("async def _get_cached_release"):]
    fn = fn[:fn.index("\nasync def ", 1)]

    assert "await _fetch_latest_release()" in fn, \
        "an aged-out cache must be refreshed synchronously, not in the background"
    assert "asyncio.create_task(_fetch_latest_release())" not in fn, \
        "fire-and-forget refresh returns the stale value to this caller"


def test_release_change_is_pushed_to_open_dashboards():
    """A tab already showing the Updates panel should not sit on the old
    version until someone reloads."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    api = (root / "em_api.py").read_text()
    assert '"type":         "release_update"' in api or '"release_update"' in api, \
        "a release change must be broadcast on the event stream"
    jsx = (root / "static" / "dashboard.jsx").read_text()
    # And the tab must keep asking, as a fallback for a missed event.
    upd = jsx[jsx.index("if (tab !== 'updates') return;"):]
    assert "setInterval" in upd[:600], \
        "the Updates tab must refresh while open, not fetch once on entry"
