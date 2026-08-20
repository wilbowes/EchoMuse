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
    {"type": "pong"}

  /control — Server → Device:
    {"type": "ack",     "device_id": "..."}
    {"type": "pending"}
    {"type": "config",  "adcDigitalGain": 100, ...}
    {"type": "leds",    "leds": [...]}
    {"type": "mic_start"}
    {"type": "mic_stop"}
    {"type": "ping"}

  /data — Device → Server:
    <binary> [0x01][seq_hi][seq_lo][PCM mono S16_LE 2560 bytes]

  /data — Server → Device:
    <binary> [0x02][PCM mono S16_LE 48kHz — 4096 bytes per period]
    <binary> [0x03] end of audio stream

  /shell — bidirectional raw binary (demand-opened by device on
           receipt of shell_open control message — not yet implemented
           in this revision; shell connections come inbound from the
           Go binary to the controller's /shell/{device_id} path)
"""

import asyncio
import collections
import contextlib
import json
import logging
import os
import socket
import struct

import numpy as np
from aiohttp import web
from openwakeword.model import Model as OWWModel
from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_db as db
import em_auth as auth
import em_api as api
import em_pki
import em_hostip
import em_linkauth
import em_eq
import em_limiter
import em_mbc
import em_scenes
import em_shadow
import em_oww_warmup
import em_barge
import em_arbiter
import em_button
import em_tap_burst
import em_esphome as esphome
import em_ble_proxy
import em_oww_models
import em_player
import em_volume

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
api.install_log_ring(_LOG_FORMAT)

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


def _log_task_exception(task: asyncio.Task) -> None:
    """
    Standard done-callback for fire-and-forget asyncio.create_task() calls.

    Without this, an exception raised inside a task nobody awaits vanishes
    silently — asyncio only surfaces it via a "Task exception was never
    retrieved" warning at garbage-collection time, easy to miss in normal
    logs. Attach via task.add_done_callback(_log_task_exception) at every
    fire-and-forget create_task() call site (see M1 in the 2026-07-05
    review — currently applied to the button-triggered voice turn task).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"Unhandled exception in background task {task.get_name()}: {exc}", exc_info=exc)


# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
# Device-link TLS listener (wss) — same three WS planes as SERVER_PORT,
# wrapped in TLS with the em_pki-generated cert. 0 disables. Devices pick
# it up from the tls_port mDNS TXT property and dial wss iff they hold the
# pushed CA file (see device/internal/client/tlscreds.go).
SERVER_TLS_PORT = int(os.environ.get("SERVER_TLS_PORT", "8770"))
# Enforcing posture: reject device connections that are not TLS + a valid
# per-device token. Leave 0 until the whole fleet shows tls=true —
# a plain, tokenless connection is the legacy default and must keep
# working during the rollout.
REQUIRE_DEVICE_TLS = os.environ.get("REQUIRE_DEVICE_TLS", "0") == "1"
API_PORT     = int(os.environ.get("API_PORT", "8768"))
# The address devices are told to dial. Detected from the routing table when
# unset — never a literal, which used to send every unconfigured deployment
# to a developer's own machine. See em_hostip.
SERVER_IP    = em_hostip.server_ip(os.environ.get("SERVER_IP"))
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")
DB_PATH      = os.environ.get("DB_PATH", "echomuse.db")

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")

# Mic
CHUNK_BYTES          = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
# NOTE: VOICE_PREROLL_DISCARD lives in em_esphome.py (esphome.VOICE_PREROLL_DISCARD)
# — it's used there in _stream_mic_audio, and _run_voice_locked below reads it
# via that single source of truth rather than keeping a second copy here that
# could drift out of sync (a duplicate here was previously dead code — see
# v2.6.3 changelog — resist the temptation to reintroduce it).


# Speaker — must match PcmSpeaker constants in Go. The wire carries MONO
# 48kHz (the device duplicates to stereo at the ALSA write — shipping two
# identical channels to a mono speaker doubled TTS bandwidth for nothing,
# and halving it matters on marginal 2.4GHz links).
SPEAKER_RATE   = 48000
SPEAKER_PERIOD = 2048
SPEAKER_BYTES  = SPEAKER_PERIOD * 2       # 4096 bytes/period (mono S16)

# The device holds playback until ~this much audio is buffered (or EOS
# arrives) — primePeriods in pcm_speaker.go. The post-playback drain sleep
# must allow for the delayed start.
SPEAKER_PRIME_SECONDS = 1.1

# Control-plane RTT probing. 5s rather than the old 30s keepalive cadence:
# characterising jitter needs samples, and one tiny JSON message per device
# per 5s is negligible next to the 256 kbps continuous mic upload each
# device already holds open. Samples are aggregated in memory and flushed on
# the existing ~30s stats report, so the DB cost is unchanged.
PING_INTERVAL_SEC = 5.0
# A sample at or above this counts as an excursion. 200ms is well clear of a
# healthy hop (Office measures 264ms median for a whole audio round trip
# including frame batching) while catching the ~1s tail under investigation.
RTT_EXCURSION_MS = 200

# Outstanding pings older than this are abandoned — a reply that late is not
# a latency measurement, it is a lost packet, and keeping them would grow
# ping_sent without bound across a long disconnect.
PING_TIMEOUT_SEC = 60.0

# LEDs
NUM_LEDS = 12

# Wake word
OWW_MODEL     = os.environ.get("OWW_MODEL", "hey_jarvis")
OWW_THRESHOLD = float(os.environ.get("OWW_THRESHOLD", "0.5"))

# mDNS re-registration interval — keeps IGMP membership alive on the LAN
MDNS_REFRESH_INTERVAL = 120

# Binary frame types
MIC_FRAME_TYPE     = 0x01
VAD_END_TYPE       = 0x04
# Distinct from VAD_END_TYPE — device never detected speech at all within its
# local no-speech grace period (see device/internal/client/data.go
# noSpeechTimeout), as opposed to VAD_END_TYPE which means speech was
# detected and then ended normally. Each frame type queues its matching
# string sentinel (esphome.VAD_SENTINEL_END / VAD_SENTINEL_TIMEOUT) so the
# type travels with the queue item — B5 fix, 2026-07-07; the old None +
# device.last_vad_was_timeout side-channel let a second sentinel overwrite
# the first's flag before it was consumed. OWW/barge-watcher consumers treat
# both flavours identically; esphome's _stream_mic_audio differentiates.
VAD_NO_SPEECH_TIMEOUT_TYPE = 0x05
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3   # [type][seq_hi][seq_lo]

# Volume conversion lives in em_volume so the scale has ONE definition and a
# test — it used to be spelled `/ 175` in three separate modules.
VOLUME_MAX_DEVICE = em_volume.DEVICE_VOLUME_MAX
_device_level_to_ha = em_volume.device_level_to_ha
_ha_volume_to_device = em_volume.ha_volume_to_device

# ─── Device registry ──────────────────────────────────────────────────────────

# How long a speaker stream will wait, IN TOTAL, for a dropped data connection
# to come back before giving up on the rest of it.
#
# A brief Wi-Fi blip mid-stream used to truncate the audio outright: send_data
# saw data_ws was None, logged, and dropped every remaining frame, so a
# reconnect a second later arrived to find the audio already thrown away
# (reported by @kopiro in #28, on long read-aloud responses).
#
# The budget is per STREAM, not per frame, and that distinction is the whole
# design. send_data is called once per audio period; a per-frame wait means a
# device that is genuinely gone stalls every remaining frame in turn, so a
# stream that should abort in seconds instead drains for hours holding the
# voice lock. Spending one shared budget across the stream rides out a blip
# and still fails fast on a real disconnect.
#
# 3s because it is covering a reconnect, not an outage: measured RTT
# excursions on this fleet peak around 1.7s, and the device's own buffer holds
# ~5.5s, so a blip inside this window is inaudible.
DATA_RECONNECT_GRACE_S = 3.0


