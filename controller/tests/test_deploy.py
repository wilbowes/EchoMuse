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
    """
    As a Home Assistant add-on, the dashboard is mounted below a generated
    path (Ingress), not at the site root — an absolute "/static/..." or
    "/api/..." reference bypasses that path straight to the root and 404s.
    Every asset/API reference must therefore be relative, resolved against
    the <base href> em_api.py injects for the add-on case.

    Source-level because starting an aiohttp app + browser is outside what
    this suite does elsewhere (see test_dashboard_bundle_is_cache_busted).
    """
    static = CONTROLLER / "static"
    index = (static / "index.html").read_text()
    dashboard = (static / "dashboard.html").read_text()
    jsx = (static / "dashboard.jsx").read_text()
    api = (CONTROLLER / "em_api.py").read_text()
    config = (CONTROLLER / "config.yaml").read_text()

    for name, html in (("index.html", index), ("dashboard.html", dashboard)):
        assert 'href="/static/' not in html, f"{name} has an absolute /static href"
        assert 'src="/static/' not in html, f"{name} has an absolute /static src"
        assert "url('/static/" not in html, f"{name} has an absolute /static url()"

    assert "'/api/" not in index, \
        "index.html must fetch relative api/... paths, not /api/..."
    assert "location.replace('/dashboard')" not in index, \
        "index.html must redirect to a relative dashboard path"

    assert "function ingressPath(path)" in jsx, \
        "dashboard.jsx must relativize absolute paths for ingress"
    assert "function ingressWebSocketUrl(path)" in jsx, \
        "dashboard.jsx must build ingress-relative WebSocket URLs"
    assert "document.baseURI" in jsx, \
        "the WebSocket URL must resolve against the injected <base href>"
    assert "fetch(ingressPath(path)" in jsx, \
        "the shared API helpers must route through ingressPath"
    assert "location.replace('/')" not in jsx, \
        "an absolute root redirect bypasses the ingress path"

    assert '"static/dashboard.js"' in api, \
        "the cache-bust replace must target the relative asset URL"
    assert 'web.HTTPFound(".")' in api, \
        "/setup must redirect relatively, or it bounces out of the ingress path"
    assert "_with_ingress_base" in api, \
        "index.html and dashboard.html must get a <base href> injected"
    assert 'request.headers.get("X-Ingress-Path"' in api, \
        "the base path must come from Home Assistant's ingress header"
    assert "ECHOMUSE_HOME_ASSISTANT_INGRESS" in config, \
        "config.yaml must set the env var that gates ingress-only mode"


def test_addon_default_threshold_matches_the_controller():
    """
    A fresh add-on install is the ONLY case where a shipped default is
    visible — an existing deployment stores every key already, so
    DEFAULT_DEVICE_CONFIG never gets consulted. That makes config.yaml's
    copy of the wake threshold the one users actually meet, and a stale
    one silently ships the value the default was changed away from.

    Shipped as 0.3 while em_db said 0.5 (#144), which is exactly the drift
    this pins. Parsed rather than imported: CI installs pytest/numpy/scipy
    only, so there is no yaml module here.
    """
    import sys
    sys.path.insert(0, str(CONTROLLER))
    from em_db import DEFAULT_DEVICE_CONFIG

    config = (CONTROLLER / "config.yaml").read_text()
    match = re.search(r"^\s*oww_threshold:\s*([0-9.]+)", config, re.M)
    assert match, "config.yaml has no oww_threshold option"
    assert float(match.group(1)) == DEFAULT_DEVICE_CONFIG["owwThreshold"], (
        f"config.yaml ships oww_threshold {match.group(1)} but "
        f"em_db.DEFAULT_DEVICE_CONFIG says "
        f"{DEFAULT_DEVICE_CONFIG['owwThreshold']} — a fresh add-on install "
        f"would get the stale value"
    )


def test_addon_image_is_published_not_built_on_the_user_machine():
    """
    Without an `image:` key Supervisor builds the Dockerfile on whatever
    the user runs Home Assistant on — an onnxruntime/ffmpeg build on a Pi —
    and cannot pass EM_CONTROLLER_VERSION, so the controller reports "dev"
    and the update notice goes quiet. The arch list must also stay within
    what controller-release.yml actually publishes.
    """
    config = (CONTROLLER / "config.yaml").read_text()
    assert re.search(r"^image:\s*\S+", config, re.M), \
        "config.yaml must pull the published image, not build on the user's machine"

    workflow = (CONTROLLER.parent / ".github/workflows/controller-release.yml").read_text()
    platforms = re.search(r"platforms:\s*(\S+)", workflow)
    assert platforms, "controller-release.yml no longer declares platforms"
    published = platforms.group(1)
    arch_block = re.search(r"^arch:\n((?:\s+-\s*\w+\n)+)", config, re.M)
    assert arch_block, "config.yaml has no arch list"
    declared = set(re.findall(r"-\s*(\w+)", arch_block.group(1)))
    # Home Assistant's arch names vs Docker's platform names.
    equivalent = {"aarch64": "linux/arm64", "amd64": "linux/amd64"}
    for arch in declared:
        assert equivalent.get(arch) in published, (
            f"config.yaml offers {arch} but controller-release.yml only "
            f"publishes {published} — that install would find no image"
        )


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


def test_streamed_playback_waits_for_the_device_not_a_computed_sleep():
    """
    Turn playback must end when the DEVICE says its buffer drained, never on an
    `audio_duration - elapsed` estimate.

    That estimate was removed on 2026-07-24: it has no visibility of the
    device's own buffer and cleared the ring 6.1s early on Retreat, 3.2s on
    Lounge. The streaming path reintroduced it (PR #47) where it is worse still
    — streaming already consumes most of the audio duration, so the remainder
    computes to ~0 and the wait disappears while up to ~5.5s is queued in
    audioChanDepth.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    fn = src[src.index("async def _run_streaming_post_turn_playback"):]
    fn = fn[:fn.index("\nasync def ", 1)]

    assert "playback_done" in fn, \
        "streamed playback must await the device's playback_stats"
    assert "asyncio.sleep(remaining)" not in fn, \
        "the computed drain estimate was removed on 2026-07-24 — do not restore it"


def test_send_ms_stays_socket_write_time():
    """
    send_ms is documented as socket-write time that completes near-instantly
    however slow the link is — "never read it as delivery; that mistake cost a
    whole investigation on 2026-07-20". Timing the whole streaming loop instead
    folds HA's synthesis time in and makes it read exactly like delivery.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    fn = src[src.index("async def stream_speaker_chunks"):]
    fn = fn[:fn.index("\n    async def ", 1) if "\n    async def " in fn[1:] else len(fn)]
    assert "send_seconds" in fn, \
        "socket-write time must be accumulated around the send calls"

    caller = src[src.index("async def _run_streaming_post_turn_playback"):]
    caller = caller[:caller.index("\nasync def ", 1)]
    assert "device.playback_send_ms = send_ms" in caller, \
        "send_ms must come from accumulated write time, not the loop duration"


def test_meter_ring_is_raised_when_audio_starts_not_at_playback_setup():
    """
    The meter pattern renders the live speaker RMS, so it draws an UNLIT ring
    until the device's ALSA write actually begins.

    On the buffered path that was invisible: the TTS fetch had already
    completed, so frames flushed at socket speed and audio began almost at
    once. Streaming moves fetch+decode inside playback, so raising the meter at
    setup time leaves the ring dark from the end of the spinner until HA
    returns audio — seconds on a slow response, and indistinguishable from a
    failed turn (user report 2026-07-31).

    The spinner must therefore stay up until the meter has something to show.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    fn = src[src.index("async def post_turn_play_esphome"):]
    fn = fn[:fn.index("\n            # P0-1")]

    assert "_meter_at_playback_start(pcm_chunks" in fn, \
        "the meter must be gated on the first audio reaching the device"

    # A meter send is legitimate inside the nested helpers (_meter_on, and the
    # dead-man refresher). What must not exist is one in the function's OWN
    # body — that runs at setup, before any audio. Nested bodies are indented
    # deeper, so indentation is the discriminator.
    assert "\n" + " " * 20 + "await device.send_led_anim(meter)" not in fn, \
        ("the meter is being raised at playback setup — on the streaming path "
         "that is before HA has returned any audio, leaving the ring dark")


def test_meter_gate_fires_for_responses_shorter_than_the_prime_window():
    """
    A response shorter than SPEAKER_PRIME_SECONDS never reaches the byte
    threshold — the device starts playing it at EOS instead. Exhaustion must
    fire the callback too, or short answers play with no ring at all.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    fn = src[src.index("async def _meter_at_playback_start"):]
    fn = fn[:fn.index("\nasync def ", 1)]

    body = fn.split("async for")[1]
    assert "if not fired:" in body.rsplit("\n", 4)[-4:][0] or "if not fired" in body, \
        "the generator must fire on exhaustion for sub-prime-length responses"
    assert "SPEAKER_PRIME_SECONDS" in fn, \
        "the threshold must track the device's actual prime window"


