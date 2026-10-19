"""
How a device is presented: the record /api/devices and the live events
WebSocket send for each Echo, merged from its database row and its live
connection, plus the two pieces of state that outlive a connection — a WiFi
change in flight and the last link refusal.
"""

from __future__ import annotations

import json
import logging
import time

import em_ble_proxy
import em_config_sections as sections_mod
import em_db as db
import em_emos_ota
import em_emos_update
import em_esphome
import em_health
import em_ota
import em_pairing
import em_releases
import em_state
import em_volume
from version import firmware_update as _firmware_update

log = logging.getLogger("echomuse.api")


# WiFi change state per device_id — {"pending": {...}|None, "last_result":
# {...}|None}. Deliberately NOT on the live Device object: the connection
# (and with it the Device) dies when the network switches, and the outcome
# arrives on the replacement connection. In-memory only — a controller
# restart mid-change just means the result event is lost, not the change
# itself (the device self-manages commit/rollback).
_wifi_states: dict[str, dict] = {}


# A change whose result never arrived (device bricked its network AND
# rollback failed, or controller restarted) must not block retries forever.
_WIFI_PENDING_TTL = 240  # device gates total ≤ ~135s + margin


def wifi_state(device_id: str) -> dict:
    """Current wifi change state for a device, with stale pending expiry."""
    st = _wifi_states.setdefault(device_id, {"pending": None, "last_result": None})
    pending = st.get("pending")
    if pending and time.time() - pending["started_at"] > _WIFI_PENDING_TTL:
        st["pending"] = None
        st["last_result"] = {
            "ok": False, "ssid": pending["ssid"],
            "error": "no result from device — change timed out (device may "
                     "be offline, or its rollback failed)",
            "at": time.time(),
        }
    return st


def wifi_record_result(device_id: str, ok: bool, ssid: str, error: str
                       ) -> tuple[dict, bool]:
    """
    Store a wifi_result reported by the device.

    Returns (state, duplicate). The device re-sends its result until the
    wifi_commit ack lands, so re-arrivals of the same outcome are flagged
    (duplicate=True) and don't refresh the timestamp — callers ack every
    arrival but log/record only the first.
    """
    st = wifi_state(device_id)
    last = st.get("last_result")
    if (last and last.get("ok") == ok and last.get("ssid") == ssid
            and last.get("error") == error and st.get("pending") is None):
        return st, True
    st["pending"] = None
    st["last_result"] = {"ok": ok, "ssid": ssid, "error": error, "at": time.time()}
    return st, False


def _listen_json(live) -> dict | None:
    view = getattr(live, "listen_view", None)
    if view is None:
        return None
    return {"state": view.state, "streams": view.streams, "reason": view.reason}


def _stored_volume(row):
    """Last-known volume as an HA 0..1 float, from the persisted config."""
    try:
        level = json.loads(row["config"] or "{}").get("startupVolume")
    except (json.JSONDecodeError, TypeError):
        return None
    if level is None:
        return None
    try:
        # float() first, deliberately: em_volume swallows bad input and
        # returns 0.0, which is the right answer on the audio path and the
        # wrong one here — this function's None means "not known", and a
        # corrupt stored value must not report as "silent".
        return em_volume.device_level_to_ha(float(level))
    except (TypeError, ValueError):
        return None