class Device:
    def __init__(
        self,
        device_id: str,
        ip: str,
        capabilities: list,
        control_ws: WebSocketServerProtocol,
    ):
        self.device_id    = device_id
        self.ip           = ip
        self.capabilities = capabilities
        self.control_ws   = control_ws
        # Set from the register message; None on firmware that predates it.
        self.ambient_light_status: dict | None = None

        self.data_ws: WebSocketServerProtocol | None = None
        # Remaining reconnect grace for the speaker stream in flight. Armed by
        # begin_data_stream(); spent down by send_data so the whole stream
        # shares one budget rather than each frame having its own.
        self._data_grace_left: float = 0.0
        self.voice_lock   = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self.mic_queue:   asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.voice_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.oww_paused   = asyncio.Event()  # set during voice turn

        # Transient state — read by em_api._merge_device()
        self.speaking  = False
        self.muted     = False
        self.listening = False
        self.thinking  = False

        # Volume as HA float (0.0–1.0). Initialised from stored config in
        # handle_control() after config is read; updated on volume_state
        # messages from the device and persisted back to config.
        # Default matches DEFAULT_DEVICE_CONFIG startupVolume=85.
        self.volume: float = _device_level_to_ha(85)

        self.data_ready = asyncio.Event()

        # Tunable at runtime — updated when a config push arrives.
        # wake_word_listener reads this each detection cycle rather than
        # caching a snapshot at startup, so config changes take effect
        # without requiring a device reconnect.
        self.oww_threshold: float = OWW_THRESHOLD
        self.oww_model:     str   = f"{OWW_MODEL}_v0.1"
        # Multi-device wake arbitration window (ms, 0 = off). Only
        # consulted when 2+ devices are connected — a solo fleet never
        # pays the latency.
        self.wake_arb_ms:   int   = 300
        # Q1 fix (2026-07-05 review): openwakeword's built-in speexdsp noise
        # suppressor — 16kHz-native, applied controller-side, only to the
        # wake path (cannot affect STT audio since STT never sees it). Like
        # oww_model, a change here requires reconstructing the OWWModel
        # instance — wake_word_listener's reload loop checks this alongside
        # oww_model. Config key: owwSpeexNs. Defaults False (opt-in — needs
        # the speexdsp-ns pip package confirmed installable in the Docker
        # build before enabling fleet-wide; see review Q1 fix sequence).
        self.oww_speex_ns:  bool  = False
        # nsAsr: controller-side DTLN noise suppression on the ASR-bound
        # turn stream only (em_ns.py; wake stream stays raw).
        self.ns_asr:        bool  = False
        # saveUtterances: keep this turn's ASR-bound mic audio and write it
        # to recordings/ at turn end (em_recordings). Read per turn, so
        # switching it off stops the next turn being captured, not the one
        # already streaming.
        self.save_utterances: bool = False
        # This turn's captured mic audio, handed from _stream_mic_audio to
        # _persist_turn (which owns the write — it has the rowid the
        # filename is keyed on) and consumed there.
        self.last_utterance_pcm: bytes | None = None
        self.eq_bands:      list  = [0.0] * 8
        self.eq_loudness:   bool  = False
        self.bass_guard_enabled: bool  = True
        self.bass_guard_db:      float = em_mbc.DEFAULT_BASS_GUARD_DB
        self.limiter_enabled:   bool  = True
        self.limiter_threshold: float = em_limiter.DEFAULT_THRESHOLD_DB
        self.limiter_release:   float = em_limiter.DEFAULT_RELEASE_MS
        # LED ring scene — render-ready palette/spinner from em_scenes,
        # refreshed on connect and on any config push carrying led* keys.
        self.led_scene:     dict  = em_scenes.resolve({})
        self.stats:         dict | None = None
        # In-flight wifi_scan awaiter (set by the API handler). Change
        # pending/result state lives in api._wifi_states instead — this
        # Device object dies with the connection when the network switches.
        self.wifi_scan_future: asyncio.Future | None = None
        # Q4 fix (2026-07-05 review): dashboard-visible near-miss counter —
        # incremented in wake_word_listener whenever a score exceeds 0.05
        # but doesn't clear device.oww_threshold. Separate field from
        # self.stats deliberately: self.stats is entirely overwritten every
        # ~30s by the device's own hardware-stats report (msg_type=="stats"
        # in handle_control), so anything stashed inside it would get wiped
        # on the next report. This field is controller-owned and persists
        # independently, reset only on device reconnect (see Device.__init__
        # semantics generally — a fresh Device is created per connection).
        self.oww_near_misses: int = 0

        # On-device wake word shadow mode (schema v13). The device scores the
        # same wake stream locally and reports threshold crossings as they
        # happen; these are correlated against THIS controller's own detections
        # at turn-persist time. Monotonic timestamps throughout: an Echo's wall
        # clock is unreliable before NTP, so the device reports the AGE of a
        # crossing and the controller converts it against its own clock — the
        # same reasoning as the control-plane RTT instrumentation.
        #
        # maxlen bounds this without a sweeper: crossings are rare (the device
        # applies a refractory period, so one per utterance) and only the most
        # recent few can ever be within a match window.
        self.shadow: em_shadow.ShadowTracker = em_shadow.ShadowTracker()
        # Monotonic instant of this controller's most recent wake detection,
        # consumed once by the turn record it belongs to.
        self.last_wake_mono = None  # float | None

        # owwOnDevice, normalised. "on" hands the wake DECISION to the device:
        # its crossing lands in pending_wake and the wake listener acts on it
        # instead of on its own score. Default off, and set from config on
        # every push, so a device whose config has never arrived behaves
        # exactly as it always did.
        self.oww_on_device: str = em_shadow.MODE_OFF
        # Whether this device is believed to HAVE the classifier it is
        # configured to use. False stands it down to controller-side wake
        # (em_shadow.effective_mode), because a device cannot score a model it
        # does not have and "on" means nobody else is triggering for it —
        # which is silent, and looks healthy (#191).
        #
        # Optimistic by default: absence of evidence is not evidence of
        # absence, and standing every device down on a fresh controller would
        # be a worse bug than the one this prevents.
        #
        # A BACKSTOP, not the primary mechanism. Config changes are handled by
        # install-before-switch (em_api._hold_back_oww_model): a device is
        # never told to use a model it does not have, so it cannot be deafened
        # by an ordinary wake-word change. This covers the causes a config
        # change cannot see — a file deleted underneath us, a device
        # reprovisioned behind our back — and its writer is the
        # reconcile-on-connect pass designed in #191, which is the first thing
        # that will actually KNOW what a device has.
        self.oww_model_ready: bool = True
        self.pending_wake: em_shadow.PendingWake = em_shadow.PendingWake()
        # This controller's own crossings while the DEVICE is triggering —
        # the comparison from the other side. Kept in "on" mode because the
        # question that justified on-device wake ("do the two agree?") is
        # still worth answering once the roles are swapped, and this is the
        # only place a controller miss can be seen at all.
        self.ctrl_shadow: em_shadow.ShadowTracker = em_shadow.ShadowTracker()

        # Per-room noise floor estimate (normalized RMS, 0..1), tracked from
        # the continuous wake stream in wake_word_listener. Measurement only —
        # never applied to the audio (see 2026-07-06 architecture discussion:
        # adaptation as measurement, not signal modification). Consumers:
        # em_esphome._stream_mic_audio's SNR-relative no-speech detection,
        # and diagnostics (near-miss logs). Asymmetric tracker: follows drops
        # quickly, rises slowly, so speech doesn't drag the floor up.
        self.noise_floor: float = 0.0

        # Barge-in (§3.2): wake word interrupts the thinking phase or TTS
        # playback. Controller-side feature — with it enabled the mic keeps
        # streaming through the turn (device AEC subtracts the speaker
        # output during playback) and _barge_watcher scores the stream from
        # STT_VAD_END onward (barge_threshold during playback, the normal
        # wake threshold during thinking); on detection it sets
        # barge_detected + cancel_event (plus speaker_flush or HA pipeline
        # cancel, phase-dependent) and the turn loop re-enters a fresh
        # turn. _barge_model is a dedicated OWW instance (the main wake
        # listener task is blocked awaiting the turn).
        self.barge_in_enabled = False
        # False until config is pushed, so a device connecting before then
        # keeps the historical tap-starts-a-turn behaviour.
        self.button_single_tap_event = False
        self.button_multi_tap_ms = 0
        self.tap_burst = em_tap_burst.TapCoalescer(
            lambda name: esphome.send_button_event(self.device_id, name),
            enabled=lambda: self.button_single_tap_event,
            on_error=_log_task_exception,
        )
        self.barge_threshold  = 0.6
        self.barge_detected   = False
        self._barge_model     = None
        self._barge_model_key = None

        # Recent voice-turn traces (dicts derived from TurnTrace at emit
        # time in em_esphome) — powers the Status tab's observability panel.
        # Hydrated from the persistent turns table on connect (handle_control),
        # appended live; bounded.
        self.turn_history: collections.deque = collections.deque(maxlen=50)

        # Wake detection detail for the turn about to start — set by
        # wake_word_listener / _barge_watcher at detection, popped by
        # em_esphome.trigger_voice_turn into the turn's trace. None for
        # button/continuation turns.
        self.last_wake: dict | None = None

        # Playback stats rendezvous. The device reports playback_stats when
        # its buffer drains, the controller persists the turn when its
        # (deliberately overestimated) drain sleep ends — either can happen
        # first. last_turn_id covers stats-after-persist: set at persist for
        # turns that played audio, consumed by handle_control (cleared on
        # use so an announcement's report can't overwrite a turn's stats).
        # pending_playback_stats covers stats-before-persist: (ts, periods,
        # underruns) stashed by handle_control, folded into the record by
        # em_esphome._persist_turn if fresh (staleness window keeps a
        # long-ago announcement's stats out of an unrelated later turn).
        self.last_turn_id: int | None = None
        self.pending_playback_stats: tuple | None = None

        # Controller-side playback timing (v7 instrumentation).
        # playback_send_t0 is set when the first 0x02 of a response goes
        # out and consumed when the device's playback_stats lands — the
        # difference is the true delivery window, as opposed to
        # playback_send_ms, which only times writing into the socket and
        # completes almost instantly however slow the link is.
        self.playback_send_t0: float | None = None
        # Set when the device reports playback_stats for the stream being
        # played. This is the authoritative "the audio has finished" signal
        # — the device emits it once its audio channel has drained after
        # EOS, i.e. when the last period has gone to ALSA. Cleared at the
        # start of every speaker stream; awaited by _run_post_turn_playback
        # in place of the wall-clock estimate that used to clear the ring
        # while the device was still playing (up to 6.1s early, 2026-07-24).
        self.playback_done = asyncio.Event()
        # Outcome of the most recently persisted turn, set by em_esphome and
        # consumed once by the turn loop's ring cleanup (see _leds_turn_end).
        self.last_turn_outcome: str | None = None

        # ── Control-plane RTT ────────────────────────────────────────────
        # End-to-end latency is the one thing the RF layer cannot tell us on
        # this hardware: the MTK driver leaves retry/discard/missed-beacon
        # at zero in /proc/net/wireless and reports NOISE=9999, and there is
        # no `iw` binary. So the tx/rx/error counters cannot distinguish a
        # healthy link from a struggling one — while RTT measures the thing
        # that actually degrades the experience, needs no driver support,
        # and discriminates between the live hypotheses: contention makes
        # latency track LOAD, whereas WiFi power-save makes it spike when
        # IDLE, quantised to the beacon interval.
        #
        # Accumulated in memory and flushed on the device's existing ~30s
        # stats report, so this costs no write the loop wasn't making.
        self.ping_seq   = 0
        self.ping_sent: dict[int, float] = {}   # seq -> monotonic send time
        self.ping_busy: dict[int, bool]  = {}   # seq -> device busy at send
        self.rtt_last_ms: int | None = None
        self.rtt_sum_ms  = 0
        self.rtt_count   = 0
        self.rtt_min_ms: int | None = None
        self.rtt_max_ms: int | None = None
        self.rtt_excursions      = 0   # samples over RTT_EXCURSION_MS
        self.rtt_excursions_idle = 0   # ...of which the device was idle
        # Denominator for the above. Without it, "every excursion happened
        # while idle" is vacuous: almost every SAMPLE is idle, because
        # devices spend most of their life not in a turn. The discriminator
        # is the excursion RATE per state, not the raw count.
        self.rtt_samples_idle    = 0

    def is_busy(self) -> bool:
        """Whether this device was doing anything when a ping went out."""
        return bool(
            self.voice_lock.locked()
            or self.speaking
            or em_player.is_playing(self.device_id)
        )

    def record_rtt(self, rtt_ms: int, was_busy: bool) -> None:
        self.rtt_last_ms = rtt_ms
        self.rtt_sum_ms += rtt_ms
        self.rtt_count  += 1
        if not was_busy:
            self.rtt_samples_idle += 1
        if self.rtt_min_ms is None or rtt_ms < self.rtt_min_ms:
            self.rtt_min_ms = rtt_ms
        if self.rtt_max_ms is None or rtt_ms > self.rtt_max_ms:
            self.rtt_max_ms = rtt_ms
        if rtt_ms >= RTT_EXCURSION_MS:
            self.rtt_excursions += 1
            if not was_busy:
                self.rtt_excursions_idle += 1

    def drain_rtt(self) -> dict:
        """Take the accumulated window and reset. Empty dict if no samples."""
        if not self.rtt_count:
            return {}
        out = {
            "rttSumMs":         self.rtt_sum_ms,
            "rttSamples":       self.rtt_count,
            "rttMinMs":         self.rtt_min_ms,
            "rttMaxMs":         self.rtt_max_ms,
            "rttExcursions":    self.rtt_excursions,
            "rttExcursionsIdle": self.rtt_excursions_idle,
            "rttSamplesIdle":   self.rtt_samples_idle,
        }
        self.rtt_sum_ms = self.rtt_count = 0
        self.rtt_min_ms = self.rtt_max_ms = None
        self.rtt_excursions = self.rtt_excursions_idle = 0
        self.rtt_samples_idle = 0
        return out
        self.playback_send_ms: int = -1
        self.playback_eq_ms:   int = -1

    async def send_control(self, msg: dict):
        try:
            await self.control_ws.send(json.dumps(msg))
        except Exception as e:
            log.warning(f"[{self.device_id}] Control send failed: {e}")

    def begin_data_stream(self) -> None:
        """
        Arm the reconnect grace for one speaker stream.

        Called at the start of each stream so the budget is fresh, and so a
        stream that already spent it cannot borrow from the next one.
        """
        self._data_grace_left = DATA_RECONNECT_GRACE_S

    async def _await_data_reconnect(self, budget: float) -> float:
        """Wait up to `budget` seconds for the data plane. Returns time spent."""
        step = 0.1
        waited = 0.0
        while waited < budget:
            await asyncio.sleep(step)
            waited += step
            if self.data_ws is not None:
                log.info(f"[{self.device_id}] Data connection back after "
                         f"{waited:.1f}s — resuming stream")
                break
        return waited

    async def send_data(self, data: bytes):
        if self.data_ws is None and self._data_grace_left > 0:
            # Ride out a blip rather than discarding the rest of the audio.
            # The budget is spent down, so a device that never returns costs
            # the stream DATA_RECONNECT_GRACE_S once, not once per frame.
            self._data_grace_left -= await self._await_data_reconnect(
                self._data_grace_left)
        if self.data_ws is None:
            log.warning(f"[{self.device_id}] No data connection")
            return
        try:
            await self.data_ws.send(data)
        except Exception as e:
            log.warning(f"[{self.device_id}] Data send failed: {e}")

    async def set_leds(self, leds: list, listening: bool | None = None):
        # The optional listening flag tells the device explicitly that this
        # frame is the listening ring (enables its direction overlay).
        # Pre-scene firmware inferred it from the ring being all-green —
        # that heuristic breaks for every non-green scene, so newer
        # firmware trusts this flag when present and old firmware just
        # ignores the extra key.
        msg = {"type": "leds", "leds": leds}
        if listening is not None:
            msg["listening"] = listening
        await self.send_control(msg)

    @property
    def led_anim_capable(self) -> bool:
        return "led_anim" in (self.capabilities or [])

    @property
    def audio_mix_capable(self) -> bool:
        """
        Whether this firmware holds music on its own plane and mixes it with
        voice at the ALSA write.

        When it does, a voice turn DUCKS the music instead of pausing it —
        which is the only place ducking can happen. The music feed runs
        LEAD_S=4s ahead of realtime, so the next four seconds are already on
        the device when a wake word fires, and audio that has left here
        cannot be ducked from here. Without the capability the controller
        keeps the pause/resume path: a device that cannot mix would never
        play the 0x04 stream at all, which is silence rather than degraded
        behaviour.
        """
        return "audio_mix" in (self.capabilities or [])

    @property
    def button_hold_capable(self) -> bool:
        """Measures hold time — and so was offered the HA event entity."""
        return "button_hold" in (self.capabilities or [])

    @property
    def oww_shadow_capable(self) -> bool:
        """
        Whether this firmware can score the wake word on-device at all.

        Capability, not version comparison: the device states what it
        implements, so the controller needs no knowledge of our release history
        and a dev build is not mistaken for an old one. This answers "could it"
        — `shadow.active` answers "is it, right now", which is a different
        question and comes from whether its stats reports carry a summary.
        Both are needed: capability drives what the dashboard offers, activity
        drives whether a missing per-turn score is a real miss.
        """
        return "oww_shadow" in (self.capabilities or [])

    @property
    def oww_trigger_capable(self) -> bool:
        """
        Whether this firmware can ACT on its own wake detection.

        Separate from oww_shadow_capable because shadow shipped first: there
        are devices in the field that score the wake word and report it, and
        cannot start a turn from it. Offering those owwOnDevice="on" produces a
        device that scores perfectly, never answers, and looks broken — the
        exact "I enabled it and nothing happened" the capability rule exists to
        prevent, so "on" is gated on this and shown disabled with the reason
        otherwise.
        """
        return "oww_trigger" in (self.capabilities or [])

    async def send_led_anim(self, anim: dict):
        """
        Hand the ring to the device's local animation engine (led_anim
        capability, v2.9+ firmware). The device renders frames on its own
        ticker until a newer led_anim/leds message replaces the spec or
        its ttlSec dead-man expires — so a controller stall or WiFi jitter
        can no longer make the spinner judder, and a dead controller can't
        leave the ring lit.
        """
        await self.send_control({"type": "led_anim", "anim": anim})

    async def ping(self):
        await self.send_control({"type": "ping"})

    async def mic_start(self):
        await self.send_control({"type": "mic_start"})

    async def mic_start_turn(self):
        """Start mic for a voice turn — signals device to lock the best directional mic."""
        await self.send_control({"type": "mic_start", "lock_mic": True})

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

    async def beam_lock(self):
        # Lock the beamformer onto the speaker's perimeter mic mid-stream —
        # no stream restart. Device no-ops if already locked or if
        # beamformingEnabled is false in its config.
        await self.send_control({"type": "beam_lock"})

    async def beam_unlock(self):
        await self.send_control({"type": "beam_unlock"})

    async def push_config(self, **kwargs):
        await self.send_control({"type": "config", **kwargs})

    async def _set_speaking(self, value: bool) -> None:
        """
        Set the speaking flag AND tell the dashboard.

        The single writer, because the flag and the push had drifted apart:
        stream_speaker/stream_speaker_chunks set it, and nothing pushed the
        transition. _push_device_state has always carried `speaking` and the
        dashboard has always rendered it above `thinking` — but the only
        pushes were at listening, thinking and turn end, so a turn read
        listening -> thinking -> idle and **never showed Speaking at all**. It
        appeared only when the dashboard's 5s poll of /api/devices happened to
        land mid-playback, which for a typical ~2s response it usually did not.

        WHEN each edge fires, and how true each is:

        - **False is device truth.** The playback functions wait on the
          device's own `playback_stats`, sent once its audio channel drains
          after EOS, and clear the flag there.
        - **True is still a controller-side ESTIMATE** — the first period put
          on the wire. The device holds audio until roughly
          SPEAKER_PRIME_SECONDS is queued (primePeriods, pcm_speaker.go), so
          the tile leads the speaker by up to that much. Closing that gap needs
          the DEVICE to report the moment it starts, which no released firmware
          does; `playback_stats` is the only playback message it sends.

        Guarded rather than plain, because one caller is stream_speaker's
        finally, which is also reached when barge-in cancels the task
        mid-send: the flag assignment is synchronous and always happens, and a
        push that cannot complete is not worth failing a speaker stream over —
        turn end pushes the same state moments later.
        """
        if self.speaking == value:
            return
        self.speaking = value
        if value:
            # Mutually exclusive phases. Leaving `thinking` set meant the tile
            # FELL BACK to Thinking the moment speaking cleared, instead of
            # going quiet — which is what made an early clear look like the
            # device had started thinking again mid-response.
            self.thinking = False
        try:
            await _push_device_state(self)
        except BaseException:
            pass

    async def stream_speaker(self, pcm: bytes):
        """Stream resampled mono 48kHz PCM as 0x02 frames, then 0x03 EOS."""
        self.begin_data_stream()
        await self._set_speaking(True)
        try:
            offset = 0
            while offset < len(pcm):
                if self.cancel_event.is_set():
                    break
                chunk = pcm[offset:offset + SPEAKER_BYTES]
                if len(chunk) < SPEAKER_BYTES:
                    # Pad the final partial period with silence — without this,
                    # up to one full period (~42ms at 48kHz) of the last word is
                    # silently dropped because the old loop required a full period.
                    chunk = chunk + bytes(SPEAKER_BYTES - len(chunk))
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                offset += SPEAKER_BYTES
        finally:
            # NOT where speaking clears — see _run_post_turn_playback. This
            # returns when the last byte is written to the socket, which
            # completes near-instantly however slow the link is; the device
            # still has its whole buffer to play.
            # EOS must go out on EVERY exit, including task cancellation
            # (barge-in cancels this task mid-send): the device's barge-in
            # flush discards 0x02 frames until it sees this stream's 0x03 —
            # a stream that ends without one would leave the discard armed
            # and swallow the next turn's audio. shield() lets the send
            # complete even though this task is mid-cancellation; the
            # original CancelledError still propagates after the finally.
            try:
                await asyncio.shield(self.send_data(bytes([SPEAKER_EOS_TYPE])))
            except BaseException:
                pass  # WS gone / re-cancelled — device flush self-heals on reconnect

    async def stream_speaker_chunks(self, pcm_chunks, stream_eq):
        """
        Stream an asynchronous PCM source as one device speaker session.

        StreamingEQ stays alive for the complete response so its biquad state
        crosses HTTP chunk boundaries without clicks. Partial device periods
        are retained until more PCM arrives and padded only once, at the true
        end of the response.
        """
        self.begin_data_stream()
        pending = bytearray()
        total_pcm = 0
        eq_seconds = 0.0
        # Accumulated time spent WRITING to the socket, excluding time waiting
        # for the source to produce audio. send_ms is documented as socket-write
        # time that "completes near-instantly however slow the link is" — timing
        # the whole streaming loop instead would fold HA's synthesis time into
        # it and make it read like delivery, which is the misreading that cost
        # an investigation on 2026-07-20.
        send_seconds = 0.0
        first_send_time = None
        try:
            async for pcm in pcm_chunks:
                if self.cancel_event.is_set():
                    break
                total_pcm += len(pcm)
                eq_started = asyncio.get_event_loop().time()
                pending.extend(stream_eq.process(pcm))
                eq_seconds += asyncio.get_event_loop().time() - eq_started

                while len(pending) >= SPEAKER_BYTES:
                    if self.cancel_event.is_set():
                        break
                    chunk = bytes(pending[:SPEAKER_BYTES])
                    del pending[:SPEAKER_BYTES]
                    if first_send_time is None:
                        first_send_time = asyncio.get_event_loop().time()
                        self.playback_send_t0 = first_send_time
                        await self._set_speaking(True)
                        log.info(
                            f"[{self.device_id}] First streamed PCM period "
                            "sent to device"
                        )
                    _t_send = asyncio.get_event_loop().time()
                    await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                    send_seconds += asyncio.get_event_loop().time() - _t_send

            # The limiter holds a look-ahead tail; without this the last few
            # ms of every response are dropped. Inaudible on a long track and
            # obvious on a short announcement, which is the kind of thing that
            # goes unnoticed for months.
            if not self.cancel_event.is_set():
                pending.extend(stream_eq.flush())

            if pending and not self.cancel_event.is_set():
                chunk = bytes(pending)
                chunk += bytes(SPEAKER_BYTES - len(chunk))
                if first_send_time is None:
                    first_send_time = asyncio.get_event_loop().time()
                    self.playback_send_t0 = first_send_time
                    await self._set_speaking(True)
                    log.info(
                        f"[{self.device_id}] First streamed PCM period "
                        "sent to device"
                    )
                _t_send = asyncio.get_event_loop().time()
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                send_seconds += asyncio.get_event_loop().time() - _t_send
        finally:
            # NOT where speaking clears — see the note in stream_speaker.
            # One EOS terminates the complete response. Sending EOS per HTTP
            # chunk would make the device repeatedly prime and flush.
            try:
                await asyncio.shield(self.send_data(bytes([SPEAKER_EOS_TYPE])))
            except BaseException:
                pass

        return total_pcm, int(eq_seconds * 1000), first_send_time, int(send_seconds * 1000)