def test_controller_update_is_advisory_only():
    """
    The dashboard may TELL you a newer controller exists; it must never offer
    to apply it.

    The controller is a container the user owns and updates with their own
    docker tooling. An in-app update would have to restart the process serving
    the page, mid-request, with no way to report the outcome — and it is an
    explicit product decision (Wil, 2026-07-31) that this stays out of the
    interface. The notice is information for a decision the user takes
    elsewhere.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    jsx = (root / "static" / "dashboard.jsx").read_text()
    start = jsx.index("{/* Controller update notice.")
    banner = jsx[start:jsx.index("{/* Summary */}", start)]
    for forbidden in ("API.post", "API.put", "API.delete", "onClick={doUpdate"):
        assert forbidden not in banner, (
            f"the controller update notice must not perform actions, found "
            f"{forbidden!r} — updating is the user's docker command to run"
        )

    api = (root / "em_api.py").read_text()
    assert 'add_get("/api/releases/controller"' in api, \
        "the controller release endpoint must be read-only (GET)"
    for verb in ("add_post", "add_put", "add_delete"):
        assert f'{verb}("/api/releases/controller' not in api, \
            f"{verb} on /api/releases/controller would make the update actionable"


def test_controller_notes_come_from_the_tag_annotation():
    """
    controller-v* tags ship a GHCR image and no GitHub Release (CLAUDE.md,
    "Versioning / releases"), so the notes must be read from the annotated
    tag object. Reading them from the releases list would return the newest
    DEVICE firmware release instead — right shape, wrong product.
    """
    from pathlib import Path
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    fn = api[api.index("async def _fetch_controller_release"):]
    fn = fn[:fn.index("\nasync def ", 1)]
    assert "GITHUB_TAGS_URL" in fn and "GITHUB_TAG_OBJECT_URL" in fn, \
        "controller notes must come from the tag annotation, not /releases"
    assert "GITHUB_API_URL" not in fn, \
        "that is the device firmware release feed, not the controller's"


def test_every_db_call_in_em_api_exists():
    """
    A typo'd db.<name> is invisible to pyflakes (it is a valid attribute
    expression) and raises AttributeError only on the request that uses it.
    That is the same shape as the NameError which stopped wake word
    fleet-wide on 2026-07-30: green CI, clean logs, broken at runtime.

    Written after db.get_global_config() — a function that has never existed —
    reached a live endpoint.
    """
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    tree = ast.parse((root / "em_api.py").read_text())
    used = {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name) and n.value.id == "db"
    }
    db_tree = ast.parse((root / "em_db.py").read_text())
    defined = {
        n.name for n in db_tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        t.id for n in db_tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    } | {
        # Annotated module constants, e.g. `MIGRATIONS: list[str] = [...]`.
        # Missing these produced a false positive on db.MIGRATIONS, which is
        # the failure mode that gets a guard disabled rather than fixed.
        n.target.id for n in db_tree.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    missing = sorted(used - defined)
    assert not missing, f"em_api.py calls db.{{{', '.join(missing)}}} which em_db.py does not define"


def test_the_wake_word_asset_wizard_step_is_mandatory():
    """
    Wil's call, overriding my "make it skippable": every provisioned device
    carries the runtime.

    The assets are not in the firmware, so a device without them advertises
    the oww_shadow capability while being unable to use it — the exact "I
    enabled it and nothing happened" this feature exists to remove. It also
    auto-runs: a button someone can leave unpressed is not mandatory.
    """
    from pathlib import Path
    jsx = (Path(__file__).resolve().parent.parent / "static" / "dashboard.jsx").read_text()

    steps = jsx[jsx.index("const _WIZARD_STEPS = ["):]
    steps = steps[:steps.index("\n];")]
    assert "'install_oww'" in steps, "the wake word asset step is missing from the wizard"

    idx = steps.count("{ id:", 0, steps.index("'install_oww'")) - 1
    auto = jsx[jsx.index("const autoSteps = new Set(["):]
    auto = auto[:auto.index(")")]
    assert str(idx) in auto, (
        f"step {idx} (install_oww) must auto-run — a step that needs a click "
        f"is one a user can skip"
    )

    runner = jsx[jsx.index("async function runInstallOwwAssets"):]
    runner = runner[:runner.index("\n  async function ", 1)]
    assert "a.md5" in runner and "throw new Error" in runner, \
        "the push must verify md5 and fail loudly — a truncated file fails later at dlopen"


def test_turn_end_reports_real_media_state_not_a_hardcoded_idle():
    """
    Issue #53: "the esphome media player reports that it is idle even though
    the music continues to play on the echo."

    Every voice turn ended by asserting MediaPlayerState.IDLE regardless of
    what the media player was doing. The feed announces PLAYING exactly once,
    when the decoder starts, so this IDLE arrived afterwards and became HA's
    last word — the entity showed a play arrow over audible music, and nothing
    ever corrected it.

    _media_state_msg() exists for precisely this ("current media_player state
    as HA should see it — em_player truth") and was being bypassed.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_esphome.py").read_text()

    fn = src[src.index("        finally:\n            # Signal HA that the satellite has finished"):]
    fn = fn[:fn.index("self._turn_active    = False")]

    assert "self._media_state_msg()" in fn, \
        "the turn must report the real media state at the end"
    assert "state=MediaPlayerState.IDLE" not in fn, (
        "a hardcoded IDLE at turn end overwrites the feed's PLAYING and "
        "leaves HA showing idle over audible music"
    )


def test_no_unjustified_hardcoded_media_state():
    """
    Forbid the SHAPE, not just the instances.

    A hardcoded MediaPlayerState sent to HA asserts what the player is doing
    without asking em_player, so it is wrong whenever the guess is wrong — and
    it wins, because the feed announces PLAYING exactly once, at the start.

    This has now been the same bug twice: the turn-end IDLE (#53, "reports
    idle even though the music continues to play"), and then IDLE on every
    device volume report, which told Music Assistant the music had stopped
    while it was audibly playing. Fixing instances one at a time is how the
    second one survived the first fix, so this pins the rule.

    One remains legitimate: PLAYING, documented as optimistic — the feed
    pushes the authoritative state moments later. It covers the announce
    transition too, which used to send ANNOUNCING.

    ANNOUNCING is now FORBIDDEN rather than merely unused. It is a real
    protobuf value and the truthful one, and HA's esphome media_player has no
    mapping for it — `_STATES.from_esphome` raises `KeyError:
    <MediaPlayerState.ANNOUNCING: 4>` inside async_write_ha_state, on a path
    unrelated to the announcement's own result, so it surfaces only as "Task
    exception was never retrieved". Nothing in HA's UI says a word, which is
    why it survived.

    Anything else must go through _media_state_msg(), which reads em_player
    truth.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_esphome.py").read_text()

    code = "\n".join(
        l for l in src.splitlines() if not l.lstrip().startswith("#")
    )
    assert "MediaPlayerState.ANNOUNCING" not in code, (
        "HA cannot map ANNOUNCING — it raises KeyError in the entity state "
        "write on every announcement"
    )

    allowed = {"MediaPlayerState.PLAYING"}
    found = [
        line.strip()
        for line in src.splitlines()
        if "state=MediaPlayerState." in line
    ]
    offenders = [
        ln for ln in found
        if not any(a in ln for a in allowed)
    ]
    assert not offenders, (
        f"hardcoded media state(s) {offenders} — use _media_state_msg() so the "
        f"entity reflects what em_player is actually doing"
    )


def test_every_deliberate_cancel_also_flushes_the_speaker():
    """
    Cancelling a turn must stop the AUDIO, not just our end of it.

    cancel_event aborts the controller's feed. It cannot touch what is already
    on the device — up to ~5.5s sits in audioChanDepth — so without a
    speaker_flush the ring clears and the device carries on talking after you
    have visibly cancelled it. Reported 2026-08-01 for the action button,
    which was the one deliberate cancel missing it while mute and barge-in
    both had it.

    Forbidding the shape rather than fixing the instance: this is the second
    bug of exactly this kind today (the other was a hardcoded MediaPlayerState
    in one of three places), and fixing them one at a time is how the second
    one survived the first.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()

    # Each deliberate cancel site, identified by its log line / guard, paired
    # with how far to look for the flush that must accompany it.
    sites = {
        "Dot button — cancelling voice turn": 900,
        "Muted during active turn": 900,
    }
    for marker, window in sites.items():
        i = src.find(marker)
        assert i != -1, f"cancel site {marker!r} not found — has it been renamed?"
        block = src[i:i + window]
        assert "cancel_event.set()" in block, f"{marker}: no cancel"
        assert "speaker_flush" in block, (
            f"{marker}: cancels the turn but never flushes the device speaker — "
            f"the response will keep playing after the turn is cancelled"
        )