def _row_sections(row) -> list:
    """
    Overridden config sections from a device row, tolerant of a row that
    predates the v8 column or carries unparseable JSON — either way the safe
    reading is "overrides nothing", which shows the device as fleet-scoped
    rather than inventing overrides it does not have.
    """
    try:
        raw = row["config_sections"]
    except (IndexError, KeyError):
        return []
    try:
        return sections_mod.normalise(json.loads(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


# device_id -> {"reason", "at"}: the last link refusal for a device on record,
# until it next registers. In memory on purpose: a refused device retries every
# few seconds, so a restarted controller has it back within one retry.
_link_refusals: dict[str, dict] = {}


def note_link_refused(device_id: str, reason: str) -> None:
    _link_refusals[device_id] = {"reason": reason, "at": time.time()}


def clear_link_refused(device_id: str) -> None:
    _link_refusals.pop(device_id, None)


def _health_json(boot: dict | None) -> dict | None:
    if not boot:
        return None
    return {
        "emmc":       em_health.emmc_summary(boot),
        "bootReason": em_health.boot_summary(boot.get("boot_reason")),
        "bootAt":     boot.get("boot_at"),
        "wearDay":    boot.get("day"),
        "emmcPart":   " ".join(x for x in (boot.get("emmc_name"), boot.get("emmc_date")) if x) or None,
    }


def merge_device(row, boot: dict | None = None) -> dict:
    """
    Merge a DB device row with live in-memory state.

    DB row provides persistent fields (label, config, firmware_ver etc).
    Live em_state.devices dict provides transient state (connected, speaking,
    muted, listening, thinking).
    """
    device_id = row["device_id"]
    live = em_state.devices.get(device_id)
    return {
        # Persistent
        "device_id":          device_id,
        "label":              row["label"],
        "approved":           bool(row["approved"]),
        "ip":                 row["ip"],
        "firmware_ver":       row["firmware_ver"],
        "firmware_previous":  row["firmware_previous"],
        # Whether the offered release is newer than what this device runs,
        # by version rather than string (EA firmware on a GA controller is
        # ahead of the GA release, not behind it).
        "firmware_update":    _firmware_update(row["firmware_ver"],
                                               em_releases.firmware_version()),
        "first_seen":         row["first_seen"],
        "last_seen":          row["last_seen"],
        "config":             json.loads(row["config"] or "{}"),
        "config_sections":    _row_sections(row),
        # Compat view for older readers: no overridden sections == fleet.
        "use_global_config":  not _row_sections(row),
        "esphome_port":       row["esphome_api_port"],
        "ble_proxy_port":     row["ble_proxy_port"],
        # Live — defaults when device is not connected
        "connected":        live is not None,
        "speaking":         live.speaking  if live else False,
        "muted":            getattr(live, "muted",     False) if live else False,
        # Stored, not live (#286): an offline device reports HA's last choice.
        "wake_word":        db.get_wake_word_enabled(device_id),
        "listening":        getattr(live, "listening", False) if live else False,
        "thinking":         getattr(live, "thinking",  False) if live else False,
        "stats":            live.stats if live else None,
        # Control-plane round trip, controller-measured. The RF counters are
        # structurally zero on this hardware (the MTK driver populates
        # neither retries nor noise), so this is the only latency signal.
        "rttMs":            getattr(live, "rtt_last_ms", None) if live else None,
        # Link quality graded on packet loss from TCP's own retransmit
        # counters (em_tcp.MinuteStrip): a verdict over the last 10 minutes
        # and loss per minute for the last 30. Loss, not signal strength, is
        # what a user hears. None until measured, and when offline.
        "linkQuality":      (live.tcp_minutes.summary(time.time())
                             if live and getattr(live, "tcp_minutes", None) else None),
        # Volume is persisted device state, not config (see
        # em_config_sections.STATE_KEYS): the live level while connected,
        # otherwise the last one the device reported, so an offline device
        # still shows where it will come back.
        "volume":           (live.volume if live is not None
                             else _stored_volume(row)),
        # Controller-side BT proxy state — non-None only while the device's
        # bleProxyEnabled config has a proxy server instantiated.
        "bleProxy":         em_ble_proxy.get_status(device_id),
        # Controller-side voice satellite state: whether Home Assistant is
        # actually on the other end of this device's ESPHome port. Without
        # it a device HA has never connected to renders as idle, which is
        # the same thing a working device renders as (#349) — the wake word
        # fires, the ring lights, and the turn dies in milliseconds.
        "voiceSatellite":   em_esphome.get_status(device_id),
        # Device-link security: token issued (persistent) + whether the
        # current control connection came in over the TLS listener (live).
        "linkTokenIssued":  bool(row["token"]) if "token" in row.keys() else False,
        "linkTls":          getattr(live, "secure", False) if live else False,
        # Why the controller is turning this device away, while it is.
        "linkRefused":      _link_refusals.get(device_id),
        # The device's owner held its button and it is waiting for Approve.
        "pairRequest":      em_pairing.pending_request(device_id),
        # Q4 fix (2026-07-05 review): near-miss counter — same lifecycle as
        # the rest of this "Live" section (resets on reconnect, since it
        # lives on the per-connection Device object, not the DB row).
        # None for a privately listening Echo: this controller scores nothing
        # from it, and 0 would read as "measured, and none".
        "owwNearMisses":    (None if live and getattr(live, "private_listening", False)
                             else getattr(live, "oww_near_misses", 0) if live else 0),
        # What this firmware can be asked to do, by capability rather than by
        # version comparison. Drives whether the dashboard OFFERS on-device
        # scoring: a toggle that silently does nothing on old firmware is worse
        # than no toggle, because it looks like the feature is broken.
        "owwShadowCapable": getattr(live, "oww_shadow_capable", False) if live else False,
        # Separate from shadow: firmware in the field scores and reports
        # without being able to act on it, and offering those "on" produces a
        # device that never answers.
        "owwTriggerCapable": getattr(live, "oww_trigger_capable", False) if live else False,
        # Gates the dashboard's Identify button (#860); disabled with the reason, not silent.
        "ledAnimCapable":   getattr(live, "led_anim_capable", False) if live else False,
        # Firmware that asks to pair itself (the owner holds the button); for
        # older firmware on a plain link the dashboard offers Pair instead.
        "pairingCapable":   getattr(live, "pairing_capable", False) if live else False,
        # What this Echo is actually doing with its microphone
        # (docs/listening.md, em_listen.resolve) — the one source for every
        # privacy statement the dashboard makes. `streams` is true, false, or
        # null for not known yet, which must never be shown as private. Null
        # for an offline device: it is not sending anything, and what it will
        # do on reconnect is its own report to make.
        "owwLocalCapable": getattr(live, "oww_local_capable", False) if live else False,
        "listen":          _listen_json(live) if live else None,
        "wakeCueCapable": getattr(live, "wake_cue_capable", False) if live else False,
        "volumeCueCapable": getattr(live, "volume_cue_capable", False) if live else False,
        "wakeMicCapable": getattr(live, "wake_mic_capable", False) if live else False,
        "remoteVolumeArcCapable": getattr(live, "remote_volume_arc_capable", False) if live else False,
        "responseLevelCapable": getattr(live, "response_level_capable", False) if live else False,
        # Sendspin player (#89): whether the firmware has one, and its status
        # (no secrets; the pairing token is its own request).
        "sendspinCapable": getattr(live, "sendspin_capable", False) if live else False,
        "bleConnectCapable": getattr(live, "ble_connect_capable", False) if live else False,
        "sendspin":        getattr(live, "sendspin", None) if live else None,
        "audioMixCapable": getattr(live, "audio_mix_capable", False) if live else False,
        # Gates the AEC delay slider, which only means anything on the
        # software tap. Paired with aecRef because the capability says the
        # firmware KNOWS how to use a hardware reference and aecRef says
        # whether this board turned out to have one — a device can be
        # capable and still be running on the software tap.
        "aecHwRefCapable": getattr(live, "aec_hw_ref_capable", False) if live else False,
        "aecRef":          getattr(live, "aec_ref", None) if live else None,
        # Which userspace the device booted: "emos", "fireos", or null from
        # firmware that cannot say. Null is not FireOS — the wizard, the
        # support bundle and the payload reconcile all need to tell "Android"
        # apart from "not asked". Offline it falls back to the value stored at
        # its last registration (schema v21), so the dashboard's slug does not
        # vanish when a device does; a live report always wins.
        "baseOs":          (getattr(live, "base_os", None) if live else None)
                           or row["base_os"],
        # `uname -m` / `uname -r` from the register message, stored value when
        # offline (schema v23). Null from firmware that does not send them.
        "kernelArch":      (getattr(live, "kernel_arch", None) if live else None)
                           or row["kernel_arch"],
        "kernelRelease":   (getattr(live, "kernel_release", None) if live else None)
                           or row["kernel_release"],
        # Flash wear and how the current boot started (schema v28, em_health):
        # the stored last boot, so it survives the device going offline. Null
        # from firmware that does not report them.
        "health":          _health_json(boot),
        # The DERIVED answer, not a second copy of the rule. em_platform owns
        # "which payloads mean anything here"; a dashboard that re-derived it
        # from baseOs would be a mirror free to disagree with the server that
        # actually refuses. Defaults True with no device, so a disconnected
        # device shows the control as it always did.
        "androidUserspace": getattr(live, "android_userspace", True) if live else True,
        # Gates the tap-as-event toggle — see em_button.decide.
        "buttonHoldCapable": getattr(live, "button_hold_capable", False) if live else False,
        # Whether the device found its ambient light sensor. Reported so the
        # dashboard can tell "no sensor" apart from "sensor present, no reading
        # yet" — which is the question #90 had to be answered by hand, because
        # nothing on screen showed the lux value at all and the only way to
        # check was a support bundle.
        "ambientLightCapable":
            "ambient_light" in (getattr(live, "capabilities", []) or []) if live else False,
        # WiFi change state (survives the reconnect a change causes)
        "wifi":             wifi_state(device_id),
        # The emOS image it runs (schema v30), and whether the newest release
        # is an update for it — decided here so the panel and the endpoint
        # that refuses cannot disagree.
        "emosVersion":      row["emos_version"],
        "emosLatest":       em_releases.emos_latest_version() or None,
        "emosUpdateAvailable": em_emos_update.update_available(
            row["emos_version"], em_releases.emos_latest_version()),
        "emosOffer":        em_emos_update.offer(
            row["emos_version"], em_releases.emos_latest_version()),
        "emos_update_in_progress": device_id in em_emos_ota.in_progress,
        "emos_update_queued":      device_id in em_emos_ota.queued,
        "emos_update_stage":       em_emos_ota.stage.get(device_id),
        "emos_update_error":       em_emos_ota.errors.get(device_id),
        # Update state
        "update_in_progress": device_id in em_ota.in_progress,
        # Waiting on the global OTA lock — started, but nothing has been sent
        # to this device yet. Distinct from update_in_progress so the panel
        # does not report a transfer that has not begun.
        "update_queued":      device_id in em_ota.queued,
        # Last OTA/rollback failure (None when the last attempt succeeded or
        # none was made) — lets the dashboard show a terminal ✗ state instead
        # of "updating…" forever when an update aborts.
        "update_error":       em_ota.errors.get(device_id),
    }