# The live device registry — keyed by device_id (ro.serialno).
# em_api receives a reference to this dict at startup.
_devices: dict[str, Device] = {}

# Peak event-loop lag observed since start, in ms (see
# event_loop_lag_monitor). Read by the API for /api/system/status.
_loop_lag_peak_ms: float = 0.0

# One arbiter for the fleet — elects a single responder when one
# utterance wakes several Echos (see em_arbiter.py).
_wake_arbiter = em_arbiter.WakeArbiter()

# Shell session coordination — keyed by device_id.
#
# _shell_pending:   Future resolved with the device ws when handle_shell receives it.
# _shell_dashboard: dashboard WebSocket set by em_api for interactive sessions.
_shell_pending:    dict[str, asyncio.Future] = {}
_shell_dashboard:  dict[str, object]         = {}


def get_device(device_id: str) -> Device | None:
    return _devices.get(device_id)


def _limiter_for(device):
    """Adapter: a Device's limiter config -> em_limiter.for_stream."""
    return em_limiter.for_stream(
        SPEAKER_RATE,
        device.limiter_enabled,
        device.limiter_threshold,
        device.limiter_release,
    )


def _guard_for(device):
    """Adapter: a Device's bass-guard config -> em_mbc.for_stream."""
    return em_mbc.for_stream(
        SPEAKER_RATE,
        device.bass_guard_enabled,
        device.bass_guard_db,
    )


async def _push_device_state(device: Device) -> None:
    """Push current transient device state to dashboard clients."""
    await api._push_event({
        "type":      "device_update",
        "device_id": device.device_id,
        "state": {
            "connected": True,
            "speaking":  device.speaking,
            "muted":     device.muted,
            "listening": device.listening,
            "thinking":  device.thinking,
        },
    })


# ─── LED helpers ──────────────────────────────────────────────────────────────

def _make_leds(r, g, b):
    return [{"id": i, "r": r, "g": g, "b": b} for i in range(NUM_LEDS)]


async def leds_off(device: Device):
    if device.led_anim_capable:
        await device.send_led_anim({"pattern": "off"})
    else:
        await device.set_leds(_make_leds(0, 0, 0))


# Turn outcomes that get a distinguishing ring cue at turn end. Everything
# else ("ok", "cancelled") ends silently: the user either heard a reply or
# pressed the button themselves, so a cue would be noise.
#
# Rhythm carries the meaning, not colour — a new colour would collide with
# red (mute), orange (link down) or cyan (volume), and the point of the
# cue is to be understandable without a legend. One slow throb reads as
# "nothing heard"; fast blinks read as "something went wrong".
_OUTCOME_ANIM = {
    "no_speech":     "nospeech_anim",
    "no_tts":        "error_anim",
    "tts_error":     "error_anim",
    "timeout":       "error_anim",
    "stream_timeout": "error_anim",
    # Not an error — HA took the wake word and ended the run deliberately,
    # which the satellite setup flow does on every prompt. Needs a cue
    # because the turn is over in milliseconds: without one the ring lights
    # and clears too fast to register, and a device that worked perfectly
    # looks like it glitched.
    "pipeline_refused": "ack_anim",
}


async def _leds_turn_end(device: Device):
    """
    Clear the ring at turn end, playing a brief self-clearing cue first if
    the turn ended in a way the user would otherwise have no signal for.

    The cue anims carry a 1s TTL, so the device retires them on its own
    ticker with no follow-up message — nothing to leak if the controller
    dies in between, and a continuation/barge repaint simply supersedes it
    via the animator's generation counter.
    """
    outcome = device.last_turn_outcome
    device.last_turn_outcome = None
    key = _OUTCOME_ANIM.get(outcome or "")
    # Barge-in re-enters a fresh turn immediately and repaints listening —
    # a cue there would flash for a few frames and read as a glitch.
    if key and device.led_anim_capable and not device.barge_detected:
        anim = device.led_scene.get(key)
        if anim:
            log.info(f"[{device.device_id}] Turn ended '{outcome}' — ring cue")
            await device.send_led_anim(anim)
            return
    await leds_off(device)


async def leds_listening(device: Device):
    if device.led_anim_capable:
        await device.send_led_anim(device.led_scene["listening_anim"])
    else:
        await device.set_leds(device.led_scene["listening"], listening=True)


async def leds_spin_green(device: Device, stop_event: asyncio.Event):
    # Name is historical — the spinner renders whatever the device's scene
    # says (head+trail dot for solid scenes, rotating palette for pride).
    #
    # led_anim firmware animates locally: one message starts the spinner,
    # the device runs it on its own ticker (controller event-loop stalls
    # and WiFi jitter can't judder it), and this task just waits to send
    # the stop. Legacy firmware falls back to controller-rendered frames.
    if device.led_anim_capable:
        try:
            await device.send_led_anim(device.led_scene["spin_anim"])
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await leds_off(device)
        return
    spin_frame = device.led_scene["spin_frame"]
    pos = 0
    try:
        while not stop_event.is_set():
            await device.set_leds(spin_frame(pos))
            pos = (pos + 1) % NUM_LEDS
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        pass
    finally:
        await leds_off(device)


# ─── Audio conversion ─────────────────────────────────────────────────────────

# (The numpy linear-interpolation resample_to_48k that used to live here is
# gone: _fetch_tts_audio now decodes at SPEAKER_RATE directly, with ffmpeg
# doing any rate conversion — and HA transcodes to 48kHz at source when it
# honours the media player's declared supported_formats.)


# ─── Voice pipeline ───────────────────────────────────────────────────────────


async def _barge_watcher(device: Device, playback_started: asyncio.Event):
    """
    Wake-word watcher spanning the thinking AND playback phases (barge-in,
    §3.2). Started at STT_VAD_END (on_thinking); before that the user's own
    command is streaming and a wake word in it is just speech.

    With barge-in enabled the mic keeps streaming through the whole turn
    (the device's AEC subtracts its own speaker output during playback) and
    oww_paused routes frames to voice_queue — which nothing else reads
    after STT ends, so this watcher drains and scores it with a dedicated
    openwakeword instance (the main wake listener task is blocked awaiting
    the turn).

    The threshold is phase-dependent because the acoustics are: during
    playback the speaker is ~25dB louder than the person at the mic, so
    speech-over-TTS scores are depressed and barge_threshold sits well
    below the wake threshold (~0.05–0.10). During thinking nothing is
    playing — scores are normal, and using the low barge threshold there
    would fire on random speech — so detection is two-tier: a single frame
    at the normal wake threshold fires immediately, and two CONSECUTIVE
    frames at a low tier (0.4× wake threshold, floored at 0.2) also fire.
    The low tier exists because a genuine barge attempt over the watcher's
    cold-started model can plateau below the wake threshold (observed
    2026-07-12: 0.240/0.242 on consecutive frames vs threshold 0.50 —
    missed, and the unwanted answer played in full), while random speech
    near-misses are isolated single frames — two elevated frames in a row
    is wake-word-shaped evidence.

    On detection: set barge_detected + cancel_event. During playback that
    aborts stream_speaker and the drain sleep, plus a device speaker_flush
    so the interruption is audible immediately (not after ~1.4s of queued
    TTS). During thinking there's no audio to flush — instead the in-flight
    HA pipeline is cancelled (local-only; any late HA result is discarded).
    """
    loop = asyncio.get_event_loop()
    if device._barge_model is None or device._barge_model_key != device.oww_model:
        name = device.oww_model
        log.info(f"[{device.device_id}] Barge-in: loading watcher model {name}")
        device._barge_model = await loop.run_in_executor(
            None, lambda: OWWModel(wakeword_models=[name])
        )
        device._barge_model_key = name
    model = device._barge_model
    # _barge_model_key stays the raw owwModel value (staleness compare
    # above); scoring needs the openwakeword prediction key (path → stem).
    barge_pred_key = em_oww_models.prediction_key(device._barge_model_key)
    model.reset()
    # reset() seeds the classifier's window with embeddings of random noise,
    # so the first FEATURE_WINDOW chunks score that noise as much as the room.
    # This watcher is where that hurt most: it reset and scored immediately,
    # and twice cancelled a turn the user was waiting on, at 0.867 and 0.700
    # within 10 chunks of starting. See em_oww_warmup.
    warmup = em_oww_warmup.WarmupGate()

    # Drop anything queued before the watcher started (command tail,
    # silence) — only fresh audio should be scored.
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    # Playback phase: bargeInThreshold is used as-is — deliberately NOT
    # floored at the wake threshold. The max() clamp guarded against
    # residual echo waking the device before AEC worked; measured with
    # working AEC (2026-07-08), self-echo peaks at 0.004 converged / 0.055
    # worst-case-unconverged, while real speech over TTS scores 0.118+ —
    # the echo is 25dB louder than the speaker at the mic, so
    # speech-over-TTS scores are inherently depressed and a sub-wake
    # threshold (~0.10) is both safe and necessary. Thinking phase uses the
    # normal wake threshold (see docstring).
    threshold = device.barge_threshold  # refined per-frame by phase below
    prev_score = 0.0  # previous frame's score — both phases need two
    buf = bytearray()
    # Observability: the watcher used to log only on detection, which made a
    # failed barge-in attempt indistinguishable from "no frames arrived at
    # all" (mic not streaming) or "frames arrived but scored ~0" (AEC residual
    # burying the speech). Track both and always report on exit.
    peak   = 0.0
    frames = 0
    # Frame RMS (0.0–1.0) discriminates the failure modes peak alone can't:
    # rms >> noise floor means echo is reaching the watcher raw (AEC off or
    # ineffective — delay mismatch / clipped-nonlinear echo); rms ≈ floor
    # with a low peak means AEC is eating the user's speech along with the
    # echo (over-suppression / divergence during double-talk).
    rms_sum = 0.0
    rms_max = 0.0
    try:
        while True:
            payload = await device.voice_queue.get()
            if payload is None or isinstance(payload, str):
                buf.clear()
                prev_score = 0.0  # sentinel = stream discontinuity; frames
                # across it are not consecutive for either two-frame rule
                continue
            buf.extend(payload)
            while len(buf) >= CHUNK_BYTES:
                frame = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                rms_sum += rms
                rms_max  = max(rms_max, rms)
                prediction = await loop.run_in_executor(None, model.predict, samples)
                score = prediction.get(barge_pred_key, 0.0)
                frames += 1
                trusted = warmup.feed()
                in_playback = playback_started.is_set()
                # em_barge owns both phases. Extracted because this shipped
                # untested and wrong: the playback branch fired on ONE frame
                # at a bar ten times lower than the wake threshold, while
                # scoring the device's own speech, and cut long responses off
                # mid-sentence (measured 2026-08-20).
                threshold = (device.barge_threshold if in_playback
                             else device.oww_threshold)
                fired, fire_note = em_barge.decide(
                    score=score,
                    prev_score=prev_score,
                    in_playback=in_playback,
                    barge_threshold=device.barge_threshold,
                    wake_threshold=device.oww_threshold,
                )
                if fired and not trusted:
                    log.info(
                        f"[{device.device_id}] Barge watcher: {fire_note} "
                        f"ignored — openwakeword warm-up, "
                        f"{warmup.progress()} chunks since reset"
                    )
                    fired = False
                prev_score = score
                if score > peak:
                    peak = score
                    if score >= 0.1:
                        log.info(
                            f"[{device.device_id}] Barge watcher: score {score:.3f} "
                            f"(threshold {threshold:.2f})"
                        )
                if fired:
                    phase = "playback" if in_playback else "thinking"
                    log.info(
                        f"[{device.device_id}] Barge-in: wake word during {phase} "
                        f"({fire_note}) — cancelling turn"
                    )
                    db.log_device(
                        device.device_id, "info", "device",
                        f"Barge-in during {phase} (score={score:.3f})"
                    )
                    device.barge_detected = True
                    # Wake detail for the interrupting turn's persistent
                    # record — popped when the turn loop re-enters
                    # trigger_voice_turn with trigger "barge-in".
                    device.last_wake = {
                        "model":       barge_pred_key,
                        "score":       round(float(score), 4),
                        "threshold":   float(threshold),
                        "noise_floor": round(device.noise_floor, 5),
                    }
                    device.cancel_event.set()
                    if in_playback:
                        await device.send_control({"type": "speaker_flush"})
                        # HA's run is normally over by now (RUN_END follows
                        # TTS_END, and we only reach playback at TTS_END), but
                        # a barge in the first milliseconds of audio can beat
                        # it. Serialise anyway — the interrupting turn is the
                        # thing that pays if we lose that race.
                        esphome.abort_ha_run(device.device_id)
                    else:
                        # Nothing is playing — HA is mid-pipeline, and an
                        # interrupting turn is about to start on the same
                        # connection. The protocol carries no run id, so the
                        # old run MUST be aborted upstream first or its tail
                        # events land on the new turn and kill it
                        # (pipeline_refused, 5 of 5 attempts, 2026-08-17).
                        esphome.cancel_voice_turn(device.device_id, abort_ha=True)
                    return
    finally:
        rms_mean = rms_sum / frames if frames else 0.0
        log.info(
            f"[{device.device_id}] Barge watcher done: {frames} frames "
            f"({frames * 80}ms) scored, peak={peak:.3f}, threshold={threshold:.2f}, "
            f"rms mean={rms_mean:.4f} max={rms_max:.4f} "
            f"(device noise floor {getattr(device, 'noise_floor', 0.0):.4f})"
        )


