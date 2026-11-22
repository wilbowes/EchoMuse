"""
A connected Echo as the controller holds it: the `Device` object with its
three WebSocket planes, its speaker stream, the per-device state the turn,
wake and barge code read, and the dashboard push of that state.

Moved out of em_controller (2026-11) with no change to the class body.
"""

import asyncio
import collections
import json
import logging
import os

from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_esphome as esphome
import em_events
import em_limiter
import em_listen
import em_mbc
import em_pacing
import em_platform
import em_player
import em_rttlog
import em_scenes
import em_shadow
import em_tap_burst
import em_tasks
import em_tcp
import em_volume
import em_wake_samples
import em_wakelevel
import em_wire

log = logging.getLogger("echomuse")


# A sample at or above this counts as an excursion. 200ms is well clear of a
# healthy hop (Office measures 264ms median for a whole audio round trip
# including frame batching) while catching the ~1s tail under investigation.
RTT_EXCURSION_MS = 200


# Wake word
OWW_MODEL     = os.environ.get("OWW_MODEL", "hey_jarvis")
OWW_THRESHOLD = float(os.environ.get("OWW_THRESHOLD", "0.5"))


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
        # The Sendspin player's status (#89), from sendspin_status and the
        # stats tick; None while the player is off or on firmware without it.
        self.sendspin: dict | None = None
        # An outstanding sendspin_token_request, answered by sendspin_token.
        self.sendspin_token_waiter: asyncio.Future | None = None
        # Set from the register message; None on firmware that predates it.
        self.ambient_light_status: dict | None = None
        # Which userspace the device booted, from its register message.
        # None until a device registers, and None forever for firmware too
        # old to say — which em_platform resolves to Android, leaving the
        # existing fleet exactly as it was.
        self._base_os: str | None = None
        self.kernel_arch: str | None = None
        # From the register message (schema v28): see em_health.
        self.boot_reason: str | None = None
        self.boot_id: str | None = None
        # The last eMMC wear row written, as (day, values), so a reading that
        # has not changed since is not rewritten every stats tick.
        self.wear_written: tuple | None = None
        self.kernel_release: str | None = None

        self.data_ws: WebSocketServerProtocol | None = None
        # Remaining reconnect grace for the speaker stream in flight. Armed by
        # begin_data_stream(); spent down by send_data so the whole stream
        # shares one budget rather than each frame having its own.
        self._data_grace_left: float = 0.0
        # Frames dropped for a missing data plane in the CURRENT stream, and
        # whether the first has been reported. send_data runs once per audio
        # period, so warning per frame writes thousands of identical lines and
        # flushes everything that would explain them out of the support
        # bundle's 2000-line ring (#351).
        self._data_gone_frames: int = 0
        self._data_gone_logged: bool = False
        self.voice_lock   = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self.mic_queue:   asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.voice_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.oww_paused   = asyncio.Event()  # set during voice turn
        # Monotonic instant oww_paused was set, or None. Only used to
        # recognise a state that cannot be legitimate: paused, with no turn
        # holding voice_lock, for longer than any turn can run. While paused
        # the wake loop routes every frame to voice_queue and the no-frames
        # watchdog deliberately stands down, so a stuck flag is SILENT
        # deafness with nothing to end it.
        self.oww_paused_since: float | None = None
        # The live wake listener, replaced when supervision restarts it.
        self.oww_task: asyncio.Task | None = None
        # Timer-alarm ring task (em_timers). None when no timer is ringing;
        # a live Task while a finished HA timer is alerting on this device.
        self.timer_alarm_task: "asyncio.Task | None" = None
        # Monotonic deadline until which the ring is held SILENT, armed when
        # a wake word is heard while it rings so whatever is said next can be
        # heard (em_timers.DismissListen). A deadline, never a flag: a wake
        # that leads nowhere (ceded, stale, a false accept on the chime) must
        # not leave an alarm silent for the rest of its ring.
        self.timer_alarm_hold_t: float = 0.0

        # How many playbacks are streaming-or-draining on the speaker plane.
        # NOT the same thing as `speaking`, which clears as soon as the socket
        # writes finish — those complete near-instantly however slow the link
        # is (see send_ms), while the device is still playing from a ~5.5s
        # buffer. Anything that must not write over live audio has to test
        # this, not `speaking`.
        self.speaker_busy = 0

        # Transient state — read by em_device_view.merge_device()
        self.speaking  = False
        self.muted     = False
        # HA's wake word picker (#286); seeded from the DB at connect.
        self.wake_word_enabled = True
        # #776: set when the live wake word was turned on for a firmware that
        # cannot honour off, so the stored value stays off. Cleared when the
        # stored value is written, which is what makes a later "on" request
        # from Home Assistant stick after an upgrade.
        self.wake_word_temporarily_on = False
        self.listening = False
        self.thinking  = False

        # Volume as HA float (0.0–1.0). Initialised from stored config in
        # handle_control() after config is read; updated on volume_state
        # messages from the device and persisted back to config.
        # Default matches DEFAULT_DEVICE_CONFIG startupVolume=85.
        self.volume: float = em_volume.device_level_to_ha(85)

        self.data_ready = asyncio.Event()
        # #299: whether ANY mic frame has arrived on the CURRENT data
        # connection. Cleared by handle_data on every new connection; set
        # by the wake listener when a frame flows. Lets the no-frames
        # watchdog tell "transport dropping" apart from "stream dead" —
        # but only within a grace window (see the watchdog: a connection
        # that stays silent BEYOND it is a zombie stream, and the ladder
        # is that case's only repair).
        self.frames_seen_this_connection: bool = False
        self.data_connected_at: float = 0.0

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
        # Wake-word training clips use the already-streamed wake audio. The
        # default is off because these clips may contain private speech.
        self.wake_clip_capture: bool = False
        self.wake_clip_min_score: float = 0.20
        self.wake_capture: em_wake_samples.WakeCapture = em_wake_samples.WakeCapture()
        # streamReply: play a reply from HA's early streaming signal rather than
        # TTS_END (em_earlytts). Read per turn by em_esphome. Config key:
        # streamReply. Off by default.
        self.stream_reply:  bool  = False
        # This turn's captured mic audio, handed from _stream_mic_audio to
        # em_turn_record.persist_turn (which owns the write — it has the rowid the
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
        # pending/result state lives in em_device_view._wifi_states instead — this
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
        self.pending_wake: em_shadow.PendingWake = em_shadow.PendingWake()
        # This controller's own crossings while the DEVICE is triggering —
        # the comparison from the other side. Kept in "on" mode because the
        # question that justified on-device wake ("do the two agree?") is
        # still worth answering once the roles are swapped, and this is the
        # only place a controller miss can be seen at all.
        self.ctrl_shadow: em_shadow.ShadowTracker = em_shadow.ShadowTracker()

        # Private listening (docs/listening.md). What the device last said it
        # is doing with its wake stream — "local", "stream" or "degraded" —
        # and why. None until it reports on this connection: the dashboard
        # must show that as unknown, never as private. `listen_view` combines
        # it with the configured mode and the firmware's capability.
        self.listen_reported: str | None = None
        self.listen_reason: str = ""
        # Which session audio reaches a turn (em_listen.SessionRouter), and
        # the session the current turn is listening on.
        self.listen_router = em_listen.SessionRouter()
        self.listen_session: int | None = None
        # Device wakes under private listening, consumed by _private_listen.
        # Bounded: a wake is only worth acting on for MAX_PENDING_WAKE_S.
        self.listen_wakes: asyncio.Queue = asyncio.Queue(maxsize=8)
        # The turn a private wake started, so a second wake while it is being
        # set up is treated as a barge, never as a second turn.
        self.private_turn_task: asyncio.Task | None = None
        # Smoothed control-plane RTT, for capture-time arbitration.
        self.rtt_est = em_listen.RttEstimator()
        # When each 0x01 frame was captured, from its sequence number
        # (em_listen.CaptureClock). Not meaningful on a VAD-gated turn
        # stream, which skips frames — mic_gated says which is running.
        self.capture_clock = em_listen.CaptureClock()
        # The Echo's monotonic clock in ours, from ping replies carrying
        # `mono` (em_listen.DeviceClock); dates a private wake's capture.
        self.device_clock = em_listen.DeviceClock()
        self.mic_gated = False

        # Per-room noise floor estimate (normalized RMS, 0..1), tracked from
        # the continuous wake stream in wake_word_listener. Measurement only —
        # never applied to the audio (see 2026-07-06 architecture discussion:
        # adaptation as measurement, not signal modification). Consumers:
        # em_esphome._stream_mic_audio's SNR-relative no-speech detection,
        # and diagnostics (near-miss logs). Asymmetric tracker: follows drops
        # quickly, rises slowly, so speech doesn't drag the floor up.
        self.noise_floor: float = 0.0
        # The wake stream's recent frame levels and the mic gain to remove
        # from them: see em_wakelevel.
        self.wake_levels = em_wakelevel.LevelRing()
        self.mic_gain_db: float = 24.0
        # (MICPGA, digital gain): the gain a wake's level reading still
        # contains. Echoes that differ here are not compared by loudness.
        self.adc_gains: tuple = (40, 88)

        # Barge-in (§3.2): wake word interrupts the thinking phase or TTS
        # playback. Controller-side feature — with it enabled the mic keeps
        # streaming through the turn (device AEC subtracts the speaker
        # output during playback) and _barge_watcher scores the stream from
        # STT_VAD_END onward (barge_threshold during playback, the normal
        # wake threshold during thinking); on detection it sets
        # barge_detected + cancel_event (plus speaker_flush or HA pipeline
        # cancel, phase-dependent) and the turn loop re-enters a fresh
        # turn. The barge watcher uses a dedicated OWW instance (the main wake
        # listener task is blocked awaiting the turn), cached module-side per
        # device_id and reused across reconnects — see em_wake_model.acquire_barge_model.
        self.barge_in_enabled = False
        # False until config is pushed, so a device connecting before then
        # keeps the historical tap-starts-a-turn behaviour.
        self.button_single_tap_event = False
        self.button_multi_tap_ms = 0
        self.tap_burst = em_tap_burst.TapCoalescer(
            lambda name: esphome.send_button_event(self.device_id, name),
            enabled=lambda: self.button_single_tap_event,
            on_error=em_tasks.log_exception,
        )
        self.barge_threshold  = 0.6
        self.barge_detected   = False
        # Set when the interrupting utterance was arbitrated away to another
        # Echo. Separate from barge_detected because the two answer different
        # questions: barge_detected says playback must stop (the user spoke
        # over this device, and that is true whoever ends up answering),
        # barge_ceded says this device must not run the interrupting turn.
        # Folding them into one flag is how both devices answered.
        self.barge_ceded      = False
        # Clear while a barge's claim is undecided; see _await_barge_decision.
        self.barge_decided    = asyncio.Event()
        self.barge_decided.set()
        # (trigger_label, wake_detail) when the barge that stood down had no
        # pipeline behind it, else None. Carried from _barge_watcher /
        # _private_barge to the turn loop's ceded branch, which is where the
        # row and the cue are written — see the comment there for why not at
        # the point the barge fired.
        self.barge_no_ha      = None

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
        # em_turn_record.persist_turn if fresh (staleness window keeps a
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
        # Set on a COMPLETED playback and read when the device's
        # playback_stats arrives. They lived after drain_rtt()'s `return` for
        # months, so they were never initialised and only existed once a
        # successful playback had assigned them. A barge-in cancel skips that
        # assignment, so the next playback_stats raised AttributeError in the
        # control handler and took the whole connection down with it —
        # measured 2026-08-20 as a device disconnecting on every barge-in.
        # -1 reads as "not measured", which is what the DB layer expects.
        self.playback_send_ms: int = -1
        self.playback_eq_ms:   int = -1
        # Set when the device reports playback_stats for the stream being
        # played. This is the authoritative "the audio has finished" signal
        # — the device emits it once its audio channel has drained after
        # EOS, i.e. when the last period has gone to ALSA. Awaited in place
        # of the wall-clock estimate that used to clear the ring while the
        # device was still playing (up to 6.1s early, 2026-07-24).
        #
        # A QUEUE of waiters, one per playback, rather than a single Event on
        # the device. It was one Event with two waiters and one setter, so two
        # concurrent playbacks both woke on whichever report arrived first —
        # observed 2026-08-28 as two `Playback complete` lines in the same
        # millisecond, and an announcement whose wait ended after a chime's
        # duration rather than its own (#373).
        #
        # FIFO because the device plays one stream at a time and reports in
        # the order it finishes them, so the oldest outstanding playback is
        # the one a report belongs to.
        #
        # This also retires the `clear()` that every caller had to remember:
        # a fresh Event per playback cannot carry a stale set from the
        # previous one, so the hazard those calls guarded against is gone by
        # construction rather than by discipline.
        self._playback_waiters: collections.deque = collections.deque()
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
        # Log-line coalescing only — the counters above are the measurement.
        self.rtt_log = em_rttlog.ExcursionLog(self.device_id)
        # Downlink loss from the kernel's counters on our own sockets to this
        # device (em_tcp); drained with the RTT window on each stats report.
        self.tcp_loss = em_tcp.LossWindow()
        # Loss per minute for the last 30, for the Status tab's link quality
        # — the hourly rollup is too coarse to show a link recovering.
        self.tcp_minutes = em_tcp.MinuteStrip()

    def is_busy(self) -> bool:
        """Whether this device was doing anything when a ping went out."""
        return bool(
            self.voice_lock.locked()
            or self.speaking
            or em_player.is_playing(self.device_id)
        )

    # ── Playback completion ──────────────────────────────────────────
    #
    # One waiter per playback. See _playback_waiters for why this is a queue
    # and not a single Event.

    def begin_playback(self) -> asyncio.Event:
        """Register a waiter for this playback and return it."""
        ev = asyncio.Event()
        self._playback_waiters.append(ev)
        return ev

    def end_playback(self, ev: asyncio.Event) -> None:
        """
        Retire a waiter, whether or not the device ever reported.

        Must be called from a finally: a playback cancelled mid-stream (a
        barge-in, a mute, a dropped device) never gets its report, and a
        waiter left in the queue would take the NEXT playback's report and
        desynchronise every one after it. Idempotent, because teardown paths
        in this file are reached more than once by design.
        """
        try:
            self._playback_waiters.remove(ev)
        except ValueError:
            pass

    def signal_playback_done(self) -> None:
        """
        Resolve the oldest outstanding playback: the device has finished one.

        A report with nothing waiting is dropped rather than remembered. That
        matches the old single-Event behaviour for a stray report, and a
        report cannot be "saved up" for a playback that has not started —
        which is the stale-set hazard the old `clear()` calls existed for.
        """
        if self._playback_waiters:
            self._playback_waiters.popleft().set()

    def record_rtt(self, rtt_ms: int, was_busy: bool) -> None:
        self.rtt_est.add(rtt_ms)
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

    def drain_tcp(self) -> dict:
        """Downlink segments and retransmits since the last report, summed over
        the control and data planes (em_tcp.LossWindow). Empty when neither
        socket could be read, so it stores as NULL rather than a clean link."""
        snaps = {}
        for ws in (self.control_ws, self.data_ws):
            transport = getattr(ws, "transport", None)
            if transport is not None:
                snaps[id(ws)] = em_tcp.read_info(transport.get_extra_info("socket"))
        return self.tcp_loss.drain(snaps)

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
        # Report the previous stream's total before re-arming. This is the
        # only lifecycle hook either side of a stream, so the count would
        # otherwise be reset without ever being said out loud.
        if self._data_gone_frames:
            log.warning(
                f"[{self.device_id}] Previous stream dropped "
                f"{self._data_gone_frames} frames with no data connection"
            )
        self._data_grace_left = DATA_RECONNECT_GRACE_S
        self._data_gone_frames = 0
        self._data_gone_logged = False

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
            # Once per stream, not once per frame — the rest are counted and
            # reported as a total by the next begin_data_stream.
            self._data_gone_frames += 1
            if not self._data_gone_logged:
                self._data_gone_logged = True
                log.warning(
                    f"[{self.device_id}] No data connection — dropping this "
                    f"stream's remaining frames silently"
                )
            return
        try:
            await self.data_ws.send(data)
        except Exception as e:
            log.warning(f"[{self.device_id}] Data send failed: {e}")

    async def send_gatt(self, payload: bytes) -> bool:
        """
        One Bluetooth connection-bridge message to the device (#656). Returns
        False when there is no data connection: unlike a speaker stream there
        is nothing to ride out, the request fails and Home Assistant is told.
        """
        ws = self.data_ws
        if ws is None or not self.ble_connect_capable:
            return False
        try:
            await ws.send(bytes((em_wire.BLE_GATT_TYPE,)) + payload)
            return True
        except Exception as e:
            log.warning(f"[{self.device_id}] ble gatt send failed: {e}")
            return False

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
    def output_chain_on_device(self) -> bool:
        """
        Whether this device runs EQ, bass guard and limiter itself, at its
        ALSA write — in which case every playback path here sends the audio
        untouched (em_eq.Passthrough).

        On the device, a change is heard within one period. Here it could not
        reach the ~5.5s already queued on the device, and the dashboard's EQ
        was only ever "immediate" for audio not yet sent.
        """
        return "output_chain" in (self.capabilities or [])

    @property
    def timer_alarm_ringing(self) -> bool:
        """A finished timer is alerting on this device right now."""
        return self.timer_alarm_task is not None and not self.timer_alarm_task.done()

    @property
    def timer_alarm_held(self) -> bool:
        """The ring is paused for someone who has just said the wake word."""
        return self.timer_alarm_hold_t > asyncio.get_event_loop().time()

    @property
    def mic_capable(self) -> bool:
        """Announces "mic" (every firmware did until boards without supported
        capture). Capabilities not yet known are not read as no mic."""
        return not self.capabilities or "mic" in self.capabilities

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
    def pairing_capable(self) -> bool:
        """
        Whether this firmware asks to pair itself (action button held, then a
        pair_request or a plain dial with `pairing`). Without it an admin
        starts pairing from the dashboard, since the device cannot ask.
        """
        return "pairing" in (self.capabilities or [])

    @property
    def ble_connect_capable(self) -> bool:
        """
        Whether this firmware can hold Bluetooth LE connections for Home
        Assistant (#656). Without it the proxy stays passive and the setting
        is shown disabled with the reason.
        """
        return "ble_connect" in (self.capabilities or [])

    @property
    def sendspin_capable(self) -> bool:
        """
        Whether this firmware can be a Sendspin player (#89). The dashboard
        shows the section disabled with the reason without it.
        """
        return "sendspin" in (self.capabilities or [])

    @property
    def wake_cue_capable(self) -> bool:
        """
        Whether this firmware can play its own wake sound (#120). The
        toggle is disabled without it: a switch that saves and stays silent
        fails the person this accessibility setting exists for.
        """
        return "wake_cue" in (self.capabilities or [])

    @property
    def wake_word_off_capable(self) -> bool:
        """
        Whether this firmware honours wakeWordEnabled=false (#286): a
        crossing opens no session, so a privately listening Echo sends
        nothing while HA's picker says "No wake word".
        """
        return "wake_word_off" in (self.capabilities or [])

    @property
    def wake_mic_capable(self) -> bool:
        """Whether firmware can listen for the wake word on a chosen mic (#705)."""
        return "wake_mic" in (self.capabilities or [])

    @property
    def identify_ring_capable(self) -> bool:
        """Whether firmware can take the whole ring on `identify` (#860)."""
        return "identify_ring" in (self.capabilities or [])

    @property
    def volume_cue_capable(self) -> bool:
        """Whether physical volume changes can play an idle preview tone."""
        return "volume_cue" in (self.capabilities or [])

    @property
    def remote_volume_arc_capable(self) -> bool:
        """Whether remote volume changes can show the cyan level arc (#634)."""
        return "remote_volume_arc" in (self.capabilities or [])

    @property
    def response_level_capable(self) -> bool:
        """Whether firmware can apply the relative voice-response gain."""
        return "response_level" in (self.capabilities or [])

    @property
    def stock_sound_capable(self) -> bool:
        """Whether firmware voices its speaker and holds the bass guard on."""
        return "stock_sound" in (self.capabilities or [])

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

    @property
    def oww_local_capable(self) -> bool:
        """Whether this firmware can listen privately (docs/listening.md).
        "Could it" — whether it IS comes from listen_view."""
        return "oww_local_only" in (self.capabilities or [])

    @property
    def listen_view(self) -> "em_listen.ListenView":
        """What this Echo is actually doing with its microphone."""
        return em_listen.resolve(
            em_shadow.normalise_mode(self.oww_on_device),
            self.capabilities, self.listen_reported, self.listen_reason,
        )

    @property
    def private_listening(self) -> bool:
        """Turns start from device wakes with sessions; no wake stream."""
        return self.listen_view.private

    @property
    def aec_hw_ref_capable(self) -> bool:
        """
        Whether this firmware can take the AEC far-end reference from a
        hardware playback loopback in the mic capture, falling back to the
        software tap at the ALSA write when the board has none.

        This answers "could it" — `aec_ref` below answers "is it, right
        now", the same split as oww_shadow_capable against shadow.active.
        Both are needed and neither substitutes for the other: proving a
        channel is a loopback requires the speaker to have played, which has
        not happened at registration, so a capability alone can never say
        the reference was found.

        What it gates is the AEC delay control. That slider compensates
        write-to-ear latency for the software tap; on a frame-aligned
        hardware reference there is no latency to compensate, so a live
        slider there is a knob that does nothing.
        """
        return "aec_hw_ref" in (self.capabilities or [])

    @property
    def aec_ref(self) -> str | None:
        """
        The far-end reference cancellation is actually running on: "hw",
        "sw", "off", or None from firmware too old to report it.

        None is NOT "sw". Every device predating this reports nothing, and
        reading that as the software tap would tell the dashboard a fact
        about a device that never stated one — the same reason a missing
        playback_stats stores NULL rather than zero.
        """
        return (self.stats or {}).get("aecRef")

    @property
    def base_os(self):
        """
        The userspace the device booted: "emos", "fireos", or None.

        None means the firmware is too old to report it, NOT FireOS. The
        distinction is the whole point of the field: `android_userspace`
        below acts on it, and reading absence as Android would keep pushing
        the debloat payload at an emOS device that has no package manager.

        Set from the REGISTER message, not the stats tick. It rode the stats
        report for exactly one commit, which put it 30 seconds too late for
        its only consumer — the payload reconcile runs on connect, so it read
        None, pushed the Magisk service.d script at an emOS device, and each
        attempt sat out the full 120s transfer timeout waiting for a
        TRANSFER_OK that could never come.
        """
        return self._base_os

    @property
    def android_userspace(self) -> bool:
        """
        Whether Android-only payloads mean anything on this device.

        The rule and its asymmetry live in em_platform, where a test can
        reach them without aiohttp.
        """
        return em_platform.android_userspace(self.base_os)

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
        # One gate for every call site that restarts the wake stream (#286).
        # mic_start_turn is not gated: an HA-initiated turn is HA's decision.
        if not self.wake_word_enabled:
            log.debug(f"[{self.device_id}] mic_start skipped — wake word off")
            return
        self.mic_gated = False
        await self.send_control({"type": "mic_start"})

    async def mic_start_turn(self):
        """Start mic for a voice turn — signals device to lock the best directional mic."""
        self.mic_gated = True
        await self.send_control({"type": "mic_start", "lock_mic": True})

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

    async def listen_ack(self, session: int):
        await self.send_control({"type": "listen_ack", "session": session})

    async def listen_close(self, session: int | None = None, reason: str = ""):
        """End a private-listening session (the active one by default).
        Closes it in the router too, so its stragglers are dropped."""
        target = self.listen_router.close(session)
        if session is None and target is None:
            return
        if target == self.listen_session:
            self.listen_session = None
        await self.send_control({"type": "listen_close",
                                 "session": target if target is not None else session,
                                 "reason": reason})

    async def beam_lock(self):
        # Lock the beamformer onto the speaker's perimeter mic mid-stream —
        # no stream restart. Device no-ops if already locked or if
        # beamformingEnabled is false in its config.
        await self.send_control({"type": "beam_lock"})

    async def beam_unlock(self):
        await self.send_control({"type": "beam_unlock"})

    async def play_cue(self, name: str):
        """
        Ask the device to play a cue it generates itself: one small JSON
        message, never delayed by the prime gate or dropped by a flush.
        """
        await self.send_control({"type": "play_cue", "cue": name})

    async def identify(self):
        """Ask the Echo to take its whole ring for 30s (identify_ring, #860)."""
        await self.send_control({"type": "identify"})

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
          em_wire.SPEAKER_PRIME_SECONDS is queued (primePeriods, pcm_speaker.go), so
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
            await push_device_state(self)
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
                chunk = pcm[offset:offset + em_wire.SPEAKER_BYTES]
                if len(chunk) < em_wire.SPEAKER_BYTES:
                    # Pad the final partial period with silence — without this,
                    # up to one full period (~42ms at 48kHz) of the last word is
                    # silently dropped because the old loop required a full period.
                    chunk = chunk + bytes(em_wire.SPEAKER_BYTES - len(chunk))
                await self.send_data(bytes([em_wire.SPEAKER_FRAME_TYPE]) + chunk)
                offset += em_wire.SPEAKER_BYTES
        finally:
            # NOT where speaking clears — see em_playback.run_post_turn_playback. This
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
                await asyncio.shield(self.send_data(bytes([em_wire.SPEAKER_EOS_TYPE])))
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
        # Audio actually handed to the socket, in seconds of playback, and
        # the time pacing spent waiting. sent_seconds against wall time since
        # the first period is the lead being held.
        sent_seconds = 0.0
        paced_seconds = 0.0

        async def send_period(chunk: bytes) -> None:
            """Send one whole device period, stamping the first one."""
            nonlocal first_send_time, send_seconds, sent_seconds, paced_seconds
            if first_send_time is None:
                first_send_time = asyncio.get_event_loop().time()
                self.playback_send_t0 = first_send_time
                await self._set_speaking(True)
                log.info(
                    f"[{self.device_id}] First streamed PCM period "
                    "sent to device"
                )
            else:
                # Hold the queue to em_wire.VOICE_LEAD_S ahead of realtime — see the
                # constant for why sending faster is pure cost. The first
                # period is exempt so nothing delays the start of playback,
                # and the lead is then free to build at full speed until it
                # is reached, which keeps the prime gate as prompt as it was.
                delay = em_pacing.lead_delay(
                    sent_seconds,
                    asyncio.get_event_loop().time() - first_send_time,
                    em_wire.VOICE_LEAD_S,
                )
                if delay > 0:
                    paced_seconds += delay
                    # Raced against cancel_event, never a bare sleep: a
                    # barge-in or mute has to land inside the pacing gap
                    # rather than after it, or pacing would add its own
                    # latency to the thing it must never delay.
                    try:
                        await asyncio.wait_for(
                            self.cancel_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    if self.cancel_event.is_set():
                        return
            _t_send = asyncio.get_event_loop().time()
            await self.send_data(bytes([em_wire.SPEAKER_FRAME_TYPE]) + chunk)
            send_seconds += asyncio.get_event_loop().time() - _t_send
            sent_seconds += em_wire.SPEAKER_PERIOD_SECONDS

        async def drain_whole_periods() -> None:
            while len(pending) >= em_wire.SPEAKER_BYTES:
                if self.cancel_event.is_set():
                    break
                chunk = bytes(pending[:em_wire.SPEAKER_BYTES])
                del pending[:em_wire.SPEAKER_BYTES]
                await send_period(chunk)

        try:
            async for pcm in pcm_chunks:
                if self.cancel_event.is_set():
                    break
                total_pcm += len(pcm)
                eq_started = asyncio.get_event_loop().time()
                pending.extend(stream_eq.process(pcm))
                eq_seconds += asyncio.get_event_loop().time() - eq_started

                await drain_whole_periods()

            # The limiter holds a look-ahead tail; without this the last few
            # ms of every response are dropped. Inaudible on a long track and
            # obvious on a short announcement, which is the kind of thing that
            # goes unnoticed for months.
            if not self.cancel_event.is_set():
                pending.extend(stream_eq.flush())

            # The flush can push `pending` back over a period, so it has to be
            # drained again before the remainder is padded. It used to pad
            # straight from here, and `bytes(em_wire.SPEAKER_BYTES - len(chunk))` is a
            # ValueError once that goes negative: the drain loop above leaves
            # 0-4095 bytes and the tail adds 478, so any remainder over 3618
            # killed the response — 11.7% of them, measured as `negative
            # count` on the 2026-08-25 soak (#331). The invariant is that a
            # period is drained after the flush, not that the tail is small
            # enough to fit.
            await drain_whole_periods()

            if pending and not self.cancel_event.is_set():
                chunk = bytes(pending) + bytes(em_wire.SPEAKER_BYTES - len(pending))
                await send_period(chunk)
        finally:
            # NOT where speaking clears — see the note in stream_speaker.
            # One EOS terminates the complete response. Sending EOS per HTTP
            # chunk would make the device repeatedly prime and flush.
            try:
                await asyncio.shield(self.send_data(bytes([em_wire.SPEAKER_EOS_TYPE])))
            except BaseException:
                pass

        return (total_pcm, int(eq_seconds * 1000), first_send_time,
                int(send_seconds * 1000), int(paced_seconds * 1000))


async def push_device_state(device: Device) -> None:
    """Push current transient device state to dashboard clients."""
    await em_events.push({
        "type":      "device_update",
        "device_id": device.device_id,
        "state": {
            "connected": True,
            "speaking":  device.speaking,
            "muted":     device.muted,
            "wake_word_enabled": device.wake_word_enabled,
            "listening": device.listening,
            "thinking":  device.thinking,
        },
    })
