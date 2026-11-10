"""
Installing the on-device wake word runtime and models on an Echo, over the
shell plane.

The runtime and models are not in the firmware (see em_oww_assets, which
plans what a device needs and holds no I/O), so they have to be installed.
This is the field transport; the provisioning wizard pushes the same bytes
over ADB from the browser, which is far better suited to 15MB than the shell
plane, and both drive the same idempotent plan.

Installs queue behind em_ota.queue: every device reconnecting after an
upgrade that adds an asset would otherwise push ~14MB each at once, over the
transport where three concurrent OTAs stalled the event loop for 11.1s.
"""

from __future__ import annotations

import asyncio
import logging

import em_config_types
import em_db as db
import em_esphome
import em_events
import em_oww_assets
import em_ota
import em_shadow
import em_shell
import em_state

log = logging.getLogger("echomuse.api")

async def device_state(live) -> dict:
    """
    What is actually installed on a device, and how much room it has.

    One shell round trip. `md5sum` is asked for per file rather than with a
    glob so a missing directory yields an empty inventory rather than an
    error line that could be mistaken for one.
    """
    d = em_oww_assets.DEVICE_DIR
    out = await em_shell.run(live, (
        f'for f in {d}/*.so {d}/*.onnx; do '
        f'[ -f "$f" ] && echo "$(busybox md5sum "$f" | busybox cut -d\" \" -f1) '
        f'$(busybox stat -c %Y "$f") $f"; done; '
        f'echo "FREE $(busybox df -m /data | busybox tail -1)"'
    ), timeout=120.0)

    free_mb = None
    lines = []
    for line in out.splitlines():
        if line.startswith("FREE "):
            # The whole df row, parsed in Python: the available column's INDEX
            # is not stable (busybox wraps a long filesystem name onto its own
            # line) and an awk field number silently yielded "65%" here, which
            # read as no measurement and quietly disabled the space check.
            free_mb = em_oww_assets.parse_free_mb(line[5:])
        else:
            lines.append(line)
    return {
        "installed": em_oww_assets.parse_device_listing("\n".join(lines)),
        "free_mb": free_mb,
    }


def wanted_models(device_id: str) -> list[str]:
    """
    The models a device should carry, most important first.

    The configured one is pinned and therefore first. Nothing else is added:
    extra slots exist so that models a user has already installed survive a
    switch, not so the controller can push models nobody asked for.
    """
    cfg = db.get_effective_device_config(device_id) or {}
    model = (cfg.get("owwModel") or "").strip()
    return [model] if model else []


def hold_back_model(live, effective: dict):
    """
    Keep a NEW wake word off a device until it has the model for it.

    Returns (config_to_send, pending_model). `pending_model` is the model the
    device should end up on once the classifier is installed, or None when the
    change can go straight through.

    A device cannot score a wake word whose classifier it does not have. Under
    `owwOnDevice=on` the controller has stood down and no longer triggers on
    its behalf, so telling the device to use a model it lacks produced a device
    with NO wake word: nothing fired, nothing warned, and the dashboard
    reported it healthy (#191). Selecting a wake word a device was never
    provisioned with is an ordinary dashboard action.

    So the device is never told about the new model until the file is there.
    It keeps listening for its CURRENT wake word, on-device, the whole time —
    no silent fall back to controller-side scoring, which is a posture the user
    did not ask for and would not see. If the install fails, the device simply
    stays where it was.

    Only devices that actually score locally are held back. With
    `owwOnDevice=off` the controller does the scoring and the file on the
    device is irrelevant, so the change applies immediately — which is the
    common case, and it stays instant.

    Both the old and the NEW mode are consulted: turning on-device scoring on
    in the same save that changes the wake word would otherwise slip through
    on the strength of the old mode being "off".
    """
    new_model = (effective.get("owwModel") or "").strip()
    if not new_model or new_model == live.oww_model:
        return effective, None
    if not live.oww_shadow_capable:
        return effective, None

    was_local = live.oww_on_device != em_shadow.MODE_OFF
    now_local = em_shadow.normalise_mode(
        effective.get("owwOnDevice", live.oww_on_device)
    ) != em_shadow.MODE_OFF
    if not (was_local or now_local):
        return effective, None

    held = dict(effective)
    held["owwModel"] = live.oww_model
    return held, new_model