async def _run_post_turn_playback(device: Device, voice_response: bytes) -> None:
    """
    Post-turn timing concern: EQ, stream to device, acoustic-feedback wait.

    voice_response is 48kHz mono S16_LE PCM (_fetch_tts_audio decodes at the
    wire rate now — no controller-side resample). Returns once the device
    audio buffer has drained (or cancel_event fires), so the caller can
    safely restart the mic without acoustic feedback into the next turn.
    """
    # Built here rather than inside _prepare_pcm so the stages survive the
    # call and can be asked what they actually did — see em_eq.describe_*.
    # One response is one buffer, so these are per-response instances and
    # carry no state between turns.
    _limiter = _limiter_for(device)
    _guard   = _guard_for(device)
    log.info(
        f"[{device.device_id}] Output chain: "
        f"{em_eq.describe_chain(device.eq_bands, device.eq_loudness, _limiter, _guard)}"
    )
    # EQ is a solid numpy crunch (hundreds of ms for a long response) — run
    # it off the event loop, which otherwise freezes every device's LED
    # frames, shell proxying, and WS handling right as playback starts
    # (observed as spinner stutter and console typing judder).
    def _prepare_pcm() -> bytes:
        return em_eq.apply(voice_response, SPEAKER_RATE, device.eq_bands,
                           device.eq_loudness, limiter=_limiter,
                           guard=_guard)

    _t_eq0 = asyncio.get_event_loop().time()
    speaker_pcm = await asyncio.get_event_loop().run_in_executor(None, _prepare_pcm)
    device.playback_eq_ms = int(
        (asyncio.get_event_loop().time() - _t_eq0) * 1000
    )
    log.info(
        f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes "
        f"({len(speaker_pcm)//SPEAKER_BYTES} periods) — "
        f"{em_eq.describe_activity(_limiter, _guard)}"
    )
    cancel_task    = asyncio.create_task(device.cancel_event.wait())
    device.playback_done.clear()
    done_task      = asyncio.create_task(device.playback_done.wait())
    stream_task    = asyncio.create_task(device.stream_speaker(speaker_pcm))
    t_stream_start = asyncio.get_event_loop().time()
    # Opens the delivery window measured against the device's
    # playback_stats report (see Device.playback_send_t0).
    device.playback_send_t0 = t_stream_start

    done, _ = await asyncio.wait(
        [stream_task, cancel_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if cancel_task in done:
        log.info(f"[{device.device_id}] Cancelled during playback")
        stream_task.cancel()
    else:
        if not device.cancel_event.is_set():
            # Wait for the DEVICE to say it finished, rather than estimating.
            #
            # The old code slept `audio_duration - elapsed` and declared
            # completion. Two things made that wrong, and both bite hardest
            # on exactly the links that need the most patience: `elapsed` is
            # socket-write time (which completes near-instantly however slow
            # the wire is) and was *subtracted*, and the estimate had no
            # visibility of how long the device's own buffer took to drain.
            # Measured 2026-07-24: the ring cleared 6.1s before the audio
            # actually stopped on a Retreat turn, 3.2s early on Lounge.
            #
            # playback_stats is emitted once the device's audio channel has
            # drained after EOS, so it is the real end of audio. The timeout
            # is only a backstop for the report never arriving (device drop,
            # pre-v2.9 firmware): generous, because ending the turn early is
            # the failure we are fixing. cancel_event is still raced — a
            # barge-in or a mute usually lands in this window, and an
            # uncancellable wait here is what caused the 5.7s dead window
            # fixed on 2026-07-10.
            audio_duration = len(speaker_pcm) / (SPEAKER_RATE * 2) + SPEAKER_PRIME_SECONDS
            elapsed        = asyncio.get_event_loop().time() - t_stream_start
            device.playback_send_ms = int(elapsed * 1000)
            timeout        = audio_duration * 2 + 10.0
            log.info(
                f"[{device.device_id}] Socket write took {elapsed:.1f}s "
                f"(NOT delivery — see delivery_ms), awaiting device "
                f"playback_stats (est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
            )
            timeout_task = asyncio.create_task(asyncio.sleep(timeout))
            await asyncio.wait(
                [done_task, cancel_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            timeout_task.cancel()
            if device.cancel_event.is_set():
                log.info(f"[{device.device_id}] Cancelled during playback drain")
            elif done_task.done():
                actual = asyncio.get_event_loop().time() - t_stream_start
                log.info(
                    f"[{device.device_id}] Playback complete "
                    f"(device-confirmed after {actual:.1f}s, est {audio_duration:.1f}s)"
                )
            else:
                # Ring held the full backstop. Either the device never
                # reported (worth knowing) or delivery was pathological.
                log.warning(
                    f"[{device.device_id}] Playback completion timed out after "
                    f"{timeout:.1f}s with no playback_stats — clearing ring anyway"
                )

    # The real end of audio, not the end of the socket write. The device
    # reports playback_stats once its audio channel drains after EOS, and
    # everything above waits for exactly that — so this is the one place that
    # knows the speaker has actually stopped. Clearing it in the stream task's
    # finally instead dropped the tile out of Speaking seconds early (the write
    # completes near-instantly), which is the same mistake the ring made until
    # 2026-07-24.
    await device._set_speaking(False)
    cancel_task.cancel()
    done_task.cancel()

async def _meter_at_playback_start(pcm_chunks, on_start):
    """
    Pass PCM through untouched, firing `on_start` when the device will have
    begun playing it.

    The device holds audio until roughly SPEAKER_PRIME_SECONDS is queued, or
    until EOS for a response shorter than that (primePeriods, pcm_speaker.go).
    Counting bytes handed to the streamer tracks that closely enough — the
    socket write completes near-instantly however slow the link is, which is
    the same property that makes send_ms useless as a delivery measure.

    Exhaustion fires it too, so a two-word answer still gets a meter. If the
    stream is cancelled or yields nothing, it never fires and the spinner
    simply stays up until the turn's ring cleanup — the failure direction
    that looks like "still working" rather than "dead".
    """
    prime_bytes = int(SPEAKER_PRIME_SECONDS * SPEAKER_RATE * 2)
    sent = 0
    fired = False
    async for chunk in pcm_chunks:
        yield chunk
        if not fired:
            sent += len(chunk)
            if sent >= prime_bytes:
                fired = True
                await on_start()
    if not fired:
        await on_start()


async def _run_streaming_post_turn_playback(device: Device, pcm_chunks) -> int:
    """
    Play decoded HA TTS while the HTTP response is still arriving.

    The voice-turn path keeps one ffmpeg decoder, one stateful EQ chain, and
    one device 0x02...0x03 stream alive across all synthesized utterances.
    Closing any layer at an arbitrary network boundary would corrupt decoding,
    reset the EQ filters, or turn each chunk into a separate announcement.
    """
    log.info(
        f"[{device.device_id}] Streaming EQ: bands={device.eq_bands} "
        f"loudness={device.eq_loudness}"
    )
    stream_eq = em_eq.StreamingEQ(
        SPEAKER_RATE,
        device.eq_bands,
        device.eq_loudness,
        limiter=_limiter_for(device),
        guard=_guard_for(device),
    )
    # Cleared BEFORE streaming starts: the device sets it when its audio
    # channel drains after EOS, and a stale set from the previous response
    # would end this turn the moment we started waiting.
    device.playback_done.clear()
    cancel_task = asyncio.create_task(device.cancel_event.wait())
    done_task   = asyncio.create_task(device.playback_done.wait())
    stream_task = asyncio.create_task(
        device.stream_speaker_chunks(pcm_chunks, stream_eq)
    )
    t_stream_start = asyncio.get_event_loop().time()

    try:
        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during streamed playback")
            return 0

        total_pcm, eq_ms, first_send_time, send_ms = stream_task.result()
        device.playback_eq_ms = eq_ms
        device.playback_send_ms = send_ms
        stream_elapsed = asyncio.get_event_loop().time() - t_stream_start
        audio_duration = total_pcm / (SPEAKER_RATE * 2) + SPEAKER_PRIME_SECONDS

        if not total_pcm:
            log.info(f"[{device.device_id}] Streamed response contained no audio")
            return 0

        # Wait for the DEVICE to say it finished, exactly as the buffered path
        # does — do NOT sleep a computed `audio_duration - elapsed`.
        #
        # That estimate was removed on 2026-07-24 because it has no visibility
        # of the device's own buffer and cleared the ring 6.1s early on Retreat
        # and 3.2s on Lounge. Streaming makes it worse, not better: the time
        # spent streaming already covers most of the audio duration, so the
        # remainder computes to ~0 and the wait vanishes entirely while up to
        # ~5.5s is still queued in audioChanDepth. playback_stats is emitted
        # once that channel drains after EOS, so it is the real end of audio.
        # The timeout is only a backstop for the report never arriving (device
        # drop, pre-v2.9 firmware). cancel_event is still raced so a barge-in
        # or mute lands promptly.
        timeout = audio_duration * 2 + 10.0
        log.info(
            f"[{device.device_id}] Streamed {total_pcm} bytes "
            f"({total_pcm//SPEAKER_BYTES} periods) in {stream_elapsed:.1f}s "
            f"while HA generated audio (socket writes {send_ms}ms — NOT "
            f"delivery, see delivery_ms); awaiting device playback_stats "
            f"(est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
        )
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))
        try:
            await asyncio.wait(
                [done_task, cancel_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)

        if device.cancel_event.is_set():
            log.info(f"[{device.device_id}] Cancelled during streamed buffer drain")
        elif done_task.done():
            log.info(f"[{device.device_id}] Streamed playback complete (device reported)")
        else:
            log.warning(
                f"[{device.device_id}] No playback_stats after {timeout:.1f}s — "
                f"continuing (device drop or pre-v2.9 firmware)"
            )
        return total_pcm
    finally:
        # See _run_post_turn_playback: the end of audio is what the DEVICE
        # reports, not when the last byte reached the socket. In the finally so
        # a cancel or an error leaves the tile idle rather than stuck Speaking.
        await device._set_speaking(False)
        for t in (cancel_task, done_task):
            t.cancel()
        if not stream_task.done():
            stream_task.cancel()
        await asyncio.gather(cancel_task, done_task, stream_task, return_exceptions=True)
        # Close the async generator explicitly rather than leaving it to the
        # event loop's asyncgen hooks: its finally is what kills ffmpeg, and on
        # a barge-in (a routine path here) deferring that to GC leaves a decoder
        # running for an indeterminate window.
        aclose = getattr(pcm_chunks, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as e:
                log.debug(f"[{device.device_id}] TTS stream close: {e}")


async def _run_voice_locked(device: Device, trigger_label: str = "unknown", is_wakeword: bool = False):
    """
    is_wakeword: explicit flag for whether this turn was triggered by wake-
    word detection (as opposed to a button press). Used to decide preroll
    discard (see C3) — kept as its own parameter rather than inferred by
    parsing trigger_label (which is a free-form string meant for logging/
    trace display, not a control-flow key) so a future change to the label
    format can't silently change behaviour here.
    """
    drained = 0
    while not device.mic_queue.empty():
        try:
            device.mic_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    if drained:
        log.info(f"[{device.device_id}] Voice turn: drained {drained} stale frames")
    # Voice preempts music: pause an active media session for the whole
    # conversation (incl. continuations) and resume it afterwards. The
    # matching resume_interrupted below only fires if this interrupt
    # actually paused something.
    await em_player.interrupt(device.device_id)
    try:
        async with device.voice_lock:
            log.info(f"[{device.device_id}] Voice turn starting (esphome mode)")
            device.listening = True
            await leds_listening(device)
            await _push_device_state(device)

            stop_spin = asyncio.Event()
            spin_task = None
            # Barge-in watcher state — reset per turn iteration below.
            watcher          = None
            playback_started = asyncio.Event()

            async def stop_watcher():
                nonlocal watcher
                if watcher is None:
                    return
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning(f"[{device.device_id}] Barge watcher error: {e}")
                watcher = None

            async def cleanup_esphome():
                device.thinking  = False
                device.listening = False
                await _push_device_state(device)
                stop_spin.set()
                if spin_task and not spin_task.done():
                    spin_task.cancel()
                    try:
                        await spin_task
                    except asyncio.CancelledError:
                        pass
                await _leds_turn_end(device)

            async def on_thinking_esphome():
                nonlocal spin_task, watcher
                if stop_spin.is_set():
                    return  # cleanup already ran; turn is over
                device.thinking  = True
                device.listening = False
                await _push_device_state(device)
                log.info(f"[{device.device_id}] Thinking (esphome)")
                if not device.cancel_event.is_set() and (
                    spin_task is None or spin_task.done()
                ):
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                # Barge-in watcher starts here, not at playback: STT has
                # ended (VAD_END), so anything on the mic from now on is
                # a potential interruption. Spans thinking → playback;
                # cancelled in the turn loop's finally.
                if device.barge_in_enabled and (
                    watcher is None or watcher.done()
                ):
                    watcher = asyncio.create_task(
                        _barge_watcher(device, playback_started)
                    )

            async def post_turn_play_esphome(pcm_chunks):
                nonlocal spin_task, watcher
                if spin_task is None or spin_task.done():
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                meter_refresh_task = None
                if device.led_anim_capable:
                    # Playback ring: throb with the response's live audio
                    # level (device-side "meter" pattern, RMS measured at
                    # the ALSA write). Replaces the thinking spinner on
                    # the device; spin_task keeps waiting on stop_event
                    # and its finally still clears the ring at turn end.
                    #
                    # RAISED WHEN AUDIO STARTS, NOT WHEN PLAYBACK IS SET UP.
                    # The meter renders the live speaker RMS, so before the
                    # device's ALSA write begins it draws an unlit ring. On
                    # the buffered path that was invisible (the fetch had
                    # already completed, so frames flushed at socket speed),
                    # but streaming moves the fetch+decode INSIDE playback:
                    # sending it here left the ring dark from the end of the
                    # spinner until HA returned audio — seconds on a slow
                    # response, and indistinguishable from a failed turn
                    # (user report 2026-07-31). The spinner keeps running
                    # until _meter_on fires, so the handover is continuous.
                    #
                    # A streamed response has no known duration up front.
                    # Refresh the same bounded dead-man TTL while PCM arrives:
                    # long speech cannot outlive its meter, but a controller
                    # crash still lets the device clear the ring on its own.
                    meter = dict(device.led_scene["meter_anim"])
                    meter["ttlSec"] = em_scenes.meter_ttl(0.0)

                    async def _refresh_meter_dead_man() -> None:
                        refresh_seconds = max(1.0, meter["ttlSec"] / 2)
                        while True:
                            await asyncio.sleep(refresh_seconds)
                            await device.send_led_anim(meter)

                    async def _meter_on() -> None:
                        nonlocal meter_refresh_task
                        if meter_refresh_task is not None:
                            return
                        await device.send_led_anim(meter)
                        meter_refresh_task = asyncio.create_task(
                            _refresh_meter_dead_man()
                        )

                    pcm_chunks = _meter_at_playback_start(pcm_chunks, _meter_on)

                if device.barge_in_enabled:
                    # Barge-in (§3.2): keep the mic running through
                    # playback — the device's AEC subtracts the speaker
                    # output, which is what makes this safe (enable AEC
                    # before enabling barge-in; without it the watcher
                    # scores raw echo and the raised threshold is the
                    # only defence). The pre-AEC problems the mic_stop
                    # guarded against are gone: AGC no longer exists on
                    # the wake stream (v2.7.0) and echo content is
                    # cancelled at the source (v2.7.3).
                    #
                    # The watcher normally exists already (started at
                    # thinking onset); the phase flag switches it to the
                    # playback threshold. Defensive create for turns
                    # that reach TTS without an STT_VAD_END.
                    playback_started.set()
                    if watcher is None or watcher.done():
                        watcher = asyncio.create_task(
                            _barge_watcher(device, playback_started)
                        )
                else:
                    # Acoustic-feedback guard (barge-in off): stop the mic
                    # BEFORE playback, not just in the post-turn finally.
                    # With the mic running through TTS pre-AEC, the device
                    # processed its own speaker echo (63-65 junk frames per
                    # turn measured 2026-07-06) and sent it upstream on the
                    # same Wi-Fi radio receiving the TTS frames (speaker
                    # underruns → audible stutter). The finally's mic_stop
                    # stays as a safety net (StopMic no-ops when already
                    # stopped); restart is owned by the continuation branch /
                    # wake listener / button handler as before.
                    await device.mic_stop()

                try:
                    return await _run_streaming_post_turn_playback(
                        device, pcm_chunks
                    )
                finally:
                    if meter_refresh_task is not None:
                        meter_refresh_task.cancel()
                        await asyncio.gather(
                            meter_refresh_task, return_exceptions=True
                        )

            # P0-1: no mic_start_turn() here on the initial (wake/button)
            # entry — for a wake turn the stream is already running on
            # ch6 and oww_paused routes frames to voice_queue. The
            # acoustic-feedback guard is mic_stop in
            # post_turn_play_esphome, sent immediately before TTS
            # playback; the finally below is only the safety net.
            #
            # Continuation loop: if HA sets continue_conversation on
            # INTENT_END, re-trigger immediately after TTS+drain rather
            # than returning to OWW idle. The reference implementation
            # (linux-voice-assistant) uses a 0.5s settle delay after TTS
            # before opening the mic — that's already covered by
            # the streaming playback path's buffer drain sleep, so no
            # additional delay is needed here.
            #
            # C2 fix (2026-07-05 review): the `finally` below runs
            # device.mic_stop() on every iteration, including the one
            # that decides to continue — previously nothing ever put the
            # stream back before looping into the next trigger_voice_turn,
            # so a continuation turn streamed from a stopped mic and
            # silently timed out as no_speech every time. Fixed by
            # calling device.mic_start() (no lock_mic — same ch6 stream
            # as the wake path; no-ops if somehow already running) in the
            # continuation branch, before looping.
            #
            # C3 fix: preroll_discard is 0 for button/continuation turns
            # (no wake-word tail to remove — discarding real audio here
            # just clips the first word/words, the exact bug P0-1 fixed
            # on the wake path) and VOICE_PREROLL_DISCARD only for the
            # initial wakeword-triggered turn.
            turn_label      = trigger_label
            preroll_discard = esphome.VOICE_PREROLL_DISCARD if is_wakeword else 0
            while True:
                should_continue = False
                try:
                    should_continue = await esphome.trigger_voice_turn(
                        device=device,
                        on_thinking=on_thinking_esphome,
                        post_turn_play=post_turn_play_esphome,
                        trigger_label=turn_label,
                        preroll_discard=preroll_discard,
                    )
                finally:
                    # Watcher spans thinking→playback and is owned here:
                    # every exit path (normal, barge, error, cancel)
                    # must stop it before the next iteration re-arms.
                    await stop_watcher()
                    # On barge the mic stays up: the user's follow-up
                    # command is already flowing into voice_queue and a
                    # mic_stop/start cycle here would drop the words
                    # spoken in the same breath as the wake word.
                    if not device.barge_detected:
                        await device.mic_stop()
                    await cleanup_esphome()
                    log.info(f"[{device.device_id}] Voice turn complete (esphome mode)")

                if device.barge_detected:
                    # Barge-in: the watcher cancelled playback because
                    # the wake word was spoken over it. Re-enter a fresh
                    # turn immediately — same shape as continuation, but
                    # with the wake-word preroll discard (there IS a
                    # "…rhasspy" tail to drop this time).
                    device.barge_detected = False
                    device.cancel_event.clear()
                    log.info(f"[{device.device_id}] Barge-in: starting interrupting turn")
                    await device.mic_start()  # defensive no-op if running
                    # Re-arm listening state — cleanup_esphome() in the
                    # finally just turned the ring off, which left the
                    # device dark while it was actually listening for the
                    # interrupting command (looked dead — user report
                    # 2026-07-08). Same re-arm as the continuation branch;
                    # no voice_queue drain here though, the follow-up
                    # words spoken after "hey rhasspy" are already in it.
                    device.listening = True
                    await leds_listening(device)
                    await _push_device_state(device)
                    turn_label      = "barge-in"
                    preroll_discard = esphome.VOICE_PREROLL_DISCARD
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                    continue

                if should_continue and not device.cancel_event.is_set():
                    log.info(f"[{device.device_id}] Continuing conversation (HA requested)")
                    # C2 fix: put the mic stream back before looping —
                    # the finally above just stopped it, and the next
                    # trigger_voice_turn will read from voice_queue,
                    # which is fed only while the device stream is
                    # running. No lock_mic — same ch6 stream as wake.
                    await device.mic_start()
                    # Fresh stream starts with the VAD gate closed — the
                    # user must speak again from zero, same onset cost
                    # as any post-mic_stop restart. Acceptable for v1 of
                    # continuation (see review C2 wrinkle note); §3.4's
                    # device preroll ring will fix this properly later.
                    # Drain stale frames accumulated during TTS playback
                    # before the next turn begins — same as post-wake drain.
                    drained = 0
                    while not device.voice_queue.empty():
                        try:
                            device.voice_queue.get_nowait()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
                    if drained:
                        log.debug(f"[{device.device_id}] Continuation: drained {drained} stale frames")
                    # Re-arm listening state for the follow-up turn.
                    device.listening = True
                    await leds_listening(device)
                    await _push_device_state(device)
                    turn_label      = "continuation"
                    preroll_discard = 0
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    # Fresh phase flag for the next turn's watcher.
                    playback_started = asyncio.Event()
                else:
                    break

    finally:
        # Drain voice_queue BEFORE clearing oww_paused. If we clear first,
        # handle_data immediately starts routing new frames to mic_queue —
        # correct. But voice_queue still contains frames that arrived during
        # the turn (post-TTS playback, during the buffer drain sleep). Those
        # frames will sit in voice_queue until the NEXT wake detection flips
        # oww_paused back, at which point they arrive at _stream_mic_audio as
        # preamble before the user has said anything — Whisper then transcribes
        # 10+ seconds of ambient noise mixed with the actual utterance.
        # Draining here, while oww_paused is still set, ensures voice_queue is
        # empty before routing flips. The post-turn drain in wake_word_listener
        # (after _run_voice_locked returns) becomes a belt-and-braces no-op.
        _drained = 0
        while not device.voice_queue.empty():
            try:
                device.voice_queue.get_nowait()
                _drained += 1
            except asyncio.QueueEmpty:
                break
        if _drained:
            log.info(
                f"[{device.device_id}] oww_paused drain: "
                f"{_drained} stale frames cleared before routing flip"
            )
        device.oww_paused.clear()
        log.info(f"[{device.device_id}] oww_paused cleared")
        # Release the arbitration claim so another device answering a
        # genuinely new utterance isn't suppressed by a stale window.
        _wake_arbiter.release(device.device_id)
        # Conversation over — un-pause a media session this turn preempted.
        await em_player.resume_interrupted(device.device_id)


# ─── Wake word listener ───────────────────────────────────────────────────────

async def wake_word_listener(device: Device):
    loop = asyncio.get_event_loop()

    current_model_name = device.oww_model
    current_speex_ns    = device.oww_speex_ns
    log.info(
        f"[{device.device_id}] OWW: loading model {current_model_name} "
        f"(speex_ns={current_speex_ns})"
    )
    model = await loop.run_in_executor(
        None,
        lambda: OWWModel(
            wakeword_models=[current_model_name],
            enable_speex_noise_suppression=current_speex_ns,
        ),
    )
    # NB: for custom models owwModel is a file path but openwakeword keys
    # the prediction dict by the filename stem — never score by the raw name.
    model_key = em_oww_models.prediction_key(current_model_name)

    log.info(f"[{device.device_id}] OWW: starting (initial threshold={device.oww_threshold:.3f})")
    await device.mic_start()

    buf = bytearray()
    # openwakeword seeds its classifier window with embeddings of random noise
    # on construction AND on every reset(), so scores are partly scores of that
    # noise until the window has refilled. Un-warmed at construction to match
    # the model above; re-armed at every reset() below. See em_oww_warmup.
    warmup = em_oww_warmup.WarmupGate()
    last_near_miss_log_ts = 0.0  # Q4: rate-limit near-miss INFO logging to 1/2s
    nm_pending = 0    # near-misses buffered since the last hourly-rollup flush
    nm_max     = 0.0  # highest buffered near-miss score
    dead_streak = 0   # consecutive 10s mic_queue timeouts (resets on any frame)
    try:
        while True:
            if device.oww_model != current_model_name or device.oww_speex_ns != current_speex_ns:
                new_name  = device.oww_model
                new_speex = device.oww_speex_ns
                log.info(
                    f"[{device.device_id}] OWW: reloading model "
                    f"{current_model_name} → {new_name} "
                    f"(speex_ns {current_speex_ns} → {new_speex})"
                )
                try:
                    _n = new_name
                    _s = new_speex
                    new_model = await loop.run_in_executor(
                        None,
                        lambda: OWWModel(
                            wakeword_models=[_n],
                            enable_speex_noise_suppression=_s,
                        ),
                    )
                    model             = new_model
                    model_key         = em_oww_models.prediction_key(new_name)
                    current_model_name = new_name
                    current_speex_ns  = new_speex
                    buf.clear()
                    # A new model is noise-seeded exactly like a reset one.
                    warmup.reset()
                    log.info(f"[{device.device_id}] OWW: model reloaded → {new_name} (speex_ns={new_speex})")
                except Exception as e:
                    log.error(
                        f"[{device.device_id}] OWW: failed to load {new_name} "
                        f"(speex_ns={new_speex}): {e} "
                        f"— reverting to {current_model_name} (speex_ns={current_speex_ns})"
                    )
                    device.oww_model     = current_model_name
                    device.oww_speex_ns  = current_speex_ns
            try:
                payload = await asyncio.wait_for(
                    device.mic_queue.get(), timeout=10.0
                )
            except asyncio.TimeoutError:
                # The wake stream is ungated and continuous (device sends
                # every 80ms, silence included — hardware mute still produces
                # zero-filled frames), so 10s of nothing on mic_queue means
                # the stream died — NOT ordinary silence, as it did when the
                # device VAD gate existed on this stream. Exception: during a
                # voice turn frames route to voice_queue instead, so an idle
                # mic_queue is expected while oww_paused is set.
                if device.oww_paused.is_set():
                    continue
                if device.muted:
                    # Hardware mute is device-sovereign: the device rejects
                    # every mic_start while muted, so a silent stream is the
                    # expected state — retrying just spams both logs every
                    # 10s. The device restarts its own wake stream on unmute
                    # (and device.muted clears with the mute_state message),
                    # so the watchdog resumes naturally if that ever fails.
                    dead_streak = 0
                    continue
                dead_streak += 1
                if dead_streak < 3:
                    log.warning(
                        f"[{device.device_id}] OWW: no mic frames for 10s on the "
                        f"continuous wake stream — sending defensive mic_start"
                    )
                    await device.mic_start()
                else:
                    # Bare mic_start hasn't worked — the classic cause is a
                    # zombie stream device-side still holding micActive
                    # against a superseded data connection (Office,
                    # 2026-07-16: deaf 4.7h while every bare mic_start was
                    # refused "already active"). mic_stop releases whatever
                    # stream exists, wherever it points; the fresh mic_start
                    # then lands on the live connection. Safe at this point
                    # by construction: 30s+ of zero frames with no turn in
                    # flight (oww_paused checked above) means there is no
                    # healthy stream to interrupt.
                    log.warning(
                        f"[{device.device_id}] OWW: no mic frames for "
                        f"{dead_streak * 10}s and defensive mic_start isn't "
                        f"helping — escalating to mic_stop + mic_start"
                    )
                    await device.mic_stop()
                    await device.mic_start()
                continue

            dead_streak = 0

            # VAD sentinel (string; None accepted defensively — the pre-B5
            # encoding) — flush partial audio so OWW never scores across a
            # stream boundary.
            if payload is None or isinstance(payload, str):
                buf.clear()
                continue

            if device.oww_paused.is_set():
                continue

            if device.muted:
                buf.clear()
                continue

            buf.extend(payload)
            while len(buf) >= CHUNK_BYTES:
                frame   = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)

                if device.speaking:
                    continue

                # Per-room noise floor tracking (measurement only — the audio
                # is never modified). Asymmetric EWMA: follows drops quickly
                # (α=0.3) so it converges down fast, rises slowly (α=0.008 ≈
                # 10s time constant at 12.5 chunks/s) so speech bursts don't
                # drag it up. Feeds the SNR-relative no-speech detection in
                # em_esphome._stream_mic_audio and the diagnostics below.
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                if device.noise_floor == 0.0:
                    device.noise_floor = rms
                elif rms < device.noise_floor:
                    device.noise_floor += 0.3 * (rms - device.noise_floor)
                else:
                    device.noise_floor += 0.008 * (rms - device.noise_floor)

                prediction = await loop.run_in_executor(
                    None, model.predict, samples
                )
                score = prediction.get(model_key, 0.0)
                # Exactly once per chunk the MODEL saw, whatever the score —
                # the `device.speaking` skip above returns before predict(),
                # so the gate and the model stay in step.
                trusted = warmup.feed()

                # Log any score above noise floor so we can see near-misses
                # and understand whether failed wakes are "close but below
                # threshold" vs "not registering at all".
                #
                # Q4 fix (2026-07-05 review): this was DEBUG-only, invisible
                # in a normal INFO deployment — exactly the data needed for
                # threshold tuning was blind by default. Now: (1) the debug
                # line stays for verbose troubleshooting, (2) an INFO line
                # fires too, rate-limited to at most once per 2s per device
                # so a run of near-misses doesn't flood the log, and (3) a
                # persistent near_misses counter is exposed to the dashboard
                # via device_update so the count is visible without tailing
                # logs at all.
                # Below-threshold only (2026-07-15 fix): scores at or above
                # the wake threshold are detections, not near-misses — the
                # old `score > 0.05` gate counted every successful wake as a
                # near-miss too, inflating both the dashboard counter and
                # the wake_counters rollup (near_miss_max = the wake score).
                # During music playback the mic hears the speaker ~25dB
                # louder than the person, so wake scores are depressed —
                # the same physics barge-in handles during TTS. Score
                # against the (lower) barge threshold while a media
                # session plays, but only when barge-in is enabled: that's
                # the user's opt-in to trusting AEC not to self-trigger.
                eff_threshold = device.oww_threshold
                if device.barge_in_enabled and em_player.is_playing(device.device_id):
                    eff_threshold = min(eff_threshold, device.barge_threshold)

                if trusted and 0.05 < score < eff_threshold:
                    device.oww_near_misses += 1
                    nm_pending += 1
                    nm_max = max(nm_max, float(score))
                    log.debug(
                        f"[{device.device_id}] OWW score: {score:.3f} "
                        f"(threshold={device.oww_threshold:.3f}, "
                        f"rms={rms:.4f}, floor={device.noise_floor:.4f})"
                    )
                    now = asyncio.get_event_loop().time()
                    if now - last_near_miss_log_ts >= 2.0:
                        last_near_miss_log_ts = now
                        log.info(
                            f"[{device.device_id}] OWW near-miss: score={score:.3f} "
                            f"(threshold={device.oww_threshold:.3f}, "
                            f"total near-misses={device.oww_near_misses})"
                        )
                        await api._push_event({
                            "type":      "device_update",
                            "device_id": device.device_id,
                            "state":     {"owwNearMisses": device.oww_near_misses},
                        })
                        # Flush the buffered near-miss counts into the hourly
                        # persistent rollup. Riding this rate-limited branch
                        # caps the DB cost at one upsert per 2s per device,
                        # however noisy the room.
                        _nm, _mx = nm_pending, nm_max
                        nm_pending, nm_max = 0, 0.0
                        await loop.run_in_executor(
                            None,
                            lambda: db.bump_wake_counters(
                                device.device_id,
                                near_misses=_nm, near_miss_max=_mx,
                            ),
                        )

                # Who gets to start a turn. In "on" mode the device decides and
                # this controller's own crossing is demoted to a measurement —
                # see em_shadow.decide_wake_source for why it keeps scoring at
                # all. In every other mode this is exactly the old condition.
                if not trusted and score >= eff_threshold:
                    log.info(
                        f"[{device.device_id}] Wake score {score:.3f} >= "
                        f"{eff_threshold:.3f} ignored — openwakeword warm-up, "
                        f"{warmup.progress()} chunks since reset"
                    )
                ctrl_hit = trusted and score >= eff_threshold
                dev_wake, dev_age = device.pending_wake.take()
                if dev_wake is None and dev_age is not None:
                    # Expired before anything could act on it. Worth a warning
                    # rather than a debug line: it is a wake the user spoke and
                    # did not get, and the age is the only evidence of why.
                    log.warning(
                        f"[{device.device_id}] on-device wake dropped — "
                        f"{dev_age:.1f}s old (limit "
                        f"{em_shadow.MAX_PENDING_WAKE_S:.1f}s)"
                    )
                source = em_shadow.decide_wake_source(
                    device.oww_on_device, dev_wake, ctrl_hit
                )
                if ctrl_hit and device.oww_on_device == em_shadow.MODE_ON:
                    # "on" mode, and this controller heard it too. Recorded for
                    # the comparison and nothing else — the device is driving.
                    #
                    # Recorded whoever WON, which is the whole point. Gating
                    # this on source == "none" lost the crossing whenever the
                    # device's wake and ours landed on the same iteration — and
                    # the turn then drains mic_queue, so there is no second
                    # chance — writing a NULL that reads as "the controller
                    # missed it" when the controller had in fact scored it
                    # identically. Two of the first three trial turns agreed to
                    # four decimal places and the third recorded a miss for
                    # exactly this reason (2026-08-10). A comparison that is
                    # wrong only in the direction that flatters the feature is
                    # the worst kind.
                    device.ctrl_shadow.record_cross(score, 0)
                    device.ctrl_shadow.active = True

                if source != "none":
                    if source == "device":
                        score      = dev_wake["score"]
                        # The bar the DEVICE cleared, which during playback is
                        # the lower barge-in one. Falls back to ours when the
                        # device did not report one, rather than recording a
                        # threshold the wake was never judged against.
                        if dev_wake["threshold"] is not None:
                            eff_threshold = dev_wake["threshold"]
                    log.info(
                        f"[{device.device_id}] Wake word detected "
                        f"(source={source}, score={score:.3f}, "
                        f"threshold={eff_threshold:.3f}, "
                        f"rms={rms:.4f}, floor={device.noise_floor:.4f})"
                    )
                    db.log_device(
                        device.device_id, "info", "device",
                        f"Wake word detected (score={score:.3f}, {source})"
                    )
                    if not device.voice_lock.locked():
                        # P0-1: do NOT send mic_stop/mic_start_turn.
                        # The stream stays running continuously. Flipping
                        # oww_paused routes subsequent frames to voice_queue.
                        # The VAD gate is already open mid-utterance — that's
                        # how OWW got the wake-word audio — so command audio
                        # flows in with zero re-trigger delay and zero RTT gap.
                        # Wake-word tail bleed ("…Jarvis") is handled by the
                        # preroll discard in _stream_mic_audio.
                        # TTS mic_stop/mic_start remains untouched — that
                        # acoustic-feedback guard is load-bearing.
                        model.reset()
                        warmup.reset()
                        buf.clear()
                        device.cancel_event.clear()
                        # Wake detail for the turn's persistent record —
                        # popped by esphome.trigger_voice_turn.
                        # float(): OWW scores are numpy float32 — sqlite3
                        # stores those as a 4-byte BLOB, which then breaks
                        # JSON serialisation of the row (2026-07-14).
                        # Monotonic instant of THIS detection, for correlating
                        # the device's shadow crossing at turn-persist time.
                        # em_shadow.now() rather than a clock of our own: both
                        # sides of that subtraction must come from one place,
                        # and this module does not import time (it uses the
                        # event loop's clock) — reaching for time.monotonic()
                        # here raised NameError on the first wake detection and
                        # killed the listener for the rest of the process.
                        #
                        # For a device-triggered wake this is the CROSSING
                        # instant, not now: the device reported how long ago it
                        # fired, and using arrival time instead would fold the
                        # network hop into every comparison and every
                        # arbitration decision — the one thing this fleet's
                        # 1.1-2.6s RTT excursions make certain to matter.
                        device.last_wake_mono = (
                            dev_wake["at"] if source == "device" else em_shadow.now()
                        )
                        device.last_wake = {
                            "model":       model_key,
                            "score":       round(float(score), 4),
                            # The EFFECTIVE threshold this wake actually cleared,
                            # not the nominal one. During playback with barge-in
                            # enabled the bar drops to bargeInThreshold, and
                            # recording 0.5 there produced rows that contradicted
                            # themselves — wake_score 0.055 against
                            # wake_threshold 0.5, i.e. "woke below its own bar"
                            # (present in the data since at least 2026-07-25).
                            # It is also what lets the on-device comparison tell
                            # a real miss from a wake the device was never asked
                            # to look for.
                            "threshold":   round(float(eff_threshold), 4),
                            "noise_floor": round(device.noise_floor, 5),
                        }
                        device.oww_paused.set()
                        log.debug(
                            f"[{device.device_id}] OWW: oww_paused set, "
                            f"routing to voice_queue (no mic_stop/mic_start_turn)"
                        )
                        # Lock the beamformer onto the speaker's perimeter mic
                        # NOW, mid-utterance — the onset detector has the
                        # freshest possible signal at this moment. No stream
                        # restart; released by beam_unlock post-turn (and
                        # implicitly by any TTS mic stop/start cycle).
                        await device.beam_lock()

                        # Multi-device arbitration: if this utterance also
                        # woke another Echo, only the best-placed one should
                        # answer. Capture routing (oww_paused, beam lock) is
                        # already set up above ON PURPOSE — the winner's
                        # command audio must be flowing from the first
                        # syllable, so we arm optimistically and revert on
                        # loss. Solo fleets skip the window entirely.
                        won_by = device.device_id
                        if device.wake_arb_ms > 0 and len(_devices) > 1:
                            # Synchronous — the winner starts its turn on
                            # this same tick. The old version awaited the
                            # full window on EVERY wake (~364ms measured)
                            # even when no other device was contending.
                            won_by = _wake_arbiter.claim(
                                device.device_id,
                                device.wake_arb_ms / 1000.0,
                            )
                        if won_by != device.device_id:
                            device.oww_paused.clear()
                            device.last_wake = None
                            await device.beam_unlock()
                            ceded = 0
                            while not device.voice_queue.empty():
                                try:
                                    device.voice_queue.get_nowait()
                                    ceded += 1
                                except asyncio.QueueEmpty:
                                    break
                            log.info(
                                f"[{device.device_id}] Wake ceded to "
                                f"{won_by} (arbitration; score={score:.3f}, "
                                f"discarded {ceded} frames)"
                            )
                            db.log_device(
                                device.device_id, "info", "controller",
                                f"Wake ceded to {won_by} (arbitration)"
                            )
                            continue

                        # "wakeword-dev" rather than a separate field: every
                        # reader of trigger already matches on the "wakeword"
                        # prefix (including _persist_turn's shadow block), so
                        # this distinguishes the two sources in the Activity
                        # tab and in queries without any of them changing.
                        label = "wakeword-dev" if source == "device" else "wakeword"
                        await _run_voice_locked(device, trigger_label=f"{label}({score:.3f})", is_wakeword=True)
                        # Back to ch6 omni for wake listening. Belt-and-braces
                        # for turns that never restarted the stream (no-TTS
                        # outcomes: error, no-speech, cancel) — a lock left
                        # in place would point wake listening at one
                        # perimeter mic instead of omni.
                        await device.beam_unlock()

                        drained = 0
                        while not device.voice_queue.empty():
                            try:
                                device.voice_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        if drained:
                            log.info(
                                f"[{device.device_id}] OWW: "
                                f"drained {drained} stale frames post-turn"
                            )
                        model.reset()
                        warmup.reset()
                        buf.clear()
                        # mic_start without lock_mic — device stays on ch6 omni
                        # (beamforming=off), same stream as OWW listening.
                        # This is a defensive restart only: if the stream
                        # somehow died during the turn, this revives it.
                        # If already running, the device no-ops it.
                        log.info(f"[{device.device_id}] OWW: defensive mic_start (no lock_mic)")
                        await device.mic_start()
                    else:
                        log.info(
                            f"[{device.device_id}] Voice turn active — "
                            f"ignoring wake"
                        )
                        model.reset()
                        warmup.reset()

    except asyncio.CancelledError:
        await device.mic_stop()
        raise


# ─── Button handler ───────────────────────────────────────────────────────────

async def handle_button_event(device: Device, event: dict):
    click_type = event.get("clickType")
    down       = event.get("down", True)

    if down:
        return

    if click_type == 138:   # DotClick
        # A HOLD is a separate gesture, forwarded to HA rather than starting a
        # turn. heldMs is measured on the device: timing the down/up messages
        # here would be at the mercy of RTT excursions measured past 1600ms on
        # this fleet, which would misread taps as holds.
        #
        # Absent heldMs (firmware predating this) reads as a tap, so the action
        # button keeps working exactly as before on devices that have not
        # updated — degrade to old behaviour, never to a wrong answer.
        held_ms = event.get("heldMs") or 0
        # Mute is read from the EVENT where the firmware reports it, falling
        # back to the last mute_state message. The event is authoritative: it
        # carries the state at the instant of the press, where device.muted is
        # whatever the last message left behind.
        muted = bool(event.get("muted", device.muted))
        action = em_button.decide(
            held_ms=held_ms,
            hold_ms=esphome.BUTTON_HOLD_MS,
            muted=muted,
            turn_active=device.voice_lock.locked(),
            # ANDed with the capability — see em_button.decide.
            tap_event=(
                device.button_single_tap_event and device.button_hold_capable
            ),
        )

        if action == em_button.HOLD:
            log.info(f"[{device.device_id}] Dot button held {held_ms}ms → HA event")
            esphome.send_button_event(device.device_id, "long")
            return

        if action == em_button.TAP_EVENT:
            window_ms = device.button_multi_tap_ms
            if window_ms <= 0:
                log.info(f"[{device.device_id}] Dot button tap → HA event (single)")
                esphome.send_button_event(device.device_id, "single")
                return

            device.tap_burst.tap(window_ms)
            log.info(
                f"[{device.device_id}] Dot button tap "
                f"{device.tap_burst.count} in burst (window {window_ms}ms)"
            )
            return

        if action == em_button.BLOCKED:
            # Only the TURN is blocked. The hold and tap-event above have
            # already been forwarded, muted or not.
            log.info(f"[{device.device_id}] Dot button tap ignored — mic is muted")
            return

        if action == em_button.CANCEL:
            log.info(f"[{device.device_id}] Dot button — cancelling voice turn")
            device.cancel_event.set()
            esphome.cancel_voice_turn(device.device_id)
            # Flush the device's speaker too, or cancelling DURING the spoken
            # response only stops the controller feeding it: the ring clears
            # while up to ~5.5s already in audioChanDepth plays out, and the
            # device carries on talking after you have visibly cancelled it.
            #
            # cancel_event alone cannot fix that — it aborts our end, not the
            # audio already on the device. Mute and barge-in both send this
            # for exactly the same reason; the button was the one deliberate
            # cancel that did not.
            await device.send_control({"type": "speaker_flush"})
        else:
            log.info(f"[{device.device_id}] Dot button → voice turn")
            device.cancel_event.clear()
            device.oww_paused.set()
            async def _button_voice_turn():
                # Button is a deliberate act with no dead zone cost — nothing
                # is being said at the moment of press, so stop/start RTT is
                # fine. Stop the running ch6 stream, restart with lock_mic:true
                # so streamMic calls beam.Lock(beamformingEnabled) and the
                # beamformer selects the best perimeter mic for this turn.
                # mic_start_turn() no-ops if already running, so stop first.
                await device.mic_stop()
                await device.mic_start_turn()
                await _run_voice_locked(device, trigger_label="button", is_wakeword=False)
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                # Post-turn: back to ch6 omni for OWW listening. mic_stop
                # first: if the turn had no TTS (cancel/error/no-speech), the
                # lock_mic stream from mic_start_turn is still running and a
                # bare mic_start would no-op against it — leaving the GATED,
                # beam-locked turn stream as the permanent wake stream. Safe
                # now that streamMic's exit has the ownership check (the
                # stop/start pair can no longer leak a second stream).
                await device.mic_stop()
                await device.mic_start()
            # M1 fix (2026-07-05 review): keep a reference and log exceptions
            # instead of a bare fire-and-forget create_task() — previously
            # any exception raised in this task vanished silently with no
            # log line, standard asyncio fire-and-forget hygiene issue.
            _btn_task = asyncio.create_task(_button_voice_turn())
            _btn_task.add_done_callback(_log_task_exception)


# ─── Control plane handler ────────────────────────────────────────────────────

async def _link_auth_ok(
    ws: WebSocketServerProtocol, device_id: str, secure: bool, plane: str
) -> bool:
    """
    Device-link auth gate, applied to all three WS planes once the
    device_id is known.

    Rules (rollout-safe by construction):
      - a presented token that MISMATCHES a stored one always rejects;
      - a stored token with NO token presented is allowed unless
        REQUIRE_DEVICE_TLS — the DB row is minted before the files land
        on the device, and rejecting in that window would cut off the
        shell plane that the credential push itself rides on;
      - a presented token for a device with NOTHING on record is ignored,
        not rejected (see below);
      - REQUIRE_DEVICE_TLS=1 requires TLS + a matching token, full stop.

    That third rule used to be a rejection, and it made deleting a device a
    one-way door. Delete removes the row, the token is a column on it, and the
    device re-reads its credential file on every dial — so it presented a token
    nothing recognised and was refused on all three planes, INCLUDING the shell
    plane the controller would otherwise push fresh credentials over. The
    device retried forever behind a pulsing orange ring, and the dashboard had
    nothing to show because as far as it was concerned the device did not
    exist.

    Rejecting there also never bought anything. The rule immediately above
    admits a connection presenting no token at all, so an unrecognised token
    was being treated as worse than none while anyone could simply omit the
    header. A device with a stale credential now comes back as pending and
    waits for approval, which is the decision a human should be making anyway.

    `REQUIRE_DEVICE_TLS=1` installs are unaffected: the last rule still
    demands a token that matches a stored one, so a deleted device is refused
    there regardless and re-provisioning over USB stays the intended path.
    """
    presented = None
    try:
        presented = ws.request.headers.get("X-EM-Token")
    except AttributeError:
        pass

    loop = asyncio.get_event_loop()
    expected = await loop.run_in_executor(None, db.get_device_token, device_id)

    verdict = em_linkauth.decide(
        presented=presented,
        expected=expected,
        secure=secure,
        require_tls=REQUIRE_DEVICE_TLS,
    )
    if not verdict.ok:
        log.warning(f"[{plane}] {device_id}: {verdict.reason} — rejecting")
        return False
    if verdict.stale_token:
        # Allowed, but worth seeing in the log: almost always a device that was
        # deleted and has come back carrying the credential from its previous
        # life, which is the answer to "why is this in pending again".
        log.warning(
            f"[{plane}] {device_id}: {verdict.reason}. "
            f"Treating as an unregistered device; it will need approval."
        )
    return True


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

        if not await _link_auth_ok(ws, device_id, secure, "control"):
            await ws.close()
            return
        ip           = msg.get("ip", str(remote[0]))
        version      = msg.get("version")
        capabilities = msg.get("capabilities", [])

        loop         = asyncio.get_event_loop()
        approval_mode = db.get_config("device_approval", DEVICE_APPROVAL)
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
                db.log_device(
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

        device = Device(device_id, ip, capabilities, ws)
        # Why the device has no ambient light sensor, when it has none. The
        # capability list says only that it is absent; without the reason,
        # an unfitted chip and an unbound driver are indistinguishable
        # remotely, and #90 needed a shell session on the user's own hardware
        # to tell them apart. Absent on firmware that does not send it, which
        # reads as "not reported" rather than as a fault.
        device.ambient_light_status = msg.get("ambient_light_status")
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
        _devices[device_id] = device

        log.info(
            f"[control] Device connected: {device_id} v={version} "
            f"at {ip} caps={capabilities}"
        )
        db.log_device(
            device_id, "info", "controller",
            f"Connected from {ip} version={version}"
        )

        await device.send_control({"type": "ack", "device_id": device_id})

        config = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        await device.send_control({"type": "config", **config})
        device.oww_threshold = float(config.get("owwThreshold", OWW_THRESHOLD))
        device.oww_model     = config.get("owwModel", f"{OWW_MODEL}_v0.1")
        device.wake_arb_ms   = int(config.get("wakeArbitrationMs", 300))
        device.oww_speex_ns  = bool(config.get("owwSpeexNs", False))
        device.ns_asr        = bool(config.get("nsAsr", False))
        device.save_utterances = bool(config.get("saveUtterances", False))
        device.barge_in_enabled = bool(config.get("bargeInEnabled", False))
        device.barge_threshold  = float(config.get("bargeInThreshold", 0.6))
        device.button_single_tap_event = bool(
            config.get("buttonSingleTapEvent", False)
        )
        device.button_multi_tap_ms = int(config.get("buttonMultiTapMs", 0))
        # Resolved against the capability — see em_shadow.effective_mode for
        # why "on" against firmware that cannot trigger must become shadow
        # rather than being honoured.
        device.oww_on_device = em_shadow.effective_mode(
            config.get("owwOnDevice"), device.oww_trigger_capable,
            device.oww_model_ready,
        )
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
        # Initialise volume from stored config — device will report its real
        # value via volume_state on connect, but this seeds a sane default
        # in the window before that first message arrives.
        device.volume = _device_level_to_ha(
            int(config.get("startupVolume", 85))
        )
        log.info(f"[control] Config pushed to {device_id} (volume={device.volume:.3f})")

        await leds_off(device)
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
            try:
                await _run_post_turn_playback(_d, pcm_bytes)
            finally:
                await _d.mic_start()
                await em_player.resume_interrupted(_d.device_id)
            # Whether the audio actually reached the speaker. Something that
            # cancelled mid-playback (a mute, a button) means the user did
            # not hear it, and telling HA it finished successfully would be
            # untrue — it is the one thing the announcement reply reports.
            return not _d.cancel_event.is_set()
        async def _send_volume_set(level: int, _d=_device_ref) -> None:
            await _d.send_control({"type": "volume_set", "level": level})
        # Capabilities before the servers come up: they decide which HA
        # entities are advertised, and advertising is a one-shot at
        # ListEntities time.
        esphome.set_device_capabilities(device_id, capabilities)
        await esphome.device_connected(
            device_id,
            SERVER_HOST,
            standalone_play=_standalone_play,
            send_volume_set=_send_volume_set,
        )
        # The ESPHome server object caches the OWW model from server
        # creation — refresh it from the config we just loaded so HA's
        # wake-word dropdown tracks dashboard changes across controller
        # restarts too.
        esphome.update_oww_model(device_id, device.oww_model)
        # BT proxy: mark the device online (brings its proxy listener up if
        # enabled) and reconcile against current config — covers devices
        # approved or toggled while they were offline.
        await em_ble_proxy.device_connected(device_id)
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
        oww_task  = asyncio.create_task(wake_word_listener(device))

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                msg_type = msg.get("type")

                if msg_type == "button":
                    await handle_button_event(device, msg)

                elif msg_type == "ambient_light":
                    # A step change in room light, sent by the device the
                    # moment it happens rather than waiting up to 30s for the
                    # stats tick — the timing IS the signal ("someone turned
                    # a light on"). The steady-state value still rides stats.
                    _lux = msg.get("lux")
                    if isinstance(_lux, int):
                        device.stats["ambientLux"] = _lux
                        esphome.update_ambient_lux(device_id, _lux)
                        log.info(f"[{device_id}] Ambient light → {_lux} lux")

                elif msg_type == "mute_state":
                    device.muted = msg.get("muted", False)
                    if device.muted and device.voice_lock.locked():
                        # Mute during an active turn terminates it — same
                        # cancel as the dot button, plus speaker_flush so
                        # any in-flight TTS goes silent immediately (the
                        # device shows the red ring the moment the button
                        # is pressed; audio carrying on would contradict
                        # it). The device guards its LED ring while muted,
                        # so the cancelled turn's LED cleanup can't clear
                        # the red ring.
                        log.info(
                            f"[{device_id}] Muted during active turn — "
                            f"cancelling"
                        )
                        device.cancel_event.set()
                        esphome.cancel_voice_turn(device_id)
                        await device.send_control({"type": "speaker_flush"})
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {"muted": device.muted},
                    })

                elif msg_type == "volume_state":
                    # Device reports its current volume level (raw tinymix index).
                    # Convert to HA float, update in-memory state, persist to
                    # config so the value survives controller and device restarts.
                    raw_level = int(msg.get("level", 85))
                    device.volume = _device_level_to_ha(raw_level)
                    log.debug(
                        f"[{device_id}] volume_state: level={raw_level} "
                        f"→ {device.volume:.3f}"
                    )
                    # Persist — read-modify-write to avoid stomping other fields
                    stored_config = await loop.run_in_executor(
                        None, db.get_device_config, device_id
                    )
                    stored_config["startupVolume"] = raw_level
                    await loop.run_in_executor(
                        None, db.set_device_config, device_id, stored_config
                    )
                    # Notify ESPHome satellite so HA's media player entity updates
                    esphome.update_device_volume(device_id, device.volume)

                elif msg_type == "stats":
                    device.stats = {
                        "cpuPct":        msg.get("cpuPct"),
                        "memUsedMb":     msg.get("memUsedMb"),
                        "memTotalMb":    msg.get("memTotalMb"),
                        "storageUsedMb": msg.get("storageUsedMb"),
                        "storageTotalMb":msg.get("storageTotalMb"),
                        "wifiRssi":      msg.get("wifiRssi"),
                        "wifiSsid":      msg.get("wifiSsid"),
                        # v7 link telemetry (firmware >= v2.9.6). This dict
                        # is an explicit allowlist, so any new device stat
                        # must be added HERE as well as in DeviceStats and
                        # record_device_stats — all three, or the field is
                        # silently dropped in the relay (2026-07-20).
                        "linkSpeedMbps": msg.get("linkSpeedMbps"),
                        "wifiFreqMhz":   msg.get("wifiFreqMhz"),
                        "wifiBssid":     msg.get("wifiBssid"),
                        "txBytes":       msg.get("txBytes"),
                        "rxBytes":       msg.get("rxBytes"),
                        "txErrors":      msg.get("txErrors"),
                        "txDropped":     msg.get("txDropped"),
                        "rxCrcErrors":   msg.get("rxCrcErrors"),
                        "ble":           msg.get("ble"),
                        # Thermals + CPU topology. coresOnline is not optional
                        # context: cpuPct is a share of ONLINE capacity, so the
                        # same work halves its percentage when hotplug adds a
                        # core. thermalCoreLimit < coresTotal means the thermal
                        # governor is capping capacity.
                        "cpuTempC":         msg.get("cpuTempC"),
                        "maxTempC":         msg.get("maxTempC"),
                        "coresOnline":      msg.get("coresOnline"),
                        "coresTotal":       msg.get("coresTotal"),
                        "thermalCoreLimit": msg.get("thermalCoreLimit"),
                        # Ambient light (TSL2540). None on a device without
                        # the sensor — 0 lux is a real reading (covered), so
                        # the two must not collapse into each other.
                        "ambientLux":       msg.get("ambientLux"),
                        # v13 on-device shadow window summary, absent when the
                        # device is not scoring (see the allowlist note above:
                        # DeviceStats, here, and the consumer below).
                        "owwShadow":     msg.get("owwShadow"),
                    }
                    # Shadow summary → hourly rollup. Present only while the
                    # device is scoring, so its presence is also the "was it
                    # looking" flag that stops a missing per-turn score from
                    # reading as a miss.
                    _sh = msg.get("owwShadow") or {}
                    device.shadow.active = bool(_sh)
                    if _sh and _sh.get("threshold"):
                        try:
                            device.shadow.threshold = float(_sh["threshold"])
                        except (TypeError, ValueError):
                            pass
                    if _sh:
                        if _sh.get("drops") or _sh.get("errors"):
                            log.warning(
                                f"[{device_id}] on-device wake word fell behind: "
                                f"{_sh.get('drops')} frames dropped, "
                                f"{_sh.get('errors')} errors ({_sh.get('lastErr') or '-'}) "
                                f"— comparison is running on a subset of the audio"
                            )
                    if msg.get("ble"):
                        em_ble_proxy.update_stats(device_id, msg["ble"])
                    # Ambient light straight through to HA's sensor entity.
                    # Rides the existing ~30s stats tick — light does not
                    # change fast enough to justify a channel of its own, and
                    # nothing per-frame is the standing rule here.
                    if "ambientLux" in msg:
                        esphome.update_ambient_lux(device_id, msg.get("ambientLux"))
                    # Fold into the persistent hourly rollup (CPU/RAM/storage/
                    # RSSI trends) — one cheap upsert per ~30s report. The
                    # last_seen refresh rides the same executor hop: a stats
                    # report IS proof of life, and without it last_seen only
                    # ever recorded the last *connect*.
                    # RTT is controller-measured, not relayed from the
                    # device message, so it is merged in here rather than
                    # coming through the allowlist above. drain_rtt() takes
                    # and resets the window accumulated since the last
                    # report, so no sample is counted twice.
                    _metrics = {**device.stats, **device.drain_rtt()}
                    def _persist_stats(_id=device_id, _s=_metrics, _shadow=_sh):
                        db.record_device_stats(_id, _s)
                        db.touch_device_seen(_id)
                        # Shadow counters ride the SAME executor hop rather
                        # than adding one: the whole point of summarising on
                        # the device is that on-device scoring costs the DB
                        # one upsert per 30s, not one per frame.
                        if _shadow:
                            db.bump_wake_counters(
                                _id,
                                dev_frames=int(_shadow.get("frames") or 0),
                                dev_drops=int(_shadow.get("drops") or 0),
                                dev_crossings=int(_shadow.get("crossings") or 0),
                                dev_max_score=float(_shadow.get("maxScore") or 0.0),
                                # v16: what a drop actually was — slowest
                                # inference vs longest frame gap.
                                dev_max_infer_ms=int(_shadow.get("maxInferMs") or 0),
                                dev_max_gap_ms=int(_shadow.get("maxGapMs") or 0),
                            )
                    await loop.run_in_executor(None, _persist_stats)
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {
                            "stats": device.stats,
                            # Controller-side proxy view rides along so the
                            # dashboard's Bluetooth panel stays live without
                            # a full device refresh.
                            "bleProxy": em_ble_proxy.get_status(device_id),
                        },
                    })

                elif msg_type == "wifi_result":
                    # Outcome of a wifi_change. The device re-sends this
                    # until it sees a wifi_commit ack (a single send can
                    # vanish into a half-open TCP connection killed by the
                    # network switch), so: ALWAYS ack — on success the ack
                    # also finalises the change (deletes rollback backup +
                    # pending marker; a failed change already removed both,
                    # so the ack is a no-op there) — and log/record only
                    # the first arrival.
                    ok    = bool(msg.get("ok"))
                    ssid  = msg.get("ssid", "")
                    error = msg.get("error") or ""
                    st, duplicate = api.wifi_record_result(device_id, ok, ssid, error)
                    await device.send_control({"type": "wifi_commit"})
                    if not duplicate:
                        if ok:
                            log.info(f"[{device_id}] WiFi changed to \"{ssid}\" — committed")
                            db.log_device(device_id, "info", "device",
                                          f'WiFi changed to "{ssid}"')
                        else:
                            log.warning(f"[{device_id}] WiFi change to \"{ssid}\" "
                                        f"failed: {error}")
                            db.log_device(device_id, "warning", "device",
                                          f'WiFi change to "{ssid}" failed: {error}')
                        await api._push_event({
                            "type":      "device_update",
                            "device_id": device_id,
                            "state":     {"wifi": st},
                        })

                elif msg_type == "playback_stats":
                    # One report per completed speaker stream (firmware
                    # >= v2.9): periods played + mid-stream underruns.
                    # Attach to the turn persisted just before playback;
                    # consume last_turn_id so a later announcement's report
                    # can't overwrite a turn's stats. Reports with no
                    # pending turn (HA announcements, TTS after a controller
                    # restart) roll into the hourly counters instead.
                    # periods/underruns are read from the top level, which
                    # every firmware sends; "stats" carries the v2.9.6+
                    # delivery-margin fields and is absent on older devices.
                    periods   = int(msg.get("periods", 0))
                    underruns = int(msg.get("underruns", 0))
                    pstats    = msg.get("stats") or {}
                    # Release _run_post_turn_playback: this report IS the
                    # end of audio, and the ring clears on it rather than
                    # on a wall-clock guess.
                    device.playback_done.set()
                    # Delivery window: first speaker frame sent -> this
                    # report. The metric the 07-20 investigation lacked —
                    # "Streaming took Xs" times the socket write and reads
                    # ~0s however slowly the device is really being fed.
                    delivery_ms = -1
                    if device.playback_send_t0 is not None:
                        delivery_ms = int(
                            (loop.time() - device.playback_send_t0) * 1000
                        )
                        device.playback_send_t0 = None
                    turn_id   = device.last_turn_id
                    device.last_turn_id = None
                    if turn_id is not None:
                        await loop.run_in_executor(
                            None, db.set_turn_playback,
                            turn_id, periods, underruns, pstats,
                        )
                        if delivery_ms >= 0:
                            await loop.run_in_executor(
                                None, db.set_turn_delivery, turn_id,
                                device.playback_send_ms,
                                delivery_ms, device.playback_eq_ms,
                            )
                        for rec in reversed(device.turn_history):
                            if rec.get("turn_id") == turn_id:
                                rec["playback_periods"] = periods
                                rec["underruns"]        = underruns
                                break
                    else:
                        # The turn row may not exist yet (device buffers
                        # usually drain before the controller's drain sleep
                        # ends) — stash for _persist_turn to fold in. A
                        # displaced earlier stash was an announcement's:
                        # keep its underruns in the hourly counters.
                        prev = device.pending_playback_stats
                        # Indices 0-2 stay (ts, periods, underruns) so the
                        # existing consumer keeps working; 3-4 carry the v7
                        # delivery detail.
                        device.pending_playback_stats = (
                            asyncio.get_event_loop().time(), periods, underruns,
                            pstats, delivery_ms,
                        )
                        if prev and prev[2]:
                            await loop.run_in_executor(
                                None,
                                lambda: db.bump_wake_counters(
                                    device_id, underruns=prev[2]
                                ),
                            )
                    if underruns:
                        log.warning(
                            f"[{device_id}] Playback underruns: {underruns} "
                            f"in {periods} periods"
                            f"{f' (turn {turn_id})' if turn_id else ''}"
                        )

                elif msg_type == "oww_shadow_cross":
                    # On-device scoring reached the wake threshold. Recorded
                    # for comparison ONLY — nothing here starts a turn, and
                    # that is the entire point of shadow mode.
                    device.shadow.record_cross(msg.get("score"), msg.get("ageMs"))
                    log.info(
                        f"[{device_id}] on-device wake crossing: "
                        f"score={msg.get('score')} age={msg.get('ageMs')}ms "
                        f"(shadow — not triggering)"
                    )

                elif msg_type == "oww_wake":
                    # On-device scoring crossed the bar AND owwOnDevice is
                    # "on", so the device is asking for a turn rather than
                    # reporting a measurement. Parked here; the wake listener
                    # picks it up on its next frame (~80ms) because that is
                    # where the turn setup lives — capture routing, beam lock
                    # and arbitration all have to happen together, and doing
                    # them from the control plane would be a second copy of
                    # the most delicate sequence in the controller.
                    #
                    # Also recorded as a crossing, so a device-triggered turn
                    # carries the same dev_* comparison fields as a
                    # controller-triggered one and the Activity tab does not
                    # have to special-case which side fired.
                    device.shadow.record_cross(msg.get("score"), msg.get("ageMs"))
                    if device.oww_on_device != em_shadow.MODE_ON:
                        # Firmware triggering while the controller thinks it
                        # should not: a config push in flight, or a rollback
                        # to a mode this device no longer has. Logged rather
                        # than obeyed — the controller's view of the mode is
                        # the one the dashboard shows.
                        log.warning(
                            f"[{device_id}] oww_wake ignored — mode is "
                            f"{device.oww_on_device!r}, not 'on'"
                        )
                    elif device.pending_wake.offer(
                        msg.get("score"), msg.get("threshold"), msg.get("ageMs")
                    ):
                        log.info(
                            f"[{device_id}] on-device wake: "
                            f"score={msg.get('score')} age={msg.get('ageMs')}ms "
                            f"(device triggered)"
                        )
                    else:
                        log.warning(
                            f"[{device_id}] malformed oww_wake dropped: {msg!r}"
                        )

                elif msg_type == "ble_adverts":
                    # BLE proxy data path — batched adverts from the
                    # device's passive scanner, forwarded to HA.
                    em_ble_proxy.forward_adverts(
                        device_id, msg.get("adverts") or []
                    )

                elif msg_type == "wifi_scan_result":
                    fut = device.wifi_scan_future
                    if fut is not None and not fut.done():
                        fut.set_result(msg)

                elif msg_type == "log":
                    level   = msg.get("level", "info")
                    message = msg.get("message", "")
                    # _push_log_event PERSISTS as well as pushing, so this must
                    # not also call db.log_device — doing both wrote every
                    # device log line twice, ~6ms apart, which is how half the
                    # device_logs table came to be duplicates. It also matters
                    # for the support bundle: thin_noise keeps the newest three
                    # [mem] lines per device, so duplication halved the readings
                    # a leak hunt actually gets.
                    #
                    # The removed call was a synchronous DB write on the event
                    # loop; _push_log_event does it in an executor.
                    await api._push_log_event(device_id, level, "device", message)

                elif msg_type == "pong":
                    # Solicited pong (carries our sequence id) -> an RTT
                    # sample. Unsolicited keepalive pongs have no id and are
                    # ignored here; pairing one with whatever ping happened
                    # to be outstanding would invent a measurement.
                    _seq = msg.get("id")
                    if _seq is not None:
                        _sent = device.ping_sent.pop(_seq, None)
                        _busy = device.ping_busy.pop(_seq, False)
                        if _sent is not None:
                            _rtt = int((loop.time() - _sent) * 1000)
                            device.record_rtt(_rtt, _busy)
                            if _rtt >= RTT_EXCURSION_MS:
                                log.info(
                                    f"[{device_id}] RTT excursion: {_rtt}ms "
                                    f"({'busy' if _busy else 'idle'})"
                                )
                    pass

                else:
                    log.debug(
                        f"[{device_id}] Unknown control message: {msg_type}"
                    )

        finally:
            ping_task.cancel()
            oww_task.cancel()

    except asyncio.TimeoutError:
        log.warning(f"[control] Registration timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[control] Handler error: {e}")

    finally:
        if device:
            # Above the stale check on purpose: per-connection state, and
            # send_button_event resolves by device_id, so an orphaned timer
            # would fire a phantom tap at the replacement connection.
            device.tap_burst.cancel()
            if _devices.get(device.device_id) is not device:
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
                db.log_device(
                    device.device_id, "info", "controller", "Disconnected"
                )
                # Stamp the moment it went away, so "last seen" is exact for
                # an offline device rather than up to one stats report stale.
                db.touch_device_seen(device.device_id)
                _devices.pop(device.device_id, None)
                await api.notify_device_disconnected(device.device_id)
                await esphome.device_disconnected(device.device_id)
                await em_ble_proxy.device_disconnected(device.device_id)
                em_player.device_gone(device.device_id)