def test_supervisor_log_path_matches_between_script_and_controller():
    """
    The supervisor writes its decisions to a persistent path and the
    controller reads them back from it. Two languages, one path — if they
    drift, the fetch silently returns nothing and a failed update stays
    unexplained, which is the exact failure this feature exists to remove.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    script = (root / "device_payloads" / "start_server.sh").read_text()
    api = (root / "em_api.py").read_text()

    import re
    m = re.search(r"^SUP_LOG=(\S+)", script, re.M)
    assert m, "start_server.sh no longer defines SUP_LOG"
    script_path = m.group(1)

    m = re.search(r'^SUPERVISOR_LOG = "([^"]+)"', api, re.M)
    assert m, "em_api.py no longer defines SUPERVISOR_LOG"
    assert m.group(1) == script_path, (
        f"supervisor log path drifted: script writes {script_path}, "
        f"controller reads {m.group(1)}"
    )


def test_supervisor_log_is_persistent_and_bounded():
    """
    Two properties it cannot lose.

    PERSISTENT: under /data. /tmp is RAM-backed, so a log there is wiped by
    the reboot used to recover from the very failure it would explain.

    BOUNDED: these devices have ~350MB free and no operator. A crash-loop
    writing every few seconds must never be able to fill /data — so the trim
    happens BEFORE the append, not after.
    """
    from pathlib import Path
    import re
    script = (Path(__file__).resolve().parent.parent
              / "device_payloads" / "start_server.sh").read_text()

    m = re.search(r"^SUP_LOG=(\S+)", script, re.M)
    assert m.group(1).startswith("/data/"), \
        "the supervisor log must live on persistent storage, not /tmp"

    fn = script[script.index("sup_log() {"):]
    fn = fn[:fn.index("\n}")]
    trim = fn.index("SUP_MAX")
    append = fn.index('>> "$SUP_LOG"')
    assert trim < append, \
        "the size check must run before the append, or a crash-loop outruns it"


def test_a_failed_update_asks_for_the_supervisor_log():
    """
    The fetch cannot happen at failure time — the device is gone, which IS the
    problem. So every failure path must record that an explanation is owed,
    and the connect path must collect it.
    """
    from pathlib import Path
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()

    # The failure paths live in _run_update_locked, which is what awaits
    # _monitor_reconnect and decides what its result means. (_run_update is
    # now the queue wrapper in front of it.)
    monitor = api[api.index("async def _run_update_locked"):]
    monitor = monitor[:monitor.index("\nasync def ", 1)]
    assert monitor.count("_supervisor_log_wanted.add") >= 2, (
        "both update-failure paths (auto-rollback and timeout) must request "
        "the supervisor log"
    )

    connect = api[api.index("async def notify_device_connected"):]
    connect = connect[:connect.index("\nasync def ", 1)]
    assert "_collect_supervisor_log" in connect, \
        "nothing collects the supervisor log when the device comes back"


def test_data_reconnect_grace_is_per_stream_not_per_frame():
    """
    A dropped data connection mid-stream should cost a pause, not the rest of
    the audio (#28, @kopiro — long read-aloud responses truncated by a brief
    Wi-Fi blip).

    The budget must be spent DOWN across the stream, never a fresh wait on
    each call. send_data runs once per audio period, so a per-frame wait means
    a device that is genuinely gone stalls every remaining frame in turn — a
    stream that should abort in seconds instead drains for hours holding the
    voice lock. That failure is worse than the truncation it replaces, which
    is why the shape is pinned rather than the constant.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()

    fn = src[src.index("    async def send_data(self, data: bytes):"):]
    fn = fn[:fn.index("\n    async def ", 1)]

    assert "_data_grace_left -=" in fn, (
        "the reconnect grace must be spent down across the stream; a wait that "
        "does not decrement is a per-frame stall"
    )
    assert "_data_grace_left > 0" in fn, \
        "send_data must stop waiting once the stream's budget is exhausted"

    # Every path that streams audio has to arm it, or the budget is stale from
    # whatever ran last.
    for stream_fn in ("async def stream_speaker(self",
                      "async def stream_speaker_chunks(self"):
        body = src[src.index(stream_fn):]
        body = body[:body.index("\n    async def ", 1)]
        assert "begin_data_stream()" in body, \
            f"{stream_fn} does not arm the reconnect grace"