async def install_then_switch(device_id: str, model: str) -> None:
    """
    Install a wake word classifier, then move the device onto it.

    Background task: the push is a multi-megabyte shell-plane transfer over a
    link measured at 5-7% packet loss, and blocking the config save on it would
    time out the request without making anything safer. Nothing is degraded
    while it runs — the device is still on its previous wake word and still
    scoring locally.

    The sync is idempotent, so a device that already has the model completes in
    an md5 compare and the switch is effectively immediate.
    """
    live = em_state.devices.get(device_id)
    if live is None:
        return

    await em_events.push_log(
        device_id, "info", "controller",
        f"Installing wake word model {model} before switching to it"
    )
    try:
        result = await sync(live, device_id)
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    live = em_state.devices.get(device_id)
    if live is None:
        return

    if not result.get("ok"):
        # Deliberately leaves the device where it was: on a wake word it can
        # actually hear. The controller is scoring the NEW model (fleet config
        # decides that), so the two disagree until this is resolved — worth
        # saying loudly, and better than a device that hears nothing.
        await em_events.push_log(
            device_id, "error", "controller",
            f"Could not install wake word model {model} "
            f"({result.get('error')}) — this device is still using "
            f"{live.oww_model}"
        )
        log.error(f"[api] [{device_id}] wake word switch to {model} abandoned: "
                  f"{result.get('error')} — device left on {live.oww_model}")
        return

    effective = await asyncio.get_event_loop().run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if (effective.get("owwModel") or "").strip() != model:
        # Changed again while the push was running; that save owns the
        # outcome and has its own install task.
        log.info(f"[api] [{device_id}] wake word changed again during install "
                 f"— dropping the switch to {model}")
        return

    await live.send_control({"type": "config", **em_config_types.well_typed(device_id, effective)})
    live.oww_model = model
    await em_esphome.update_oww_model(device_id, model)
    await em_events.push_log(
        device_id, "info", "controller",
        f"Wake word model {model} installed — device switched"
    )
    log.info(f"[api] [{device_id}] wake word switched to {model} after install")


async def push_config(device_id: str, live) -> None:
    """
    Re-send the device its effective config, which is what makes it rebuild
    its scorer from what is now installed. The dashboard's install said
    "restart the device" for this, and the Echo stayed deaf until someone did
    (035T, 2026-10-24).
    """
    loop = asyncio.get_event_loop()
    effective = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    await live.send_control({"type": "config", **em_config_types.well_typed(device_id, effective)})


