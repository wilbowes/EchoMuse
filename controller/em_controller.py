"""
EchoMuse Controller
===================

WebSocket server. Echo Dot devices connect via mDNS discovery.

mDNS advertisement is handled internally — no separate container required.

Architecture:
- Advertise _emcontroller._tcp on SERVER_PORT (zeroconf, host network)
- Devices open THREE connections:
    /control — JSON control plane (buttons, LEDs, mic_start/stop, ping,
                                   register, config, log, pending)
    /data    — binary data plane (mic PCM frames in, speaker PCM frames out)
    /shell   — raw binary stdin/stdout (demand-opened for shell sessions
                                        and OTA binary transfer)
- HTTP API and dashboard SPA served by aiohttp on API_PORT

Device WebSocket protocol:
  /control — Device → Server:
    {"type": "register", "device_id": "G0K0XXXXXXXX", "ip": "...",
     "version": "v2.0.1", "capabilities": [...]}
    {"type": "button", "clickType": 138, "down": false}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "playback_stats", "periods": 123, "underruns": 0}
    {"type": "listen_state", "state": "local", "reason": ""}
    {"type": "oww_wake", "score": .9, "threshold": .5, "ageMs": 80,
     "session": 7, "floor": .002, "barge": false}   (docs/listening.md)
    {"type": "listen_end", "session": 7, "reason": "ack_timeout"}
    {"type": "pong"}

  /control — Server → Device:
    {"type": "ack",     "device_id": "...", "features": [...]}
    {"type": "pending"}
    {"type": "config",  "adcDigitalGain": 100, ...}
    {"type": "leds",    "leds": [...]}
    {"type": "mic_start"}
    {"type": "mic_stop"}
    {"type": "listen_ack",   "session": 7}
    {"type": "listen_close", "session": 7, "reason": "end_of_speech"}
    {"type": "ping"}

  /data — Device → Server:
    <binary> [0x01][seq_hi][seq_lo][PCM mono S16_LE 2560 bytes]
    <binary> [0x06][JSON {"adverts": [...]}]   (only if ble_adverts_data)
    <binary> [0x07][session u32][seq u16][PCM]  (only if listen_session)

  /data — Server → Device:
    <binary> [0x02][PCM mono S16_LE 48kHz — 4096 bytes per period]
    <binary> [0x03] end of audio stream

  /shell — bidirectional raw binary (demand-opened by device on
           receipt of shell_open control message — not yet implemented
           in this revision; shell connections come inbound from the
           Go binary to the controller's /shell/{device_id} path)
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import struct
import time

from aiohttp import web
from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_db as db
import em_events
import em_credentials
import em_process_stats
import em_reconcile
from version import VERSION as CONTROLLER_VERSION
import em_releases
import em_state
import em_auth as auth
import em_api as api
import em_pki
import em_hostip
import em_pairing
import em_config_types
import em_dbwriter
import em_tasks
import em_device
import em_platform
import em_boards
import em_tcp
import em_wsclose
import em_eq
import em_limiter
import em_mbc
import em_scenes
import em_shadow
import em_wire
import em_wakeword
import em_esphome as esphome
import em_ble_proxy
import em_player
import em_volume
import em_loglevel
import em_leds
import em_playback
import em_alarm
import em_link_gate
import em_turn
import em_wake_listen
import em_button_events
import em_planes
import em_control_handlers

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

# Any non-empty string is truthy in Python, so `os.environ.get("DEBUG")`
# alone turned debug logging ON for DEBUG=0 — and em_start.py renders a
# false add-on option as exactly that string, so an untouched "Debug
# logging" toggle would have shipped every add-on install at DEBUG level.
# Same `== "1"` convention as REQUIRE_DEVICE_TLS, widened to the word
# spellings because DEBUG went undocumented for long enough that a
# container user's .env may already say `true`.
DEBUG = os.environ.get("DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format=_LOG_FORMAT,
)
log = logging.getLogger("echomuse")

# Keep the last few hundred lines in memory so a support bundle can carry the
# controller's own log, not just the relayed per-device one.
em_process_stats.install_log_ring(_LOG_FORMAT)

# websockets' own logger stays quiet — its per-connection chatter is not
# useful — but that ALSO silenced its account of why a connection closed,
# and the handlers below discarded the exception carrying the close frames.
# em_wsclose renders that ourselves, so the reason survives the silence.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

# aiohttp's per-request access log is the dashboard polling itself — 65% of
# measured log volume (em_support.py) — and drowns out actual events at INFO.
# Respect DEBUG rather than silencing unconditionally, same as the rest of
# this module's log level.
logging.getLogger("aiohttp.access").setLevel(logging.INFO if DEBUG else logging.WARNING)

# LOG_LEVELS="echomuse.esphome=DEBUG,aiohttp.access=INFO" — per-logger
# overrides on top of the global level, applied after the two defaults above
# so a specific setting can supersede them. `.get(..., "")` rather than a
# truthiness test for the reason DEBUG needed one: every non-empty string is
# truthy in Python, and em_start.py writes a false add-on option as exactly
# the string "0". A bad pair warns and is skipped (em_loglevel), never fatal
# — a typo in a diagnostics setting must not refuse to boot a controller.
_overrides = em_loglevel.apply(os.environ.get("LOG_LEVELS", ""))
for _problem in _overrides.problems:
    log.warning("LOG_LEVELS: %s", _problem)
if _overrides.levels:
    log.info("Log levels: " + ", ".join(
        f"{name}={logging.getLevelName(level)}"
        for name, level in _overrides.levels.items()))


# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
# Device-link TLS listener (wss) — same three WS planes as SERVER_PORT,
# wrapped in TLS with the em_pki-generated cert. 0 disables. Devices pick
# it up from the tls_port mDNS TXT property and dial wss iff they hold the
# pushed CA file (see device/internal/client/tlscreds.go).
SERVER_TLS_PORT = int(os.environ.get("SERVER_TLS_PORT", "8770"))
API_PORT     = int(os.environ.get("API_PORT", "8768"))
# The address devices are told to dial. Detected from the routing table when
# unset — never a literal, which used to send every unconfigured deployment
# to a developer's own machine. See em_hostip.
SERVER_IP    = em_hostip.server_ip(os.environ.get("SERVER_IP"))
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")
DB_PATH      = os.environ.get("DB_PATH", "echomuse.db")

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")


PING_INTERVAL_SEC = 5.0


# Outstanding pings older than this are abandoned — a reply that late is not
# a latency measurement, it is a lost packet, and keeping them would grow
# ping_sent without bound across a long disconnect.
PING_TIMEOUT_SEC = 60.0


# mDNS re-registration interval — keeps IGMP membership alive on the LAN
MDNS_REFRESH_INTERVAL = 120


# Volume conversion lives in em_volume so the scale has ONE definition and a
# test — it used to be spelled `/ 175` in three separate modules.
VOLUME_MAX_DEVICE = em_volume.DEVICE_VOLUME_MAX
_device_level_to_ha = em_volume.device_level_to_ha
_ha_volume_to_device = em_volume.ha_volume_to_device

# ─── Device registry ──────────────────────────────────────────────────────────


# #315: the control plane gets the same treatment the data plane has had —
# a device whose control connection drops during a controller restart, an
# add-on update or a link excursion keeps its ESPHome entities, BLE proxy
# and media session for this long before they are released. The device's
# own reconnect (endpoint pinned, per #106) lands well inside the window.
CONTROL_RECONNECT_GRACE_S = 5.0


async def _send_gatt(device_id: str, payload: bytes) -> bool:
    """em_ble_proxy's way to a device's data plane, by id."""
    device = em_state.devices.get(device_id)
    return device is not None and await device.send_gatt(payload)


def get_device(device_id: str) -> em_device.Device | None:
    return em_state.devices.get(device_id)


# ─── Audio conversion ─────────────────────────────────────────────────────────

# (The numpy linear-interpolation resample_to_48k that used to live here is
# gone: em_tts_audio.fetch now decodes at em_wire.SPEAKER_RATE directly, with ffmpeg
# doing any rate conversion — and HA transcodes to 48kHz at source when it
# honours the media player's declared supported_formats.)


# ─── Voice pipeline ───────────────────────────────────────────────────────────


# ── Timer alarm ──────────────────────────────────────────────────────────────
# HA owns the countdown (em_esphome advertises the TIMERS capability and folds
# VoiceAssistantTimerEventResponse events into a per-device registry). When a
# timer finishes, the registry asks us to ring; we loop a chime over the same
# speaker plane a voice response uses and pulse the ring, until the alarm is
# dismissed or a safety cap fires. Controller-side and firmware-free.
#
# Three ways to dismiss, and all three are LOCAL, because HA discards a timer
# the moment it finishes and cannot cancel one that is already ringing: a
# dot-button tap, the wake word followed by anything spoken
# (_dismiss_by_speech), or HA cancelling a still-RUNNING timer before it fires.


# ─── Wake word listener ───────────────────────────────────────────────────────


# ─── Button handler ───────────────────────────────────────────────────────────


# ─── Control plane handler ────────────────────────────────────────────────────


# ─── Control-plane messages ─────────────────────────────────────────────────
#
# One handler per message type a registered device sends on /control, looked up
# in CONTROL_HANDLERS by handle_control's message loop (#288). Each takes the
# device and the decoded message and nothing else, so each can be called on its
# own. `register` is not here: it runs before the loop and is fatal on failure.


async def _on_pair_request(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # The owner held the action button on a connected
    # device; an admin issues credentials with Approve.
    await api.notify_pair_request(device_id, "link")


CONTROL_HANDLERS = {
    "button":           em_button_events.handle_button_event,
    "ambient_light":    em_control_handlers.on_ambient_light,
    "mute_state":       em_control_handlers.on_mute_state,
    "volume_state":     em_control_handlers.on_volume_state,
    "stats":            em_control_handlers.on_stats,
    "sendspin_status":  em_control_handlers.on_sendspin_status,
    "sendspin_token":   em_control_handlers.on_sendspin_token,
    "wifi_result":      em_control_handlers.on_wifi_result,
    "playback_stats":   em_control_handlers.on_playback_stats,
    "playback_started": em_control_handlers.on_playback_started,
    "oww_shadow_cross": em_control_handlers.on_oww_shadow_cross,
    "oww_wake":         em_control_handlers.on_oww_wake,
    "listen_state":     em_control_handlers.on_listen_state,
    "listen_end":       em_control_handlers.on_listen_end,
    "ble_adverts":      em_control_handlers.on_ble_adverts,
    "wifi_scan_result": em_control_handlers.on_wifi_scan_result,
    "log":              em_control_handlers.on_log,
    "pair_request":     _on_pair_request,
    "pong":             em_control_handlers.on_pong,
}


async def handle_control(ws: WebSocketServerProtocol, secure: bool = False):
    """
    Handle a /control WebSocket connection from a device.
    """
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "register":
            log.warning(
                f"[control] First message from {remote} was not register — closing"
            )
            await ws.close()
            return

        device_id    = msg["device_id"]
        # Sent by firmware whose owner held the action button (em_pairing).
        pairing      = bool(msg.get("pairing"))
        loop         = asyncio.get_event_loop()

        if pairing and em_pairing.approved(device_id):
            # An admin approved this pairing. Rotate first: with no token on
            # record the device is admitted by em_linkauth's existing rules,
            # and issue_credentials mints its new one below.
            await loop.run_in_executor(None, db.clear_device_token, device_id)
            log.info(f"[control] {device_id}: pairing approved — admitting to issue credentials")

        if not await em_link_gate.link_auth_ok(ws, device_id, secure, "control"):
            # Said before closing, so the device can show "hold the button to
            # pair" rather than "no controller" (firmware with `pairing`;
            # older firmware ignores it). A pairing device is told pending.
            reply = {"type": "refused"}
            if pairing:
                await api.notify_pair_request(device_id, "plain")
                reply = {"type": "pending", "pairing": True}
            try:
                await ws.send(json.dumps(reply))
            except Exception:
                pass
            await ws.close()
            return
        ip           = msg.get("ip", str(remote[0]))
        version      = msg.get("version")
        capabilities = msg.get("capabilities", [])

        loop         = asyncio.get_event_loop()
        approval_mode = await loop.run_in_executor(
            None, db.get_config, "device_approval", DEVICE_APPROVAL
        )
        row          = await loop.run_in_executor(None, db.get_device, device_id)

        if row is None:
            if approval_mode == "auto":
                label = f"Unknown {device_id[:8]}"
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await loop.run_in_executor(
                    None, db.approve_device, device_id, label, None
                )
                log.info(
                    f"[control] Auto-approved new device: {device_id} "
                    f"label={label!r}"
                )
                row = await loop.run_in_executor(None, db.get_device, device_id)
            else:
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await ws.send(json.dumps({"type": "pending"}))
                log.info(
                    f"[control] Unknown device held as pending: {device_id} "
                    f"from {ip}"
                )
                await api.notify_device_pending(device_id, ip)
                em_dbwriter.submit(db.log_device,
                    device_id, "info", "controller",
                    f"Device seen for first time — pending approval ({ip})"
                )
                await ws.close()
                return

        if not row["approved"]:
            await loop.run_in_executor(
                None, db.upsert_device_seen, device_id, ip, version
            )
            await ws.send(json.dumps({"type": "pending"}))
            log.info(
                f"[control] Device pending approval: {device_id} from {ip}"
            )
            await api.notify_device_pending(device_id, ip)
            await ws.close()
            return

        await loop.run_in_executor(
            None, db.upsert_device_seen, device_id, ip, version
        )

        device = em_device.Device(device_id, ip, capabilities, ws)
        # Why the device has no ambient light sensor, when it has none. The
        # capability list says only that it is absent; without the reason,
        # an unfitted chip and an unbound driver are indistinguishable
        # remotely, and #90 needed a shell session on the user's own hardware
        # to tell them apart. Absent on firmware that does not send it, which
        # reads as "not reported" rather than as a fault.
        device.ambient_light_status = msg.get("ambient_light_status")
        # Static property of the boot, so it arrives with registration
        # rather than on the stats tick — em_reconcile.on_connect asks for it
        # immediately and a stats-borne value is ~30s too late.
        device._base_os = msg.get(em_platform.REGISTER_KEY)
        # Persisted as well as held live (schema v21). The live value answers
        # "what is THIS device", which is all payload gating ever needs; the
        # stored one answers "what is the fleet", which is a question about
        # devices that are mostly offline. Written on every register, because a
        # device reflashed between FireOS and emOS is the case it has to track.
        if device._base_os:
            em_dbwriter.submit(db.set_device_base_os, device_id, device._base_os)
        # Which board, for the model HA shows; before the satellite exists.
        if boards := em_boards.record(em_state.boards, device_id, msg.get("board")):
            em_dbwriter.submit(db.set_config, em_boards.CONFIG_KEY, boards)
        # The running kernel, so emOS on FireOS 5's 64-bit kernel and FireOS
        # 6's 32-bit one can be told apart (schema v23). Absent on older
        # firmware; stored only when reported so a known value is not erased.
        device.kernel_arch = msg.get("kernel_arch") or None
        device.kernel_release = msg.get("kernel_release") or None
        if device.kernel_arch:
            em_dbwriter.submit(db.set_device_kernel, device_id, device.kernel_arch, device.kernel_release or "")
        # How this boot started and the eMMC's wear (schema v28), one row per
        # boot_id: a device re-registers on every redial. Held live too, so
        # the dashboard shows them without a query. Absent on older firmware.
        device.boot_reason = msg.get("boot_reason") or None
        device.boot_id = msg["boot_id"] if isinstance(msg.get("boot_id"), str) and msg["boot_id"] else None
        if device.boot_id:
            em_dbwriter.submit(db.record_boot, device_id, device.boot_id,
                               msg.get("version"), device.boot_reason)
        em_control_handlers.note_wear(device, msg.get("emmc"))
        # Link-security telemetry for the dashboard: True when this control
        # connection arrived over the TLS listener.
        device.secure = secure
        # Hydrate the observability panel's turn history from the persistent
        # turns table so it survives controller and device restarts.
        try:
            past_turns = await loop.run_in_executor(
                None, db.get_turns, device_id, device.turn_history.maxlen
            )
            device.turn_history.extend(past_turns)
        except Exception as e:
            log.warning(f"[{device_id}] Turn history hydration failed: {e}")
        em_state.devices[device_id] = device

        log.info(
            f"[control] Device connected: {device_id} v={version} "
            f"at {ip} caps={capabilities}"
        )
        em_dbwriter.submit(db.log_device,
            device_id, "info", "controller",
            f"Connected from {ip} version={version}"
        )

        await device.send_control({
            "type": "ack",
            "device_id": device_id,
            "features": em_wire.CONTROLLER_FEATURES,
            # The device's wall clock. An Echo has no RTC that survives a
            # power cut and boots reading 2010; under emOS nothing ever
            # corrects it, because bionic resolves through Android's property
            # service so no bionic-linked binary there has DNS for an NTP
            # pool. Telling it over the link it already trusts costs one
            # integer on a message that already flows — against an NTP server
            # here, which would mean a listening socket, a second way for the
            # device to find us, and a daemon to supervise, for an accuracy
            # nothing in this system reads. Every measurement we take is
            # monotonic and the device never sends a timestamp.
            #
            # Not negotiated, and does not need to be: adding a field is safe
            # unnegotiated, older firmware ignores it, and a device that gets
            # no field leaves its clock alone.
            "time_ms": int(time.time() * 1000),
        })

        config = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        # A stored value of the wrong type fails the device's whole decode and
        # the conversions below; dropped, it reads as absent at both ends.
        config, bad_keys = em_config_types.drop_invalid(config)
        if bad_keys:
            log.warning(f"[control] {device_id}: stored config has values of "
                        f"the wrong type, not sent: {', '.join(bad_keys)}")
        # The Sendspin player advertises itself to Music Assistant under the
        # device's label, which the firmware does not otherwise know. Carried
        # on the same push, so the player starts under the right name rather
        # than restarting a moment later; a rename sends it alone.
        if device.sendspin_capable and row["label"]:
            config = {**config, "sendspinName": row["label"]}
        await device.send_control({"type": "config", **config})
        device.oww_threshold = float(config.get("owwThreshold", em_device.OWW_THRESHOLD))
        device.oww_model     = config.get("owwModel", f"{em_device.OWW_MODEL}_v0.1")
        device.wake_arb_ms   = int(config.get("wakeArbitrationMs", 300))
        device.mic_gain_db   = float(config.get("micGainDb", 24))
        device.adc_gains     = (int(config.get("adcMicpga", 40)),
                                int(config.get("adcDigitalGain", 88)))
        device.oww_speex_ns  = bool(config.get("owwSpeexNs", False))
        device.ns_asr        = bool(config.get("nsAsr", False))
        device.save_utterances = bool(config.get("saveUtterances", False))
        # Seeded before the wake listener starts, so its mic_start is skipped.
        device.wake_word_enabled = await loop.run_in_executor(
            None, db.get_wake_word_enabled, device_id
        )
        # Not a config key (em_db keeps it apart from the dashboard's copy),
        # so it is sent on its own. The device boots with it on.
        if device.wake_word_off_capable:
            await device.push_config(wakeWordEnabled=device.wake_word_enabled)
        device.wake_clip_capture = bool(config.get("wakeClipCapture", False))
        device.wake_clip_min_score = float(config.get("wakeClipMinScore", 0.20))
        device.stream_reply = bool(config.get("streamReply", False))
        device.barge_in_enabled = bool(config.get("bargeInEnabled", False))
        device.barge_threshold  = float(config.get("bargeInThreshold", 0.6))
        device.button_single_tap_event = bool(
            config.get("buttonSingleTapEvent", False)
        )
        device.button_multi_tap_ms = int(config.get("buttonMultiTapMs", 0))
        # See em_device_config.apply_live_config: consumed only on the controller-detected
        # wake path, mirrored in both places because that is the rule
        # test_config_mirrors.py enforces and the reason it exists.
        device.wake_sound = bool(config.get("wakeSound", False))
        # Resolved against the capability — see em_shadow.effective_mode for
        # why "on" against firmware that cannot trigger must become shadow
        # rather than being honoured.
        new_oww_on_device = em_shadow.effective_mode(
            config.get("owwOnDevice"), device.oww_trigger_capable,
        )
        if new_oww_on_device != device.oww_on_device:
            # #696 review: a buffered pre-roll from the old mode must not
            # carry into the new one.
            device.wake_capture.reset()
        device.oww_on_device = new_oww_on_device
        # Wake word assets, start script and debloat, reconciled against what
        # the device actually has — see em_reconcile.on_connect for why the
        # arrival is the trigger. Background: shell round trips and possibly a
        # multi-megabyte push, none of which the handshake should wait on.
        em_tasks.spawn(em_reconcile.on_connect(device_id, device))
        # An Echo without its own output chain is filtered here, which needs
        # scipy (em_eq._signal): import it in a worker thread now, so the
        # first stream does not stall the event loop on a ~2 s import.
        if not device.output_chain_on_device:
            em_tasks.spawn(asyncio.to_thread(em_eq.preload))
        # An approval (a new device, or an approved pairing) issues link
        # credentials unless this connection already has working ones.
        if em_pairing.approved(device_id):
            if secure and await em_link_gate.presented_its_token(ws, device_id):
                em_pairing.done(device_id)
            else:
                em_tasks.spawn(em_credentials.issue_credentials(device_id))
        elif pairing:
            # Connected (no credentials yet, or plain) and asking: offer it.
            await api.notify_pair_request(device_id, "link")
        device.eq_bands      = config.get("eqBands", [0.0] * 8)
        device.eq_loudness   = bool(config.get("eqLoudness", False))
        device.bass_guard_enabled = bool(config.get("bassGuardEnabled", True))
        device.bass_guard_db      = float(config.get(
            "bassGuardDb", em_mbc.DEFAULT_BASS_GUARD_DB))
        device.limiter_enabled   = bool(config.get("limiterEnabled", True))
        device.limiter_threshold = float(config.get(
            "limiterThreshold", em_limiter.DEFAULT_THRESHOLD_DB))
        device.limiter_release   = float(config.get(
            "limiterRelease", em_limiter.DEFAULT_RELEASE_MS))
        device.led_scene     = em_scenes.resolve(config)
        # #263: hand the device its current listening animation so a wake it
        # detected ITSELF can light the ring immediately, instead of waiting
        # for leds_listening to make the round trip (measured +522ms before
        # the ring moved; control-plane tail reaches 2s, #139). The
        # controller's frame still lands within an RTT and takes over — the
        # device just stops being two hops behind its own event.
        if device.led_anim_capable and device.led_scene.get("listening_anim"):
            await device.send_control(
                {"type": "config",
                 "listeningAnim": device.led_scene["listening_anim"]})
        # Initialise volume from stored config — device will report its real
        # value via volume_state on connect, but this seeds a sane default
        # in the window before that first message arrives.
        device.volume = _device_level_to_ha(
            int(config.get("startupVolume", 85))
        )
        log.info(f"[control] Config pushed to {device_id} (volume={device.volume:.3f})")

        await em_leds.leds_off(device)
        await api.notify_device_connected(device_id)
        _device_ref = device
        async def _standalone_play(pcm_bytes: bytes, _d=_device_ref) -> bool:
            # Same acoustic-feedback guard as voice turns: announcements
            # play outside a turn, so the always-on OWW stream is live —
            # stop it for the duration and put it back after. An active
            # media session pauses for the announcement and resumes.
            #
            # cancel_event is cleared first, exactly as a voice turn does.
            # It is set by a cancel (a button press during a turn, a mute)
            # and was ONLY ever cleared when the next voice turn started —
            # so a cancelled turn left it set and _run_post_turn_playback
            # then abandoned every subsequent announcement at "Cancelled
            # during playback", silently, until a turn happened to run.
            # Measured on Test Device 01 on 2026-08-17: a turn cancelled at
            # 12:02:32 killed the next seven announcements over three
            # minutes. An announcement is a new action and nothing that set
            # that flag earlier has any claim on it.
            _d.cancel_event.clear()
            await em_player.interrupt(_d.device_id)
            await _d.mic_stop()
            # The ring a spoken reply gets (#779): an announcement, and the
            # opening message of a conversation HA starts, played with the
            # ring dark. The length is known here, so one TTL covers it.
            ring = em_scenes.announcement_ring(
                capable=_d.led_anim_capable,
                turn_running=_d.voice_lock.locked(),
                alarm_ringing=_d.timer_alarm_ringing)
            if ring:
                meter = dict(_d.led_scene["meter_anim"])
                meter["ttlSec"] = em_scenes.meter_ttl(
                    len(pcm_bytes) / (em_wire.SPEAKER_RATE * 2))
                await _d.send_led_anim(meter)
            try:
                await em_playback.run_post_turn_playback(_d, pcm_bytes)
            finally:
                if ring:
                    await em_leds.leds_off(_d)
                await _d.mic_start()
                await em_player.resume_interrupted(_d.device_id)
            # Whether the audio actually reached the speaker. Something that
            # cancelled mid-playback (a mute, a button) means the user did
            # not hear it, and telling HA it finished successfully would be
            # untrue — it is the one thing the announcement reply reports.
            return not _d.cancel_event.is_set()
        async def _send_volume_set(level: int, _d=_device_ref) -> None:
            await _d.send_control({"type": "volume_set", "level": level})
        async def _ring_alarm(_d=_device_ref) -> None:
            await em_alarm.start_timer_alarm(_d)
        async def _stop_alarm(_d=_device_ref) -> None:
            await em_alarm.stop_timer_alarm(_d)
        async def _start_conversation(_d=_device_ref) -> None:
            # HA has finished asking; the answer is whatever is said next.
            # Same shape as the button turn — a deliberate act with no wake
            # word, so mic_stop/mic_start_turn to get the beam-locked stream,
            # and preroll_discard 0 because there is no wake-word tail to trim.
            #
            # The mute check is here rather than a refusal further up because
            # of what HA does with a refusal: `async_internal_ask_question`
            # awaits its answer future with NO timeout, so a satellite that
            # stays silent hangs the caller's script for good. Running the turn
            # keeps that bounded — the device rejects every mic_start while
            # muted (and the ADC is muted in hardware regardless), so nothing
            # is captured, the streaming phase gives up at its cap and HA ends
            # the run with no answer. The mute is not weakened by this; the
            # device is still the one enforcing it.
            if _d.muted:
                log.info(
                    f"[{_d.device_id}] start_conversation while muted — "
                    f"the microphone stays closed"
                )
                em_dbwriter.submit(db.log_device,
                    _d.device_id, "info", "controller",
                    "Home Assistant asked a question while the mic was muted"
                )
            _d.cancel_event.clear()
            _d.oww_paused.set()
            _d.oww_paused_since = asyncio.get_event_loop().time()
            try:
                await _d.mic_stop()
                await _d.mic_start_turn()
                await em_turn.run_voice_locked(
                    _d,
                    trigger_label=esphome.CONVERSATION_TRIGGER,
                    is_wakeword=False,
                )
            finally:
                # Back to the ch6 omni wake stream, exactly as the button turn
                # does — and for its reason: a turn that ended without TTS
                # leaves the beam-locked stream running, and a bare mic_start
                # would no-op against it.
                await _d.mic_stop()
                await _d.mic_start()
        def _set_wake_word(on: bool, _d=_device_ref) -> None:
            # HA's wake word picker (#286). State is set before returning,
            # because HA reads the configuration back straight after writing
            # it; the stored write is queued in order, the stream follows.
            mic_muted, enabled = esphome.get_mic_muted_and_wake_word(_d.device_id)
            if em_wakeword.decline_off(want=on,
                                       listening_locally=_d.listen_reported == "local",
                                       device_can=_d.wake_word_off_capable):
                log.info(f"[{_d.device_id}] Wake word off declined — this "
                         f"firmware would still send audio on each wake")
                return
            # #776: when the live value was turned on for a firmware that cannot
            # honour off, the stored value is still off. A request for "on" here
            # must still be stored, or the person's choice comes back as off
            # after the upgrade — they would have to set it twice. The live
            # value is already on, so on_request reports no change; this writes
            # the stored value and clears the temporary flag.
            if on and _d.wake_word_temporarily_on:
                _d.wake_word_temporarily_on = False
                em_dbwriter.submit(db.set_wake_word_enabled, _d.device_id, True)
                log.info(f"[{_d.device_id}] Wake word on stored — the temporary "
                         f"on is now the stored choice")
                return
            t = em_wakeword.on_request(want=on, enabled=enabled, mic_muted=mic_muted)
            if not t.changed:
                return
            _d.wake_word_enabled = t.enabled
            esphome.update_wake_word(_d.device_id, t.enabled)
            em_dbwriter.submit(db.set_wake_word_enabled, _d.device_id, t.enabled)
            log.info(f"[{_d.device_id}] Wake word {'on' if t.enabled else 'off'} (Home Assistant)")

            async def _follow() -> None:
                if _d.wake_word_off_capable:
                    await _d.push_config(wakeWordEnabled=t.enabled)
                if t.stop_stream:
                    await _d.mic_stop()
                if t.start_stream:
                    await _d.mic_start()
                await em_events.push({
                    "type":      "device_update",
                    "device_id": _d.device_id,
                    "state":     {"wake_word_enabled": t.enabled},
                })
            em_tasks.spawn(_follow())
        # Capabilities before the servers come up: they decide which HA
        # entities are advertised, and advertising is a one-shot at
        # ListEntities time.
        esphome.set_device_capabilities(device_id, capabilities)
        await esphome.device_connected(
            device_id,
            SERVER_HOST,
            standalone_play=_standalone_play,
            send_volume_set=_send_volume_set,
            ring_alarm=_ring_alarm,
            stop_alarm=_stop_alarm,
            start_conversation=_start_conversation,
            set_wake_word=_set_wake_word,
            wake_word_enabled=device.wake_word_enabled,
        )
        # #776: firmware that can honour the stored choice again (an upgrade)
        # may meet a Home Assistant still showing the temporary "on". Without
        # the capability the listen_state handler decides, once the Echo has
        # said how it is listening.
        if device.wake_word_off_capable:
            esphome.update_wake_word(device_id, device.wake_word_enabled, refresh=True)
        # A device boots at its stored startupVolume, which an output mute
        # never overwrites — so a mute from before this connection has to be
        # sent again, or HA shows muted over audible music.
        _remute = esphome.output_mute_on_reconnect(device_id)
        if _remute is not None:
            await _send_volume_set(_remute)
        # The ESPHome server object caches the OWW model from server
        # creation — refresh it from the config we just loaded so HA's
        # wake-word dropdown tracks dashboard changes across controller
        # restarts too.
        await esphome.update_oww_model(device_id, device.oww_model)
        # BT proxy: mark the device online (brings its proxy listener up if
        # enabled) and reconcile against current config — covers devices
        # approved or toggled while they were offline.
        await em_ble_proxy.device_connected(device_id, device.ble_connect_capable)
        await em_ble_proxy.reconcile(device_id)

        # ── Main message loop ─────────────────────────────────────────────

        async def ping_loop():
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                now = loop.time()
                # Abandon replies that never came — a very late pong is a
                # lost packet, not a latency sample.
                stale = [q for q, t in device.ping_sent.items()
                         if now - t > PING_TIMEOUT_SEC]
                for q in stale:
                    device.ping_sent.pop(q, None)
                device.ping_seq += 1
                seq = device.ping_seq
                device.ping_sent[seq] = now
                # Record busyness at SEND time: the discriminator is whether
                # the device was doing anything when the probe went out, and
                # by the time the reply lands a turn may have started or
                # ended.
                device.ping_busy[seq] = device.is_busy()
                await device.send_control({"type": "ping", "id": seq})

        ping_task = asyncio.create_task(ping_loop())
        oww_task  = em_wake_listen.supervise_wake_listener(device)
        device.oww_task = oww_task

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                msg_type = msg.get("type")

                # One bad message must not cost the connection (#255):
                # the LEAST important message in the protocol used to be
                # able to disconnect the device - a missing attribute while
                # handling a playback_stats telemetry report took the
                # ESPHome satellite, the BLE proxy and the data plane down
                # with it, and reconnected on every barge-in. A handler
                # failure now logs message type + traceback and moves on.
                # Deliberate exceptions:
                #   - CancelledError propagates: teardown is not a message
                #     fault.
                #   - register runs BEFORE this loop and stays fatal -
                #     arriving into an unknown state is different from
                #     failing to record a statistic.
                try:
                    handler = CONTROL_HANDLERS.get(msg_type)
                    if handler is None:
                        log.debug(
                            f"[{device_id}] Unknown control message: {msg_type}"
                        )
                    else:
                        await handler(device, msg)

                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        f"[{device_id}] Control handler failed for "
                        f"{msg_type!r} - message dropped, connection kept")
        finally:
            ping_task.cancel()
            # device.oww_task, not the local: a supervised restart replaces
            # the task object, and cancelling the stale one would leave the
            # live listener running against a closed connection.
            (device.oww_task or oww_task).cancel()

    except asyncio.TimeoutError:
        log.warning(f"[control] Registration timeout from {remote}")

    except websockets.exceptions.ConnectionClosed as e:
        _close_why = em_wsclose.from_exception(e)
        _msg = f"[control] Connection closed: {em_wsclose.describe(*_close_why)}"
        if em_wsclose.is_routine(_close_why[0], _close_why[2]):
            log.info(_msg)
        else:
            log.warning(_msg)

    except Exception as e:
        log.error(f"[control] Handler error: {e}")

    finally:
        if device:
            # Above the stale check on purpose: per-connection state, and
            # send_button_event resolves by device_id, so an orphaned timer
            # would fire a phantom tap at the replacement connection.
            device.tap_burst.cancel()
            # Per-connection: this task belongs to this Device object, so
            # cancelling it can never touch a live replacement connection's
            # ring (unlike the shared services guarded below).
            if device.timer_alarm_task is not None:
                device.timer_alarm_task.cancel()
                device.timer_alarm_task = None
            if em_state.devices.get(device.device_id) is not device:
                # A replacement connection has already registered for this
                # device_id — this socket is stale. Tearing down shared
                # per-device services here would rip them out from under the
                # live connection: on 2026-07-14 a stale close 4s after a
                # reconnect stopped Lounge's ESPHome listener, so HA's
                # redials hit connection-refused and every turn failed
                # no_ha for 11 hours until the next device bounce.
                log.info(
                    f"[control] Stale connection closed for "
                    f"{device.device_id} — replacement is active, keeping "
                    f"services up"
                )
            else:
                log.info(f"[control] Device disconnected: {device.device_id}")
                em_dbwriter.submit(db.log_device,
                    device.device_id, "info", "controller", "Disconnected"
                )
                # Stamp the moment it went away, so "last seen" is exact for
                # an offline device rather than up to one stats report stale.
                em_dbwriter.submit(db.touch_device_seen, device.device_id)
                em_state.devices.pop(device.device_id, None)
                # #315: the services stay up for a grace window instead of
                # being torn down immediately — a four-second link blip used
                # to deregister the HA entities, drop the BLE proxy and kill
                # the media session, then rebuild all of it when the device
                # returned on its own.
                em_tasks.spawn(_release_device_services(device))


async def _release_device_services(device) -> None:
    """
    Release a device's services CONTROL_RECONNECT_GRACE_S after its control
    connection closed — unless a replacement connection registered in the
    meantime (#315). Mirrors the data plane's DATA_RECONNECT_GRACE_S.

    esphome.device_connected() is idempotent (a still-listening port is
    kept and only the callbacks are refreshed), so holding services
    through a blip costs nothing on reconnect.
    """
    try:
        await asyncio.sleep(CONTROL_RECONNECT_GRACE_S)
        current = em_state.devices.get(device.device_id)
        if current is not None and current is not device:
            return  # replacement is live; it owns the services now
        log.info(
            f"[{device.device_id}] control connection not restored within "
            f"{CONTROL_RECONNECT_GRACE_S:.0f}s — releasing services"
        )
        await api.notify_device_disconnected(device.device_id)
        await esphome.device_disconnected(device.device_id)
        await em_ble_proxy.device_disconnected(device.device_id)
        em_player.device_gone(device.device_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"[{device.device_id}] delayed service release failed: {e}")


# ─── Wake-word sample capture ─────────────────────────────────────────────────


# ─── Data plane handler ───────────────────────────────────────────────────────


# ─── Router ───────────────────────────────────────────────────────────────────

# ─── Shell plane handler ──────────────────────────────────────────────────────


async def _route(ws: WebSocketServerProtocol, secure: bool):
    path = ws.request.path if hasattr(ws, "request") else getattr(ws, "path", "/")
    em_tcp.tune(ws.transport.get_extra_info("socket"))

    if path == "/control":
        await handle_control(ws, secure)
    elif path == "/data":
        await em_planes.handle_data(ws, secure)
    elif path.startswith("/shell/"):
        await em_planes.handle_shell(ws, path, secure)
    else:
        log.warning(f"Unknown WebSocket path: {path} from {ws.remote_address}")
        await ws.close()


async def router(ws: WebSocketServerProtocol):
    await _route(ws, secure=False)


async def router_tls(ws: WebSocketServerProtocol):
    await _route(ws, secure=True)


# ─── mDNS ─────────────────────────────────────────────────────────────────────

def _make_mdns_info(tls_active: bool) -> ServiceInfo:
    props = {"version": "1", "server": MDNS_NAME}
    if tls_active:
        # Devices holding the pushed CA dial wss://<addr>:<tls_port> instead
        # of the plain port. Absent property = pre-TLS controller → plain ws.
        props["tls_port"] = str(SERVER_TLS_PORT)
    return ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=SERVER_PORT,
        properties=props,
        server=f"{MDNS_NAME}.local.",
    )


async def _mdns_refresh_loop(azc: AsyncZeroconf, info: ServiceInfo) -> None:
    while True:
        await asyncio.sleep(MDNS_REFRESH_INTERVAL)
        try:
            await azc.async_update_service(info)
            log.debug("mDNS registration refreshed")
        except Exception as e:
            log.warning(f"mDNS refresh failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def event_loop_lag_monitor(interval: float = 1.0,
                                 warn_ms: float = 250.0) -> None:
    """
    Watch for asyncio event-loop stalls.

    Sleeps for a known interval and measures the overshoot: if the loop is
    blocked by synchronous work, the wake-up is late by roughly the length
    of that block. Anything blocking the loop also delays speaker frames
    reaching the socket, so this is the controller-side counterpart to the
    device's buffer-margin metric — it answers "were we the ones who were
    late?" without needing a profiler attached.

    Costs one wake-up per second and logs only when a threshold is crossed;
    the running peak is exposed on /api/system/status.
    """
    loop = asyncio.get_event_loop()
    next_cpu = 0.0
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        lag_ms = (loop.time() - t0 - interval) * 1000
        if lag_ms > em_state.loop_lag_peak_ms:
            em_state.loop_lag_peak_ms = lag_ms
        # Piggyback the controller's CPU sampling on this ticker rather than
        # starting a second one: it is one os.times() every 30s, and the
        # windowed CPU it feeds is only read when a support bundle is built.
        if loop.time() >= next_cpu:
            next_cpu = loop.time() + em_process_stats.CPU_SAMPLE_INTERVAL_S
            em_process_stats.sample_cpu()
        if lag_ms >= warn_ms:
            log.warning(
                f"[loop] event loop stalled {lag_ms:.0f}ms — "
                f"speaker sends and LED frames were delayed by this much"
            )


async def _until_stopped() -> None:
    """Return on SIGTERM or SIGINT, so main()'s teardown runs. In a container
    we are PID 1 (em_start execs us), and the kernel drops a signal PID 1 has
    no handler for: `docker stop` used to SIGKILL us after its grace period."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("Shutting down")
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)