def test_support_bundle_attributes_metrics_to_a_device():
    """
    `db.get_device_metrics` builds its own result dicts and does NOT include
    the device, so the support handler must attach it. Without that, every
    device's hourly CPU, memory and RTT pool into one flat anonymous list —
    present in the bundle, useless for diagnosis, and wrong in the quiet way
    where the file still looks full of data. Shipped like that in #63.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    body = src.split("_get_support_bundle")[-1]
    call = re.search(r"metrics \+= \[(.*?)\]\n", body, re.S)
    assert call, "could not find where the support bundle collects metrics"
    assert "device_id" in call.group(1), (
        "support bundle metrics rows must carry device_id — "
        "get_device_metrics does not return it"
    )


def test_support_bundle_redacts_account_names():
    """
    Account names reach the bundle through ordinary log prose ("Shell session
    opened by wil"), which no quote, URL or identifier rule matches. The
    handler must therefore pass the user table to em_support; the redaction
    itself is tested in test_support.py.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    # "return web.Response", not "web.Response" — the latter is the handler's
    # own return annotation and slices the body away to nothing.
    body = src.split("_get_support_bundle")[-1].split("return web.Response")[0]
    assert "accounts=" in body and "get_all_users" in body, (
        "the support bundle must pass the real accounts to em_support — "
        "there is nothing in a log line to pattern-match them by"
    )
    assert '"role"' in body, (
        "accounts must carry the role: a name is replaced by <admin>, and a "
        "positional alias would be one-to-one with a real person"
    )


def test_the_music_feed_reads_its_lead_per_chunk():
    """
    A voice turn lowers the music feed's lead so the response gets the shared
    data plane (TURN_LEAD_S). That only works if the pacing loop reads the
    CURRENT lead each time round — capturing LEAD_S once, or referring to the
    module constant, silently restores the old behaviour and the fix becomes
    a no-op with every test still passing.
    """
    src = (CONTROLLER / "em_player.py").read_text()
    feed = src.split("async def _feed")[-1]
    pacing = re.search(r"ahead = sent / BYTES_PER_SEC.*?await asyncio\.sleep\(([^)]*)\)",
                       feed, re.S)
    assert pacing, "could not find the feed's pacing sleep"
    window = feed[:pacing.end()]
    assert "self.lead_s" in window, (
        "the pacing loop must read self.lead_s, not the LEAD_S constant — "
        "otherwise lowering the lead for a voice turn does nothing"
    )


def _fn_body(src: str, name: str) -> str:
    """Slice one async def out of a module's source, up to the next top-level def."""
    start = src.index(f"async def {name}")
    rest  = src[start + 1:]
    end   = rest.index("\nasync def ") if "\nasync def " in rest else len(rest)
    return src[start:start + 1 + end]


def test_firmware_transfer_is_verified_by_md5_not_by_an_exit_status():
    """
    TRANSFER_OK only ever proved that the decode pipeline and chmod exited 0 —
    not that the bytes on the device match the bytes we sent (#76).

    That matters because a corrupt binary and a genuinely broken one produce
    the SAME observable: three fast exits, a symlink flip, and a device back on
    its old version. Shipping an unverified binary therefore costs a reboot and
    a rollback to learn nothing at all.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn  = _fn_body(src, "_stream_file_to_device")

    assert "hashlib.md5(data).hexdigest()" in fn, (
        "the transfer must hash what it actually sent — hashing anything else "
        "verifies the wrong thing"
    )
    assert ".part" in fn and "mv " in fn, (
        "bytes must land in .part and be renamed only once verified, so a bad "
        "transfer leaves the destination as it was"
    )
    # The rename must be conditional on the comparison, not merely nearby.
    assert re.search(r"case .*GOT.*in .*want.*mv ", fn, re.S), (
        "the rename must be guarded by the md5 comparison"
    )
    assert "rm -f" in fn, "a failed verification must remove the .part"

    # Anchored on the CALL, not the function body: the docstring explains why
    # require_verify is set, so a body-wide search passes on the prose alone
    # while the argument is gone (caught by reintroducing exactly that).
    slot = _fn_body(src, "_stream_binary_to_slot")
    call = slot[slot.index("return await _stream_file_to_device"):]
    assert "require_verify=True" in call, (
        "firmware is the payload where an unverifiable transfer must fail "
        "rather than proceed — it is the one we are about to boot"
    )


def test_a_corrupt_binary_never_reaches_the_symlink_flip():
    """
    The real win of verifying is not the error message, it is the ordering:
    a mismatch must be caught while the device is still running fine, not
    after it has taken a reboot and a rollback to tell us the same thing.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    ota = src[src.index("_stream_binary_to_slot(live, binary"):]
    ota = ota[:ota.index("_monitor_reconnect")]

    guard = ota.index("if not ok:")
    flip  = ota.index("ln -sf")
    assert guard < flip, (
        "the transfer result must be checked BEFORE the symlink flip"
    )
    assert "return" in ota[guard:flip], (
        "a failed transfer must return, not fall through to the flip — "
        "otherwise verification changes the log message and nothing else"
    )


def test_ota_checks_free_space_before_writing_anything():
    """
    The OTA path had no space check at all, unlike the asset path.

    Two traps, both already paid for elsewhere: read the figure with
    parse_free_mb rather than an awk field index (busybox wraps a long
    filesystem name onto its own line, so $4 is the PERCENTAGE on these
    devices), and treat an unreadable df as "carry on" rather than as a full
    disk — refusing on an unparsed reading blocks updates on any device whose
    df we have not seen.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    ota = src[src.index("inactive_slot = "):src.index("_stream_binary_to_slot(live, binary")]

    assert "parse_free_mb" in ota, (
        "free space must be read with parse_free_mb, never an awk field index"
    )
    # Comments are stripped first: the trap is worth explaining in a comment,
    # and a test that reads its own warning as the bug is a test that can only
    # be silenced by deleting the explanation.
    code = "\n".join(l for l in ota.splitlines() if not l.lstrip().startswith("#"))
    assert "awk" not in code, "an awk field index reads the percentage on these devices"
    assert "free_mb is not None and free_mb <" in ota, (
        "an unknown reading must not be compared as if it were a number"
    )


def test_a_transfer_never_deletes_the_destination_before_sending():
    """
    For firmware the destination IS the rollback slot, so deleting it up front
    means a transfer that fails early leaves the device with a good active
    slot and an empty partner — and a later crash-loop flips the symlink onto
    nothing. Three Dots hit exactly that in #121, while being told the slot
    had been left untouched.

    The `.part` discipline only protects `dest` from a CORRUPT transfer. It
    cannot protect it from being removed before the transfer starts.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn  = _fn_body(src, "_stream_file_to_device")
    code = "\n".join(l for l in fn.splitlines() if not l.lstrip().startswith("#"))

    assert "rm -f {dest}" not in code, (
        "deleting dest before sending destroys the rollback slot on any "
        "transfer that fails early"
    )
    # The .part cleanup on a bad md5 must survive — that one is load-bearing.
    assert "rm -f {landing}" in code or "rm -f {landing};" in code, (
        "a failed verification must still remove the .part"
    )


def test_a_failed_transfer_says_which_stage_it_failed_at():
    """
    One message covered five outcomes, and the two furthest apart are "the
    bytes arrived corrupt" and "no byte was ever sent". #121 was the second
    reported in the language of the first, and only the controller's own
    stdout could tell them apart — which is not something a user can produce
    mid-update.

    A device shell that answers nothing must NOT read as "no base64 decoder":
    one is a link problem worth retrying, the other is a property of the
    device that retrying cannot change.
    """
    src = (CONTROLLER / "em_api.py").read_text()

    assert "class TransferResult" in src and "__bool__" in src, (
        "the result must stay truthy so `if not await ...` call sites keep "
        "their meaning"
    )

    fn = _fn_body(src, "_stream_file_to_device")
    for stage in ("shell", "decoder", "send", "verify", "corrupt"):
        assert f'_transfer_failed("{stage}"' in fn, (
            f"the {stage} failure must be distinguishable from the others"
        )

    assert "if DETECT_MARKER not in detect_buf" in fn, (
        "a silent shell must report as a link problem, not a missing decoder"
    )

    # The OTA message must carry the stage through rather than re-flattening it.
    ota = src[src.index("_stream_binary_to_slot(live, binary"):]
    ota = ota[:ota.index("_monitor_reconnect")]
    assert "{ok}" in ota, (
        "the update failure message must name the stage the transfer reached"
    )
    # Comments stripped first, for the reason the free-space test gives: the
    # old wording is worth naming in a comment, and a test that reads its own
    # explanation as the bug can only be silenced by deleting the explanation.
    ota_code = "\n".join(l for l in ota.splitlines() if not l.lstrip().startswith("#"))
    assert "failed or did not verify" not in ota_code, (
        "the flattened message is what made #121 unreadable"
    )


def test_tested_firmware_build_matches_the_docs():
    """
    The wizard warns when a device is on a FireOS build other than the one
    EchoMuse is developed against, and docs/rooting.md tells people which to
    flash. Those two have to name the same build: a warning pointing at a
    version the docs do not mention is worse than no warning, because the
    person reading it has nowhere to go.

    Verified against the fleet 2026-08-07 — all three connected devices report
    ro.build.version.incremental = 272.6.8.0_user_680767620.
    """
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    m = re.search(r"_TESTED_FIREOS_BUILD\s*=\s*'([^']+)'", jsx)
    assert m, "dashboard.jsx no longer declares _TESTED_FIREOS_BUILD"
    build = m.group(1)

    rooting = (CONTROLLER.parent / "docs" / "rooting.md").read_text()
    assert build in rooting, (
        f"the wizard warns against build {build} but docs/rooting.md never "
        f"names it — a reader has nowhere to go"
    )


def test_push_log_event_callers_do_not_also_persist():
    """
    `_push_log_event` persists AND pushes. A caller that also calls
    `db.log_device` writes the line twice.

    That is not hypothetical: the device `log` handler did both, so every
    device log line landed twice about 6ms apart, and roughly half of
    `device_logs` was duplicates. It stayed invisible because a doubled log
    line looks like a device that logged twice.

    Two things it cost beyond the wasted rows. `em_support.thin_noise` keeps
    the newest three `[mem]` lines per device, so duplication halved the
    distinct readings a leak hunt gets from a bundle. And the redundant call
    was a synchronous SQLite write on the event loop, which
    `event_loop_lag_monitor` exists to catch.

    Checked by source shape because the alternative is importing
    em_controller, which the suite deliberately does not do.
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("em_controller.py", "em_api.py"):
        lines = (root / name).read_text().splitlines()
        for i, line in enumerate(lines):
            if "_push_log_event(" not in line or "async def" in line:
                continue
            # The persist would sit just above the push, in the same block.
            window = lines[max(0, i - 6):i]
            offenders = [w.strip() for w in window if "db.log_device(" in w]
            assert not offenders, (
                f"{name}:{i+1} calls _push_log_event, which already persists, "
                f"but is preceded by {offenders[0]!r}. That writes the log "
                f"line twice. Drop the db.log_device call."
            )


# ── Ambient light status reaches a support bundle (#90) ──────────────────────
#
# Two users reported no light sensor and the bundle could not say why: the
# firmware knows whether the chip is absent or the driver simply has not
# bound, but writes that only to its own log, which the bundle does not
# collect and a reboot clears. Diagnosing it needed a shell session on their
# hardware. These pin the three links in the chain that fixes it, because
# each fails silently — a missing field just looks like an old device.

def test_register_handler_stores_ambient_light_status():
    """The controller must keep what the device reported at registration."""
    root = Path(__file__).resolve().parent.parent
    ctl = (root / "em_controller.py").read_text()
    assert 'device.ambient_light_status = msg.get("ambient_light_status")' in ctl, (
        "the register handler must store ambient_light_status off the register "
        "message — without it the reason is received and dropped"
    )


def test_bundle_live_state_carries_ambient_light_status():
    """And the support bundle must actually carry it.

    em_support takes live_state wholesale rather than through an allowlist, so
    the field has to be put there by em_api. A reason that never reaches a
    bundle leaves us exactly where #90 started.
    """
    root = Path(__file__).resolve().parent.parent
    api = (root / "em_api.py").read_text()
    assert '"ambient_light_status":' in api, (
        "em_api's live_state must include ambient_light_status so support "
        "bundles can answer why a device reports no light sensor"
    )


def test_ingress_login_passes_the_real_deployment_flag_and_peer_address():
    """
    The ingress login endpoint must hand em_ingressauth.decide the LIVE
    INGRESS_ONLY value and the LIVE peer address — not a literal.

    Hardcoding either turns the decision function into theatre: `True` makes
    the standalone container honour an attacker-supplied X-Remote-User-Id,
    which is an unauthenticated admin session on a dashboard that proxies a
    root shell to every device. The decision itself is tested in
    test_ingressauth.py; this pins that it is actually consulted with real
    inputs.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    call = re.search(
        r"em_ingressauth\.decide\((.*?)\)\s*\n", src, re.S)
    assert call, "em_api no longer calls em_ingressauth.decide"
    args = call.group(1)

    assert "ingress_only=INGRESS_ONLY" in args, (
        "ingress_only must be the module's INGRESS_ONLY, never a literal")
    assert "remote=request.remote" in args, (
        "remote must be the live peer address, never a literal")
    for literal in ("ingress_only=True", "remote=em_ingressauth.INGRESS_GATEWAY_IP"):
        assert literal not in args, f"{literal} defeats the check entirely"


def test_ingress_login_is_the_only_reader_of_the_remote_user_headers():
    """
    X-Remote-User-* may be read in exactly one place. A second reader is a
    second chance to forget that the headers are only meaningful behind
    Supervisor's gateway — Supervisor strips client copies, nothing else does.
    """
    # Match the READ, not the mention — em_ingressauth names these headers in
    # prose explaining where they come from and why they can be trusted,
    # which is documentation rather than a second trust boundary.
    readers = []
    for path in CONTROLLER.glob("*.py"):
        text = path.read_text()
        if re.search(r'headers(?:\.get\(|\[)\s*["\']X-Remote-User', text):
            readers.append(path.name)
    assert readers == ["em_api.py"], (
        f"X-Remote-User headers read in {readers} — expected em_api.py only")


def test_addon_panel_stays_admin_only():
    """
    panel_admin gates the sidebar entry for a dashboard that proxies a root
    shell. It is Supervisor's default, pinned here so a future edit to
    config.yaml has to be deliberate.
    """
    cfg = (CONTROLLER / "config.yaml").read_text()
    assert re.search(r"^panel_admin:\s*true\s*$", cfg, re.M), (
        "config.yaml must set panel_admin: true explicitly")


def test_recordings_and_transcripts_are_admin_only():
    """
    Utterance audio and STT text are the most sensitive things the controller
    stores — recognisable speech from inside someone's home, and the reason
    saveUtterances defaults off.

    Under the add-on every Home Assistant user in the household can reach the
    dashboard (HA's ingress view sets requires_auth=False and panel_admin only
    hides the sidebar entry), so "read-only" stopped being a synonym for
    "someone the operator trusts with the recordings".

    Enforced server-side, not in the dashboard: /api/devices/{id}/turns is a
    plain GET with a session token, so a UI-only rule protects nothing from
    anyone who opens the network tab.
    """
    src = (CONTROLLER / "em_api.py").read_text()

    audio = re.search(
        r"(@auth\.require_\w+)\s*\nasync def _get_turn_audio\b", src)
    assert audio, "_get_turn_audio not found"
    assert audio.group(1) == "@auth.require_admin", (
        "the utterance audio route must be admin-only")

    turns = re.search(
        r"async def _get_device_turns\b.*?\n(?=\n@|\ndef |\nasync def )",
        src, re.S)
    assert turns and "_redact_turns_for" in turns.group(0), (
        "_get_device_turns must strip transcripts for non-admin sessions")

    redact = re.search(r"def _redact_turns_for\(.*?\n(?=\n@|\ndef |\nasync def )",
                       src, re.S)
    assert redact and '"stt_text"' in redact.group(0), (
        "_redact_turns_for must remove stt_text")


def test_role_changes_refuse_to_strand_the_install():
    """
    PATCH /api/users/{id} must refuse to demote the last admin. On the
    standalone container local accounts are the only auth, so an install with
    no admin has no way back in — and the endpoint that creates that state is
    the one an admin reaches for while tidying up.

    """
    src = (CONTROLLER / "em_api.py").read_text()
    handler = re.search(
        r"async def _patch_user\b.*?\n(?=\n@|\ndef |\nasync def )", src, re.S)
    assert handler, "_patch_user not found"
    body = handler.group(0)

    assert "admin_count" in body, "must count admins before demoting one"
    assert "last_admin" in body, "must refuse to remove the final admin"



def test_addon_options_are_wired_end_to_end():
    """
    An add-on option needs four separate things to work: a default in
    `options`, a type in `schema`, a translation, and a line in em_start.py's
    OPTION_ENV_VARS. Miss one and it fails a different way each time, none of
    them loud:

      - no `schema` entry — Supervisor rejects the whole options block
      - no translation      — the raw key renders as the field label
      - no OPTION_ENV_VARS  — the setting is accepted, displayed, stored, and
                              then ignored; em_start warns to the add-on log
                              and starts anyway, which is the right call for
                              boot resilience and means nobody sees it

    The reverse direction matters too: an OPTION_ENV_VARS entry with no
    option is a mapping for a setting Supervisor will never send.

    Added with the `debug` option (2026-08-16). DEBUG had no add-on option at
    all, so controller debug logging — the first thing support asks for — was
    reachable on the container and not on the add-on.
    """
    import yaml

    config = yaml.safe_load((CONTROLLER / "config.yaml").read_text())
    translations = yaml.safe_load(
        (CONTROLLER / "translations/en.yaml").read_text())

    options = set(config["options"])
    schema = set(config["schema"])
    translated = set(translations["configuration"])

    start = (CONTROLLER / "em_start.py").read_text()
    block = re.search(r"OPTION_ENV_VARS\s*=\s*\{(.*?)\}", start, re.S)
    assert block, "em_start.py no longer defines OPTION_ENV_VARS"
    mapped = set(re.findall(r'"(\w+)"\s*:', block.group(1)))

    assert not options - schema, \
        f"options with no schema entry: {sorted(options - schema)}"
    assert not schema - options, \
        f"schema entries with no default: {sorted(schema - options)}"
    assert not options - translated, \
        f"options with no translation: {sorted(options - translated)}"
    assert not options - mapped, (
        f"options not in em_start.py's OPTION_ENV_VARS: "
        f"{sorted(options - mapped)} — Supervisor would accept and store "
        f"these and the controller would never see them")
    assert not mapped - options, (
        f"OPTION_ENV_VARS entries with no add-on option: "
        f"{sorted(mapped - options)}")


def test_debug_env_var_is_not_truthy_for_zero():
    """
    em_start.py renders a false bool option as the STRING "0", and every
    non-empty string is truthy in Python. So a bare
    `if os.environ.get("DEBUG")` puts every add-on install at DEBUG level
    with the toggle showing off — the failure being verbose logs nobody
    asked for, on the deployment least able to rotate them.

    Pinned rather than assumed because the original line read exactly that
    way, and the same shape is the obvious thing to write for the next
    bool env var.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert not re.search(r'if\s+os\.environ\.get\(\s*"DEBUG"\s*\)\s*else', src), (
        "DEBUG is being read as a bare truthiness test — the string \"0\" "
        "that em_start.py writes for a false option is truthy")

    match = re.search(r'^DEBUG\s*=\s*(.+)$', src, re.M)
    assert match, "em_controller.py no longer defines a DEBUG flag"
    assert '"0"' in match.group(1) and '"1"' in match.group(1), (
        "DEBUG must default to \"0\" and test for \"1\", the convention "
        "REQUIRE_DEVICE_TLS already uses")
def test_wake_word_phrase_is_sent_and_matches_what_we_advertise():
    """
    Home Assistant's voice satellite setup arms an interceptor for the next
    wake word and reads `wake_word_phrase` off the pipeline start. On None it
    raises AssistSatelliteError("No wake word phrase provided") and ends the
    run in milliseconds — so every voice turn worked while the one flow that
    asks a device to prove it heard a wake word could never complete, and the
    only way past was Skip (confirmed on hardware 2026-08-16).

    HA then matches that phrase against the STATE of its wake-word select
    entity, whose options are the display names we advertise
    (esphome/assist_satellite.py `ww_state.state == wake_word_phrase`). So the
    phrase and the advertised name must come from ONE function or they drift
    apart and HA silently matches neither.

    Source-shape assertions: this suite does not import em_esphome, which
    needs aiohttp and the protobufs.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()

    start = re.search(r"api_pb2\.VoiceAssistantRequest\((.*?)\)\)", src, re.S)
    assert start, "the pipeline-start VoiceAssistantRequest was not found"
    assert "wake_word_phrase" in start.group(1), (
        "VoiceAssistantRequest must carry wake_word_phrase — without it HA "
        "refuses the run and the satellite setup dialog never advances")

    advertised = re.search(r"api_pb2\.VoiceAssistantWakeWord\((.*?)\)", src, re.S)
    assert advertised, "VoiceAssistantWakeWord advertisement not found"
    assert "em_oww_models.display_name" in advertised.group(1), (
        "the advertised wake_word must come from em_oww_models.display_name, "
        "the same source as the phrase we send")

    assert 'display_name(server.oww_model_id)' in src, (
        "the phrase must be derived with display_name too — a second spelling "
        "is how it drifts from what we advertised")


def test_button_turns_claim_no_wake_word():
    """
    A button turn has no wake word, and saying otherwise tells HA something
    untrue about how the turn started — it would also satisfy a setup-flow
    interceptor with a wake word nobody spoke. aioesphomeapi maps "" back to
    None, so an empty string is the protocol's way to say "none".
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    gate = re.search(r'wake_word_phrase\s*=\s*\((.*?)else ""', src, re.S)
    assert gate, "the wake_word_phrase gate was not found"
    assert 'trigger_label.startswith("wakeword")' in gate.group(1), (
        "the phrase must be gated on the trigger being a wake word, so button "
        "turns send \"\"")


def test_a_run_ha_never_started_ends_the_turn():
    """
    Home Assistant's satellite setup intercepts a wake word by emitting
    RUN_END and returning, without ever starting a pipeline. The controller
    held the microphone anyway until its own timers expired — measured at
    20.0s (streaming hard cap) and 5.2s (no-speech window) — while HA had
    re-armed for the next wake word 18ms after ending the first. That is why
    the setup flow's second "say it again" step landed on a device still
    listening for the first.

    The discriminator is RUN_START, not timing. Measured on hardware, a
    genuine run is RUN_START (2ms before STT_START) … RUN_END (last, after
    TTS_END); an interception is RUN_END alone. So a RUN_END with no
    RUN_START cannot be the premature/duplicate RUN_END the other branch
    exists for, because that one arrives MID-turn and a mid-turn RUN_END
    follows the RUN_START that opened it.

    Also: the outcome is `pipeline_refused`, not `no_speech`. The audio was
    captured and streamed into a run that had already closed, and the outcome
    is persisted — so `no_speech` would put every HA-side refusal into the
    activity stats as a silent user.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()

    assert "VOICE_ASSISTANT_RUN_START" in src, (
        "RUN_START must be handled — its absence is what identifies a "
        "RUN_END that HA emitted without running a pipeline")
    assert "_run_started" in src and "_ha_never_started" in src, \
        "no state tracks whether HA ever started the run it just ended"
    assert '"pipeline_refused"' in src, \
        "an HA run that never started must not be recorded as no_speech"

    handler = re.search(
        r"VOICE_ASSISTANT_RUN_END:(.*?)elif event_type", src, re.S)
    assert handler, "RUN_END handler not found"
    body = handler.group(1)

    assert "if not self._run_started:" in body, (
        "RUN_END must check whether a RUN_START was ever seen before "
        "treating itself as terminal")

    # The premature/duplicate branch must stay gated on INTENT_END — acting
    # on a mid-turn RUN_END would cut genuine turns short.
    premature = body.split("if not self._run_started:")[1]
    assert "self._intent_ended or self._turn_cancelled" in premature, (
        "a RUN_END that DID follow a RUN_START must stay non-terminal until "
        "INTENT_END — this is the guard against cutting genuine turns")


def test_entity_names_do_not_repeat_the_device_label():
    """
    Home Assistant sets `_attr_has_entity_name = True` for every esphome
    entity and composes "<device name> <entity name>" itself. Our device name
    is already "<label> Voice Assistant", so putting the label in the entity
    name too renders it twice:

        'EA Test Device 01 Voice Assistant EA Test Device 01 Ambient Light'
        'Lounge Voice Assistant Lounge'

    Observed on every device and every entity (2026-08-16). em_ble_proxy had
    it right all along with a bare "BLE advertisements", which is what makes
    this a slip rather than a misunderstanding.

    An empty name is HA's convention for the device's primary entity —
    `self._attr_name = static_info.name or None` in esphome/entity.py — and
    renders as the device name alone, so the media player is deliberately
    "" rather than a label.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()

    block = re.search(r"ListEntitiesMediaPlayerResponse\((.*?)ListEntitiesDoneResponse",
                      src, re.S)
    assert block, "the ListEntities block was not found"
    body = block.group(1)

    assert "self.label" not in body, (
        "an entity name must not include the device label — HA already "
        "prefixes the device name, so it renders twice")
    assert 'name=""' in body, \
        "the media player should take the device's own name"


def test_asset_sync_does_not_shadow_its_accumulator():
    """
    _sync_oww_assets keeps a `pushed` list of installed asset names. Assigning
    the per-file transfer result to that same name shadowed the list on the
    FIRST file, so the append at the end of the loop raised
    AttributeError: 'TransferResult' object has no attribute 'append'
    and on-device wake word assets could not be installed at all.

    It reached a release because TransferResult is deliberately truthy-
    compatible so existing `if not …` call sites keep working — which is
    exactly why the one call site that treated the result as a list was the
    only thing that broke, and nothing else complained.

    Found on hardware 2026-08-16: a device reporting "classifier model not
    installed" while the dashboard offered to send it and returned 500.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _sync_oww_assets\(.*?\n(?=\nasync def |\ndef )",
                   src, re.S)
    assert fn, "_sync_oww_assets not found"
    body = fn.group(0)

    assert "pushed = []" in body, "the accumulator is gone"
    assert "pushed.append(" in body, "nothing accumulates installed names"
    assert not re.search(r"pushed\s*=\s*await\s+_stream_file_to_device", body), (
        "the transfer result must not be assigned to `pushed` — that shadows "
        "the accumulator and the append raises AttributeError")