async def reconcile(device_id: str, live) -> bool:
    """
    On connect: make sure the device has every asset it should, whatever its
    wake word mode (em_oww_assets.reconcile_action).

    Every other install path runs while the device is connected — the wizard
    over ADB, `install_then_switch` on a config save, the Updates tab by hand.
    A device that was OFFLINE when its wake word changed has none of them: the
    connect handler pushes the effective config directly, so it is told to use
    a classifier it may never have received (#191), and a device on the
    controller's wake word used to be skipped entirely.

    Three rules:

    - **Failure to LOOK is not evidence of absence.** Any error reading the
      device's inventory changes nothing, so a shell plane that is not up yet
      — likely, moments after connect — costs nothing.
    - **Repair, never switch.** A device missing its selected model is left in
      its mode (it answers the button) and warned about; once the model is in,
      a config push rebuilds its scorer. It is never moved to the controller's
      wake word — see em_shadow.effective_mode.
    - **Quiet when there is nothing to do.** Devices on this fleet reconnect
      often, so the ordinary path is one shell round trip and no log line.

    Runs as a background task: it is a shell round trip and possibly a
    multi-megabyte push over a link measured at 5-7% packet loss, and nothing
    about the connect handshake should wait on it.

    Returns whether the device is now as it should be, so em_reconcile can
    retry a run that was cut off rather than wait out its debounce.
    """
    loop = asyncio.get_event_loop()
    effective = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    # Firmware that cannot load a runtime can use none of this, and neither can
    # an Echo with no microphone it can read. The wake word MODE is
    # deliberately not checked: every device carries the full set, so a mode
    # switch never waits on an install (em_oww_assets.reconcile_action).
    if not live.oww_shadow_capable or not live.mic_capable:
        return True
    mode_off = em_shadow.normalise_mode(effective.get("owwOnDevice")) == em_shadow.MODE_OFF

    desired, _ = em_oww_assets.desired_assets(wanted_models(device_id))
    try:
        state = await device_state(live)
    except Exception as e:
        log.info(f"[api] [{device_id}] oww reconcile: could not read the device "
                 f"({e}) — leaving it as configured")
        return False

    missing = em_oww_assets.missing_selected_classifier(desired, state["installed"])
    gaps = em_oww_assets.missing_assets(desired, state["installed"])
    action = em_oww_assets.reconcile_action(mode_off, missing, gaps)
    if action == "none":
        return True

    # The mode is never changed here: a device missing its model keeps it,
    # answers the button, and hears its wake word again once this installs.
    if action == "deaf":
        while_missing = ("this Echo cannot hear its wake word until it installs "
                         "(the button still works)")
        log.warning(f"[api] [{device_id}] oww reconcile: {missing} is not "
                    f"installed — {while_missing}")
        await em_events.push_log(device_id, "warn", "controller",
                              f"Wake word model {missing} is missing — {while_missing}")
    else:
        # Nothing is deaf, so no warning is owed — but anything missing is a
        # device that cannot switch mode, change wake word or gate on Silero
        # tomorrow. Repair quietly; see missing_assets.
        log.info(f"[api] [{device_id}] oww reconcile: {len(gaps)} asset(s) "
                 f"missing ({', '.join(gaps)}) — installing")

    try:
        result = await sync(live, device_id)
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    live = em_state.devices.get(device_id)
    if live is None:
        return False

    if not result.get("ok"):
        if action == "deaf":
            await em_events.push_log(
                device_id, "error", "controller",
                f"Could not install {missing} ({result.get('error')}) — "
                f"this Echo still cannot hear its wake word"
            )
            log.error(f"[api] [{device_id}] oww reconcile: install failed "
                      f"({result.get('error')})")
        else:
            # Not an error event: the device is working and the user has lost
            # nothing today. A log line is the right weight.
            log.warning(f"[api] [{device_id}] oww reconcile: could not install "
                        f"the missing assets ({result.get('error')})")
        return False

    # The device builds its scorer from the config push, so it needs telling
    # when what it builds from has changed: a classifier it was deaf without
    # (the mechanism install_then_switch relies on), or the streaming
    # embedding model, which a running scorer otherwise only picks up at the
    # next restart (#893).
    stream_arrived = em_oww_assets.STREAM_NAME in (result.get("pushed") or [])
    if action == "deaf" or stream_arrived:
        await live.send_control({"type": "config", **em_config_types.well_typed(device_id, effective)})
    if action == "deaf":
        await em_events.push_log(
            device_id, "info", "controller",
            f"Wake word model {missing} installed — listening for its wake word again"
        )
        log.info(f"[api] [{device_id}] oww reconcile: {missing} installed")
    return True


async def sync(live, device_id: str, progress=None) -> dict:
    """
    Make a device's asset directory match what it needs, queued behind every
    other multi-megabyte transfer on this controller.

    Asset installs share `em_ota.queue` with firmware updates. On an upgrade that
    adds an asset, every device reconnects at once and each would start its
    own push — ~14MB each for a device that never had the runtime, which is
    most of an existing fleet on the controller's wake word (every device
    carries the full set since 2026-09-22). Three concurrent OTAs over the same
    transport stalled the event loop 11.1s (2026-09-02), and that loop feeds
    speaker periods. Bounded by em_ota.MAX_HOLD_S for the OTA path's reason: a
    device that stops reading must not hold the queue for good.
    """
    if em_ota.queue.locked():
        log.info(f"[api] [{device_id}] oww assets: queued behind another transfer")
        if progress:
            await progress("info", "waiting for another device's transfer…")
    async with em_ota.queue:
        try:
            return await asyncio.wait_for(
                _sync_locked(live, device_id, progress),
                timeout=em_ota.MAX_HOLD_S,
            )
        except asyncio.TimeoutError:
            return {"ok": False,
                    "error": f"abandoned after {em_ota.MAX_HOLD_S:.0f}s so the queue could continue"}