# ─── Data plane handler ───────────────────────────────────────────────────────

async def handle_data(ws: WebSocketServerProtocol, secure: bool = False):
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "identify":
            log.warning(
                f"[data] First message from {remote} was not identify — closing"
            )
            await ws.close()
            return

        device_id = msg["device_id"]

        if not await _link_auth_ok(ws, device_id, secure, "data"):
            await ws.close()
            return

        for _ in range(20):
            device = _devices.get(device_id)
            if device is not None:
                break
            await asyncio.sleep(0.1)

        if device is None:
            log.warning(f"[data] Unknown device_id: {device_id} — closing")
            await ws.close()
            return

        device.data_ws = ws
        device.data_ready.set()
        log.info(f"[data] Data connection established: {device_id}")

        async for raw in ws:
            if not isinstance(raw, bytes):
                continue
            if len(raw) <= MIC_HEADER_LEN:
                continue
            if raw[0] != MIC_FRAME_TYPE:
                continue
            if len(raw) == MIC_HEADER_LEN + 1 and raw[MIC_HEADER_LEN] in (VAD_END_TYPE, VAD_NO_SPEECH_TIMEOUT_TYPE):
                sentinel = (
                    esphome.VAD_SENTINEL_TIMEOUT
                    if raw[MIC_HEADER_LEN] == VAD_NO_SPEECH_TIMEOUT_TYPE
                    else esphome.VAD_SENTINEL_END
                )
                q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
                if q.full():
                    try:
                        q.get_nowait()
                        log.warning(f"[{device.device_id}] queue full — dropped one frame to deliver VAD sentinel")
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(sentinel)
                except asyncio.QueueFull:
                    log.error(f"[{device.device_id}] VAD sentinel lost — queue still full after drain")
                continue
            payload = raw[MIC_HEADER_LEN:]
            q = device.voice_queue if device.oww_paused.is_set() else device.mic_queue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the OLDEST frame, not the newest — keeps the tail of
                # the audio contiguous with real time, which is what OWW and
                # STT care about. Dropping the newest froze the queue at a
                # stale snapshot while fresh speech was discarded.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    except asyncio.TimeoutError:
        log.warning(f"[data] Identify timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[data] Handler error: {e}")

    finally:
        if device:
            if device.data_ws is ws:
                device.data_ws = None
                device.data_ready.clear()
            log.info(f"[data] Data connection closed: {device.device_id}")