def test_a_new_wake_word_is_installed_before_the_device_is_told_about_it():
    """
    A device cannot score a wake word whose classifier it does not have, and
    under owwOnDevice=on the controller has stood down — so telling it to use
    a model it lacks produced a device with NO wake word: nothing fired,
    nothing warned, the dashboard reported it healthy (#191).

    The first fix stood the device down to controller-side scoring while the
    model installed. That works, and it silently overrides a setting the user
    chose — a posture change they did not ask for and cannot see. Holding the
    key back instead means the device keeps listening for its CURRENT wake
    word, on-device, throughout, and simply stays there if the install fails.

    The hold-back is invisible at the call site: the config push looks
    ordinary, and the whole guard is that one key was swapped out first.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _apply_live_config\(.*?\n(?=\nasync def |\ndef )",
                   src, re.S)
    assert fn, "_apply_live_config not found"
    body = fn.group(0)

    hold = body.find("_hold_back_oww_model(live, effective)")
    push = body.find('send_control({"type": "config"')
    assert hold != -1, "the new model is no longer held back"
    assert push != -1, "the config push is gone"
    assert hold < push, (
        "the model must be held back BEFORE the config is pushed, or the "
        "device is told to use a model it does not have"
    )
    assert "_install_then_switch" in body, "nothing installs the pending model"


def test_only_devices_that_score_locally_are_held_back():
    """
    With owwOnDevice=off the controller does the scoring and the file on the
    device is irrelevant — holding the change back would delay it for no
    reason. That is the common case and it stays instant.

    Both the old and the new mode are consulted: enabling on-device scoring in
    the same save that changes the wake word must not slip through on the
    strength of the old mode being "off".
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"def _hold_back_oww_model\(.*?\n(?=\nasync def |\ndef )",
                   src, re.S)
    assert fn, "_hold_back_oww_model not found"
    body = fn.group(0)

    assert "oww_shadow_capable" in body, (
        "a device that cannot score locally must not be held back"
    )
    assert body.count("MODE_OFF") >= 2, (
        "both the current and the incoming mode must be consulted"
    )
    assert 'effective.get("owwOnDevice"' in body, (
        "the incoming mode is not read — a save that turns on-device scoring "
        "on while changing the wake word would slip through"
    )