async def _sync_locked(live, device_id: str, progress=None) -> dict:
    """
    Make a device's asset directory match what it needs. Idempotent. Run with
    `em_ota.queue` held — call sync.

    Push to `.part` then rename only once md5 matches, so an interrupted
    transfer can never leave a file the device would try to dlopen. md5 is
    the ONLY definition of success — the shell transport has produced files
    of the right length and the wrong content, and a corrupt
    libonnxruntime.so fails at dlopen with an error that names nothing.
    """
    async def say(level: str, msg: str):
        log.info(f"[api] [{device_id}] oww assets: {msg}")
        await em_events.push_log(device_id, level, "controller", f"Wake word assets: {msg}")
        if progress:
            await progress(level, msg)

    desired, problems = em_oww_assets.desired_assets(wanted_models(device_id))
    for p in problems:
        await say("warn", p)
    if not desired:
        return {"ok": False, "error": "; ".join(problems) or "nothing to install"}

    state = await device_state(live)
    plan = em_oww_assets.plan_sync(desired, state["installed"], state["free_mb"])

    if plan.blocked:
        await say("error", plan.blocked)
        return {"ok": False, "error": plan.blocked}

    if plan.is_noop:
        await say("info", "already up to date")
        return {"ok": True, "pushed": [], "pruned": [], "problems": problems}

    d = em_oww_assets.DEVICE_DIR
    await em_shell.run(live, f"mkdir -p {d}")

    pushed = []
    for asset in plan.push:
        mb = asset.size / (1024 * 1024)
        await say("info", f"sending {asset.name} ({mb:.1f}MB)…")
        try:
            data = asset.source.read_bytes()
        except OSError as e:
            await say("error", f"{asset.name} unreadable on the controller: {e}")
            return {"ok": False, "error": f"{asset.name}: {e}"}

        dest = em_oww_assets.device_path(asset.name)
        part = f"{dest}.part"
        # NOT `pushed` — that is the accumulator above, and assigning the
        # transfer result to it shadowed the list on the first file, so the
        # append below raised AttributeError and asset installation failed
        # outright. em_shell.TransferResult is truthy-compatible, which is what let
        # this reach a release: every `if not …` call site kept working and
        # only the one that treated it as a list broke.
        sent = await em_shell.stream_file(live, data, part, mode="644")
        if not sent:
            await say("error", f"{asset.name} transfer failed: {sent}")
            return {"ok": False, "error": f"{asset.name}: {sent}"}

        res = await em_shell.run(live, (
            f'GOT=$(busybox md5sum {part} | busybox cut -d" " -f1); '
            f'if [ "$GOT" = "{asset.md5}" ]; then mv {part} {dest} && '
            f'chmod 644 {dest} && echo OK; else rm -f {part}; echo "BAD:$GOT"; fi'
        ), timeout=120.0)
        if "OK" not in res:
            await say("error", f"{asset.name} verify failed ({res.strip() or 'no output'})")
            return {"ok": False, "error": f"{asset.name}: md5 mismatch"}
        pushed.append(asset.name)

    for name in plan.prune:
        await em_shell.run(live, f"rm -f {em_oww_assets.device_path(name)}")
    if plan.prune:
        await say("info", f"removed unused: {', '.join(plan.prune)}")

    # Touch the selected classifier so LRU eviction sees it as most recent
    # even on a sync that did not need to re-push it.
    sel = next((a for a in desired if a.kind == "classifier"), None)
    if sel:
        await em_shell.run(live, f"touch {em_oww_assets.device_path(sel.name)}")

    # No restart is owed: the device rebuilds its scorer from a config push,
    # sent by whichever caller installed something it scores with
    # (reconcile, install_then_switch, the dashboard's install via push_config).
    await say("info", f"installed {len(pushed)} file(s)")
    return {"ok": True, "pushed": pushed, "pruned": plan.prune, "problems": problems}