def _close_db() -> None:
    em_dbwriter.drain()
    db.close()


async def main():
    log.info(f"EchoMuse Controller {CONTROLLER_VERSION}")
    db.init(DB_PATH)
    em_state.boards.update(em_boards.decode(await asyncio.get_running_loop()
                           .run_in_executor(None, db.get_config, em_boards.CONFIG_KEY)))
    auth.maybe_generate_bootstrap_token()
    em_player.init(
        get_device=em_state.devices.get,
        notify_state=esphome.push_media_state,
    )

    runner = await api.create_runner()
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, API_PORT)
    await site.start()
    log.info(f"Dashboard + API listening on http://{SERVER_HOST}:{API_PORT}")

    release_task       = asyncio.create_task(em_releases.poll_loop())
    session_prune_task = asyncio.create_task(api.session_prune_loop())
    loop_lag_task      = asyncio.create_task(event_loop_lag_monitor())

    # Device-link TLS: generate/load the CA + server cert. Failure to set
    # up TLS (missing cryptography package, unwritable dir) must never take
    # the plain listener down with it — the fleet lives on that during
    # rollout.
    tls_ctx = None
    if SERVER_TLS_PORT:
        try:
            tls_dir = em_pki.ensure_pki(DB_PATH)
            if tls_dir:
                tls_ctx = em_pki.server_ssl_context(tls_dir)
                em_state.set_tls_dir(tls_dir)
        except Exception as e:
            log.error(f"Device-link TLS setup failed — wss listener disabled: {e}")
    em_state.set_link_ports(SERVER_IP, SERVER_PORT, SERVER_TLS_PORT if tls_ctx else 0)

    # Bound to SERVER_IP's own interface, never auto-detected across all of
    # them. zeroconf's default enumerates every interface on the host and
    # opens a socket per one, and on a multi-homed box that includes whatever
    # else is attached — a monitoring VLAN in one deployment, something else
    # elsewhere. A send on any one of those sockets can fail on its own
    # (measured: `OSError: [Errno 19] No such device` from a `Monitor*`
    # interface that carries a real IPv4 address and gets auto-detected like
    # any other), and that exception surfaces inside the asyncio event loop
    # rather than being contained to the interface that caused it — stalling
    # the loop long enough to miss keepalive pings and disconnect the fleet.
    # SERVER_IP is already the one address every device is told to dial, so
    # advertising from anywhere else would be a second kind of wrong answer.
    azc  = em_hostip.bind_mdns(AsyncZeroconf, SERVER_IP)
    info = _make_mdns_info(tls_active=tls_ctx is not None)
    await azc.async_register_service(info, allow_name_change=True)
    log.info(
        f"mDNS advertising {MDNS_NAME}._emcontroller._tcp.local "
        f"→ {SERVER_IP}:{SERVER_PORT}"
        + (f" (tls_port={SERVER_TLS_PORT})" if tls_ctx else "")
    )
    mdns_task = asyncio.create_task(_mdns_refresh_loop(azc, info))

    log.info(f"WebSocket server starting on {SERVER_HOST}:{SERVER_PORT}")

    try:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(websockets.serve(
                router,
                SERVER_HOST,
                SERVER_PORT,
                ping_interval=em_wire.WS_PING_INTERVAL_S,
                ping_timeout=em_wire.WS_PING_TIMEOUT_S,
                max_size=10 * 1024 * 1024,
            ))
            if tls_ctx is not None:
                await stack.enter_async_context(websockets.serve(
                    router_tls,
                    SERVER_HOST,
                    SERVER_TLS_PORT,
                    ssl=tls_ctx,
                    ping_interval=em_wire.WS_PING_INTERVAL_S,
                    ping_timeout=em_wire.WS_PING_TIMEOUT_S,
                    max_size=10 * 1024 * 1024,
                ))
                log.info(f"Device-link TLS (wss) listening on {SERVER_HOST}:{SERVER_TLS_PORT}")
            if em_link_gate.REQUIRE_DEVICE_TLS:
                log.info("REQUIRE_DEVICE_TLS=1 — plain/tokenless device connections will be rejected")

            await esphome.start_esphome_servers(em_state.devices, SERVER_HOST)
            # After the voice satellites — BT proxies reuse their zeroconf.
            em_ble_proxy.set_gatt_sender(_send_gatt)
            await em_ble_proxy.start_ble_proxy_servers(SERVER_HOST)

            log.info("EchoMuse Controller ready — waiting for devices")
            await _until_stopped()

    finally:
        await em_ble_proxy.stop_ble_proxy_servers()
        await esphome.stop_esphome_servers()
        release_task.cancel()
        session_prune_task.cancel()
        loop_lag_task.cancel()
        mdns_task.cancel()
        await azc.async_unregister_service(info)
        await azc.async_close()
        await runner.cleanup()
        await asyncio.get_running_loop().run_in_executor(None, _close_db)
        log.info("EchoMuse Controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