def test_a_failed_install_leaves_the_device_on_its_old_wake_word():
    """
    The failure direction that matters. Switching anyway is what produced the
    deaf device; standing the device down instead overrides the user's choice.
    Staying put means the device keeps answering, on a wake word it can hear.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn = re.search(r"async def _install_then_switch\(.*?\n(?=\nasync def |\ndef )",
                   src, re.S)
    assert fn, "_install_then_switch not found"
    body = fn.group(0)

    # The failure BRANCH itself, not the span up to the switch — that span
    # contains other returns, which is how the first version of this test
    # passed against a branch that fell straight through.
    branch = re.search(r'\n    if not result\.get\("ok"\):\n(.*?)\n    (?=\S)',
                       body, re.S)
    assert branch, "the failure branch is gone"
    assert re.search(r"^        return\s*$", branch.group(1), re.M), (
        "a failed install must return, not fall through into switching the "
        "device onto a model it does not have"
    )



def test_both_effective_mode_call_sites_pass_readiness():
    """
    Config push and device registration both resolve the mode. A guard applied
    to one and not the other is a device that is safe until it reconnects —
    the same shape as the v7 stats-relay miss.
    """
    for name in ("em_api.py", "em_controller.py"):
        src = (CONTROLLER / name).read_text()
        # Non-greedy matching to the first ")" is wrong here: the argument
        # itself contains one (`effective.get("owwOnDevice")`). Take a fixed
        # window after each call instead — the call sites are three lines.
        for m in re.finditer(r"effective_mode\(", src):
            call = src[m.end():m.end() + 200]
            assert "model_ready" in call or "oww_model_ready" in call, (
                f"{name}: an effective_mode call omits model readiness — "
                f"{call.splitlines()[0]!r}"
            )


def test_an_announcement_clears_the_cancel_flag_before_playing():
    """
    cancel_event is set by a cancel — a button press during a turn, a mute —
    and was ONLY ever cleared when the next VOICE TURN started. Nothing in the
    announcement path cleared it, and _run_post_turn_playback checks it, so a
    cancelled turn silently killed every subsequent announcement until a turn
    happened to run.

    Measured on Test Device 01, 2026-08-17: a turn cancelled at 12:02:32 left
    seven announcements over the next three minutes logging "Cancelled during
    playback" and playing nothing. Adding a second device made it look like a
    routing bug — that device played fine, because its own flag was clear.

    An announcement is a new action; nothing that set the flag earlier has any
    claim on it.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    fn = re.search(r"async def _standalone_play\(.*?\n(?=        async def )",
                   src, re.S)
    assert fn, "_standalone_play not found"
    body = fn.group(0)

    # The literal calls, not the prose: this function's comments name
    # _run_post_turn_playback before either call happens.
    clear = body.find("_d.cancel_event.clear()")
    play = body.find("await _run_post_turn_playback(")
    assert clear != -1, (
        "an announcement no longer clears cancel_event — a cancelled turn "
        "will silently kill every announcement that follows it"
    )
    assert clear < play, "the flag must be cleared BEFORE the audio is played"