# ─── Router ───────────────────────────────────────────────────────────────────

# ─── Shell plane handler ──────────────────────────────────────────────────────

async def handle_shell(ws: WebSocketServerProtocol, path: str, secure: bool = False):
    import aiohttp as _aiohttp

    # Path may carry a query: /shell/{device_id}?pty=1 signals that the
    # device actually established a PTY session (it may have been requested
    # but failed to allocate — the device falls back to a plain pipe and
    # omits the flag). The dashboard needs the established mode, not the
    # requested one, to pick its input framing.
    device_id, _, query = path.removeprefix("/shell/").partition("?")
    pty_mode = "pty=1" in query
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    if not await _link_auth_ok(ws, device_id, secure, "shell"):
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id} (pty={pty_mode})")

    done_future  = _shell_pending.get(device_id)
    dashboard_ws = _shell_dashboard.get(device_id)

    if done_future is None or done_future.done():
        log.warning(f"[shell] No pending shell request for {device_id} — closing")
        await ws.close()
        return

    if dashboard_ws is None:
        log.info(f"[shell] Programmatic session: {device_id}")
        done_future.set_result(ws)
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=300.0)
        except (asyncio.TimeoutError, Exception):
            pass
        log.info(f"[shell] Programmatic session ended: {device_id}")
        return

    log.info(f"[shell] Proxying: {device_id}")

    # Tell the dashboard which mode the device established before any
    # shell bytes flow: PTY sessions use framed input (0x00 stdin /
    # 0x01 resize) and emit terminal escape sequences; pipe sessions
    # (pre-PTY firmware) are raw both ways.
    try:
        await dashboard_ws.send_str(json.dumps({"type": "shell_meta", "pty": pty_mode}))
    except Exception:
        pass

    async def device_to_dashboard():
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await dashboard_ws.send_bytes(msg)
                else:
                    await dashboard_ws.send_str(msg)
        except Exception:
            pass

    async def dashboard_to_device():
        try:
            async for msg in dashboard_ws:
                if msg.type == _aiohttp.WSMsgType.BINARY:
                    await ws.send(msg.data)
                elif msg.type == _aiohttp.WSMsgType.TEXT:
                    await ws.send(msg.data.encode())
                elif msg.type in (_aiohttp.WSMsgType.CLOSE,
                                  _aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass

    tasks = [
        asyncio.create_task(device_to_dashboard()),
        asyncio.create_task(dashboard_to_device()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        log.info(f"[shell] Session ended: {device_id}")
        if not done_future.done():
            done_future.set_result(None)

async def _route(ws: WebSocketServerProtocol, secure: bool):
    path = ws.request.path if hasattr(ws, "request") else getattr(ws, "path", "/")

    if path == "/control":
        await handle_control(ws, secure)
    elif path == "/data":
        await handle_data(ws, secure)
    elif path.startswith("/shell/"):
        await handle_shell(ws, path, secure)
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
    global _loop_lag_peak_ms
    loop = asyncio.get_event_loop()
    next_cpu = 0.0
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        lag_ms = (loop.time() - t0 - interval) * 1000
        if lag_ms > _loop_lag_peak_ms:
            _loop_lag_peak_ms = lag_ms
        # Piggyback the controller's CPU sampling on this ticker rather than
        # starting a second one: it is one os.times() every 30s, and the
        # windowed CPU it feeds is only read when a support bundle is built.
        if loop.time() >= next_cpu:
            next_cpu = loop.time() + api.CPU_SAMPLE_INTERVAL_S
            api.sample_cpu()
        if lag_ms >= warn_ms:
            log.warning(
                f"[loop] event loop stalled {lag_ms:.0f}ms — "
                f"speaker sends and LED frames were delayed by this much"
            )


async def main():
    log.info(f"EchoMuse Controller {api.CONTROLLER_VERSION}")
    db.init(DB_PATH)
    auth.maybe_generate_bootstrap_token()
    em_player.init(
        get_device=_devices.get,
        notify_state=esphome.push_media_state,
    )

    runner = await api.create_runner(_devices, _shell_pending, _shell_dashboard)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, API_PORT)
    await site.start()
    log.info(f"Dashboard + API listening on http://{SERVER_HOST}:{API_PORT}")

    release_task       = asyncio.create_task(api.release_poll_loop())
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
                api.set_tls_dir(tls_dir)
        except Exception as e:
            log.error(f"Device-link TLS setup failed — wss listener disabled: {e}")

    azc  = AsyncZeroconf()
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
                ping_interval=20,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ))
            if tls_ctx is not None:
                await stack.enter_async_context(websockets.serve(
                    router_tls,
                    SERVER_HOST,
                    SERVER_TLS_PORT,
                    ssl=tls_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                ))
                log.info(f"Device-link TLS (wss) listening on {SERVER_HOST}:{SERVER_TLS_PORT}")
            if REQUIRE_DEVICE_TLS:
                log.info("REQUIRE_DEVICE_TLS=1 — plain/tokenless device connections will be rejected")

            await esphome.start_esphome_servers(_devices, SERVER_HOST)
            # After the voice satellites — BT proxies reuse their zeroconf.
            await em_ble_proxy.start_ble_proxy_servers(SERVER_HOST)

            log.info("EchoMuse Controller ready — waiting for devices")
            await asyncio.Future()

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
        log.info("EchoMuse Controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