def test_an_announcement_reports_whether_it_actually_played():
    """
    The reply to HA carries one fact — did the user hear it. Something that
    cancels mid-playback means they did not, and `return True` regardless
    would make the reply decorative.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    fn = re.search(r"async def _standalone_play\(.*?\n(?=        async def )",
                   src, re.S)
    body = fn.group(0)
    assert "-> bool" in body, "_standalone_play must report whether it played"
    assert re.search(r"return not .*cancel_event\.is_set\(\)", body), (
        "the return value must reflect whether playback was cancelled"
    )


def test_the_speaking_flag_and_its_dashboard_push_cannot_drift():
    """
    The dashboard renders `speaking` above `thinking` and _push_device_state
    has always carried it — but nothing pushed the transition. The only pushes
    were at listening, thinking and turn end, so a turn read
    listening -> thinking -> idle and never showed Speaking at all. It appeared
    only when the dashboard's 5s poll of /api/devices happened to land
    mid-playback, which for a ~2s response it usually did not.

    Setting the flag and telling anyone about it are now one operation, so a
    third streaming path cannot reintroduce the gap by doing only the first.
    """
    src = (CONTROLLER / "em_controller.py").read_text()

    setter = re.search(r"async def _set_speaking\(.*?\n(?=    async def |    def )",
                       src, re.S)
    assert setter, "_set_speaking not found"
    assert "_push_device_state(self)" in setter.group(0), (
        "the setter must push — that is the whole reason it exists"
    )

    # Every other write is a bug. __init__'s default and the setter's own
    # assignment are the two legitimate ones.
    writes = re.findall(r"self\.speaking\s*=\s*(?!=)", src)
    assert len(writes) == 2, (
        f"expected 2 assignments to self.speaking (the __init__ default and "
        f"the setter), found {len(writes)} — a streaming path is setting the "
        f"flag without pushing it"
    )


def test_speaking_clears_on_device_confirmation_not_on_the_socket_write():
    """
    The stream task returns when the last byte reaches the socket, which
    completes near-instantly however slow the link is — the device still has
    its whole buffer to play. Clearing the flag there dropped the tile out of
    Speaking seconds early, and because `thinking` was still set it fell BACK
    to Thinking mid-response before finally going idle.

    Exactly the mistake the LED ring made until 2026-07-24, in the same file.
    The playback functions wait on the device's playback_stats; they are the
    only things that know the speaker has stopped.
    """
    src = (CONTROLLER / "em_controller.py").read_text()

    for fn_name in ("stream_speaker", "stream_speaker_chunks"):
        fn = re.search(rf"async def {fn_name}\(.*?\n(?=    async def |    def )",
                       src, re.S)
        assert fn, f"{fn_name} not found"
        assert "_set_speaking(False)" not in fn.group(0), (
            f"{fn_name} clears speaking when the socket write finishes, not "
            f"when the audio does"
        )

    for fn_name in ("_run_post_turn_playback", "_run_streaming_post_turn_playback"):
        fn = re.search(rf"async def {fn_name}\(.*?\n(?=\nasync def |\ndef )",
                       src, re.S)
        assert fn, f"{fn_name} not found"
        assert "_set_speaking(False)" in fn.group(0), (
            f"{fn_name} waits for the device's playback_stats and must be what "
            f"clears speaking"
        )


def test_speaking_and_thinking_are_mutually_exclusive():
    """
    Both flags reach the dashboard and `speaking` outranks `thinking`, so a
    stale `thinking` is invisible until speaking clears — and then the tile
    reads as if the device started thinking again mid-response.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    setter = re.search(r"async def _set_speaking\(.*?\n(?=    async def |    def )",
                       src, re.S).group(0)
    assert "self.thinking = False" in setter, (
        "starting to speak must clear thinking, or the tile falls back to "
        "Thinking when speaking ends"
    )


def test_the_speaking_push_cannot_fail_a_speaker_stream():
    """
    One caller is stream_speaker's finally, which is also reached when
    barge-in cancels the task mid-send. A push that cannot complete there must
    not take the stream down with it — turn end pushes the same state moments
    later. The flag assignment is synchronous and happens either way.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    setter = re.search(r"async def _set_speaking\(.*?\n(?=    async def |    def )",
                       src, re.S).group(0)
    assign = setter.find("self.speaking = value")
    push = setter.find("_push_device_state(self)")
    assert assign < push, "the flag must be set before the push is attempted"
    assert "except BaseException" in setter, (
        "a bare `except Exception` does not catch the CancelledError this sees "
        "during barge-in"
    )


# ── The wake listener must not be able to die quietly ─────────────────────────
#
# Source-shape tests, because the suite cannot import em_controller — which is
# precisely why this had no coverage. On 2026-08-20 a device went deaf with no
# error line and stayed that way until the add-on was restarted: the listener
# is started with create_task and catches only CancelledError, so any other
# exception ends it, and the connection handler holding a reference means
# asyncio never even logs "Task exception was never retrieved". The device
# meanwhile scores wake words and reports them into a dead loop, so it looks
# healthy from every side.

def test_the_wake_listener_is_started_through_its_supervisor():
    """
    A bare create_task(wake_word_listener(...)) is the bug. The whole guard is
    that the call site goes through the supervisor, and nothing else enforces
    that.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # The supervisor's OWN create_task is the one legitimate use, so check
    # everywhere else. Splitting on the def keeps this honest if the helper
    # moves.
    start = src.index("def _supervise_wake_listener")
    end = src.index("async def wake_word_listener")
    outside = src[:start] + src[end:]
    assert "create_task(wake_word_listener(" not in outside, (
        "wake_word_listener started with a bare create_task outside its "
        "supervisor — an exception would end it silently and nothing would "
        "restart it. Use _supervise_wake_listener()."
    )
    assert "_supervise_wake_listener(device)" in outside


def test_the_supervisor_attaches_a_done_callback():
    """Without one the task ends and no code ever learns that it did."""
    src = (CONTROLLER / "em_controller.py").read_text()
    body = src[src.index("def _supervise_wake_listener"):]
    body = body[:body.index("async def wake_word_listener")]
    assert "add_done_callback" in body
    # A restart that ignores cancellation would fight ordinary teardown.
    assert "cancelled()" in body
    # It must say so — silent recovery hides a recurring fault.
    assert "log.error" in body


def test_teardown_cancels_the_live_listener_not_a_stale_handle():
    """
    A supervised restart replaces the task object, so cancelling the variable
    captured at connect time would leave the live listener running against a
    closed connection.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "(device.oww_task or oww_task).cancel()" in src


def test_a_stuck_pause_can_be_recovered():
    """
    The other silent-deafness path. While oww_paused is set the wake loop
    routes every frame away and the no-frames watchdog stands down, so a flag
    that is never cleared is deafness with nothing to end it.

    Recovery is gated on voice_lock being free as well as time, because a long
    turn is legitimate — the spinner TTL alone runs to 135s — and only the
    combination is impossible.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "OWW_PAUSE_STUCK_S" in src
    assert "oww_paused_since" in src
    stuck = src[src.index("oww_paused stuck for") - 2000:
                src.index("oww_paused stuck for") + 500]
    assert "voice_lock.locked()" in stuck, (
        "the stuck-pause recovery must check that no turn holds the lock — "
        "time alone would cut a long but healthy turn"
    )


def test_deleting_a_device_bounces_its_link():
    """
    Link auth is decided ONCE, at register time, so deleting a device's row
    does nothing to the socket it is already on: it disappears from the
    dashboard and carries on serving turns, and only comes back as pending
    after something else drops the link — a reboot, a controller restart, a
    WiFi blip. From the front that reads as a delete that did not happen.

    The bounce must come AFTER the row is gone. The device redials in 5s
    (`control.go` Run), and a close issued first races that redial against
    the delete — it re-registers into the row being deleted and survives.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn  = _fn_body(src, "_delete_device")
    # Comments stripped: this one explains the ordering it is pinning, and a
    # test that reads its own explanation as the code can be satisfied by
    # prose alone.
    code = "\n".join(l for l in fn.splitlines() if not l.lstrip().startswith("#"))

    assert "_disconnect_device(device_id)" in code, (
        "a deleted device keeps running on its existing connection unless the "
        "control plane is closed"
    )
    assert code.index("db.delete_device") < code.index("_disconnect_device"), (
        "the row must be gone before the device is told to redial, or it "
        "re-registers into the row being deleted"
    )


def test_deleting_a_device_drops_its_satellite():
    """
    `device_disconnected` keeps the DeviceESPhomeServer across a disconnect on
    purpose — the port belongs to that device for good. So a delete that does
    not remove it leaves the old port sitting in `_servers`, and a re-added
    device meets it in `device_connected`, returns early on "already
    listening", and never reaches assign_esphome_port: it keeps the port it
    was being deleted to move OFF, under the previous row's label and MAC,
    while its new row reads esphome_api_port NULL and the dashboard shows no
    port at all.

    The BT proxy already had this (`em_ble_proxy.reconcile`); the satellite
    did not.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    fn  = _fn_body(src, "_delete_device")
    code = "\n".join(l for l in fn.splitlines() if not l.lstrip().startswith("#"))

    assert "em_esphome.device_deleted(device_id)" in code, (
        "a deleted device's satellite must be dropped, or its port outlives "
        "the row and the next registration inherits it"
    )

    esp = (CONTROLLER / "em_esphome.py").read_text()
    body = _fn_body(esp, "device_deleted")
    assert "_servers.pop(device_id" in body, (
        "stopping the listener is not enough — the registry entry is what "
        "device_connected finds and reuses"
    )


# ── The ring must say something when there is nothing to talk to ──────────────
#
# Source-shape, because the suite cannot import em_controller. A turn with no
# HA behind it ends in milliseconds, so without a cue the ring lights and
# clears too fast to register and the device reads as broken at exactly the
# moment it is working — the same failure ack_anim was added for, and the one
# the ESPHome port-collision incident presented as ("the wake word stopped
# working", while every part of it worked).

def test_a_dropped_turn_surfaces_its_outcome_to_the_ring():
    """
    `last_turn_outcome` is what `_leds_turn_end` reads. A completed turn sets
    it; a turn that never started has to as well, or the cleanup finds None
    and blacks the ring with no signal at all.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    fn  = _fn_body(src, "_record_dropped_turn")
    assert "device.last_turn_outcome" in fn, (
        "a turn dropped for no HA must tell the ring cleanup why, or the "
        "user gets a ring that flashes and clears"
    )


def test_a_device_with_no_ha_stands_down_before_it_can_claim():
    """
    Detection order is a PROXIMITY proxy: the nearest Echo crosses threshold
    first whether or not HA has ever dialled its satellite port. So an
    unlinked device must stand down before `_wake_arbiter.claim`, or it wins
    on nearness, silences the device that could have answered, and then dies
    no_ha — nothing answers, and the one that was ready is the one that went
    dark.

    Source-shape because the suite cannot import em_controller, and the
    ordering is the whole guard: a check placed after the claim would leave
    the claim taken.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    body = src[src.index("async def wake_word_listener"):]
    serves = body.index("can_serve_turn")
    claim  = body.index("_wake_arbiter.claim")
    assert serves < claim, (
        "the capability check must come BEFORE the arbitration claim — "
        "after it, the unlinked device has already taken the window"
    )
    guard = body[serves:claim]
    assert "if serves and" in guard, (
        "the claim itself must be gated on it, not merely preceded by it"
    )


def test_the_button_path_stands_down_the_same_way():
    """
    The button is what someone reaches for when the wake word appeared to do
    nothing, so answering it with silence is the worst version of this bug.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert src.count("esphome.can_serve_turn") >= 2, (
        "both the wake path and the button path must ask; a turn that cannot "
        "reach HA should never hold the voice lock"
    )


def test_the_no_ha_cue_does_not_depend_on_another_device_losing():
    """
    The cue reports the DEVICE's state, not a turn's outcome — it says the
    wake word works and the controller is here and HA is not. Firing it only
    when no other Echo took the utterance would make it disappear exactly on
    the multi-device fleets where the confusion is worst.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    body = src[src.index("async def wake_word_listener"):]
    stand = body.index("if not serves:")
    tail  = body[stand:stand + 2000]
    assert "_leds_turn_end(device)" in tail, "no cue on the stand-down path"
    assert "record_dropped_wake" in tail, (
        "the wake must still reach the activity history, or an HA outage is "
        "indistinguishable from a device that heard nothing"
    )


def test_every_outcome_cue_names_a_scene_key_that_exists():
    """
    `device.led_scene.get(key)` falls through to a dark ring when the key is
    wrong, so a typo here costs the cue silently — and only on the outcome
    that is already the unusual one.
    """
    import em_scenes
    src = (CONTROLLER / "em_controller.py").read_text()
    block = src[src.index("_OUTCOME_ANIM = {"):]
    block = block[:block.index("\n}")]
    keys = re.findall(r':\s*"(\w+_anim)"', block)
    assert keys, "no cue mappings found — did _OUTCOME_ANIM move?"
    scene = em_scenes.resolve({})
    for key in keys:
        assert key in scene, f"_OUTCOME_ANIM names {key}, which no scene defines"


def _strip_prose(src: str) -> str:
    """
    Drop comments and string literals from a source slice.

    A guard that greps for the thing it forbids finds the comment explaining
    it and passes anyway — three separate tests in this tree have done exactly
    that. The prose below this function's subject is unusually chatty about
    locks, so strip it before asserting on code.
    """
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"#.*", "", src)
    return src


def test_firmware_updates_are_serialised_across_the_whole_controller():
    """
    Three concurrent OTAs stalled the event loop for 11.1 seconds (measured
    2026-09-02, `[loop] event loop stalled` in the GA log) — and that loop is
    what sends speaker periods and LED frames, so a device answering someone
    pays for a device being updated.

    `_updates_in_progress` cannot prevent it: it stops ONE device being
    updated twice and says nothing about two devices at once. The serialiser
    has to be a global lock taken inside `_run_update`, so both entry points —
    the fleet deploy and a hand-clicked single update — go through it.
    """
    src  = (CONTROLLER / "em_api.py").read_text()
    body = _strip_prose(_fn_body(src, "_run_update"))

    assert "_ota_lock.acquire()" in body, (
        "_run_update must take the global OTA lock — a per-device guard does "
        "not stop two devices updating at once"
    )
    assert "_ota_lock.release()" in body, "the lock must be released"

    # Nothing may reach the device before the lock is held, or the serialising
    # is decorative: the transfer is the expensive part, and it all lives
    # inside _run_update_locked.
    acquire = body.index("_ota_lock.acquire()")
    work    = body.index("_run_update_locked")
    assert acquire < work, (
        "the lock must be acquired BEFORE the update work starts"
    )
    inner = _strip_prose(_fn_body(src, "_run_update_locked"))
    assert "_stream_binary_to_slot" in inner, (
        "the transfer must live inside the locked half"
    )
    assert "_ota_lock" not in inner, (
        "the locked half must not touch the lock — one owner, or a release "
        "on a path that never acquired frees somebody else's turn"
    )

    # Bounded, or one wedged device holds the whole fleet's queue: the base64
    # send loop has no timeout of its own.
    assert "OTA_MAX_HOLD_S" in body and "wait_for" in body, (
        "the locked half must run under a timeout — serialising turns a "
        "device-local stall into a fleet-wide one"
    )

    # Released in the finally, not on the success path — an update that raises
    # would otherwise hold the lock for the life of the process and no device
    # could ever be updated again without a restart.
    tail = body[body.rindex("finally:"):]
    assert "_ota_lock.release()" in tail, (
        "release the lock in the finally — a failed update must not strand "
        "every later one behind it"
    )


def test_a_queued_update_is_reported_as_queued_not_as_in_progress():
    """
    Serialising means a device can be started and not yet touched. Reporting
    that as `update_in_progress` claims a transfer that has not begun, which
    is the same failure as any control that appears to work and does not.
    """
    src = (CONTROLLER / "em_api.py").read_text()
    assert '"update_queued":' in src, (
        "/api/devices must expose update_queued, or the dashboard cannot tell "
        "waiting from working"
    )

    body = _strip_prose(_fn_body(src, "_run_update"))
    add     = body.index("_updates_queued.add")
    acquire = body.index("_ota_lock.acquire()")
    assert add < acquire, "a device must be marked queued BEFORE it waits"
    assert "_updates_queued.discard" in body, (
        "queued state must be cleared once the lock is held, or a device "
        "reads as queued for the whole of its own update"
    )

    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "update_queued" in jsx, (
        "the fleet deploy modal must render the queued state it is sent"
    )
