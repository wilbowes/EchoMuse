"""
db.py — EchoMuse Controller persistence layer
==============================================

SQLite-backed storage for device registry, per-device config, logs,
users, sessions, system configuration, and activity stats (voice turns,
hourly wake/near-miss counters, hourly hardware metrics).

All public functions are synchronous — they are intended to be called
from asyncio handlers via loop.run_in_executor() when called on the
hot path, or directly for startup/shutdown operations.

Usage:
    import db

    db.init("echomuse.db")          # call once at startup

    device = db.get_device(device_id)
    db.upsert_device_seen(device_id, ip, version)
    db.log_device(device_id, "info", "device", "Connected")
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

import em_config_sections
import em_recordings

log = logging.getLogger("echomuse.db")

# ─── Default device config ────────────────────────────────────────────────────

DEFAULT_DEVICE_CONFIG = {
    # owwOnDevice: on-device wake word scoring. "off" or "shadow".
    # Shadow scores the wake stream on the device and reports what it WOULD
    # have detected, without acting on it, so the two can be compared on the
    # same audio. Default off and it should stay that way: it costs ~38% of one
    # core permanently on top of the ~18-20% mic-pipeline baseline, and it
    # needs ONNX Runtime plus the models installed on the device out of band
    # (they are not in the firmware). Enable on ONE device at a time.
    "owwOnDevice":      "off",
    "adcDigitalGain":   88,
    "adcMicpga":        40,
    # micGainDb: fixed digital gain (dB) the device applies to the full
    # 24-bit capture before quantising to the 16-bit stream. Sized from
    # 20h fleet logs (2026-07-07): speech RMS at wake detection was
    # 0.0001–0.0006 FS (~3–20 LSB in 16-bit — the old S24→S16 truncation
    # discarded most of the signal), loudest observed chunk 0.0035 FS, so
    # +24dB (×16) lifts speech into a usable range with ample clipping
    # headroom. Device clamps to [0, 42]; clipped-sample count appears in
    # the device's periodic VAD diag log. Note: the device interprets
    # vadThreshold in pre-gain units (threshold is scaled by the gain
    # internally), so this can be tuned without retuning vadThreshold.
    "micGainDb":        24,
    # AEC (speexdsp, device-side, whole mic path incl. wake stream).
    # Default ON, and coupled to bargeInEnabled below: with barge-in the mic
    # streams throughout playback, and AEC is the only thing stopping the
    # device waking on its own TTS. Turning one on without the other is the
    # self-trigger case the barge threshold reasoning assumes away, so if
    # this goes back to False, bargeInEnabled must go with it.
    # ~14dB attenuation per response, held across turns since v2.7.8; check
    # the [aec] att= logs when tuning.
    # aecDelayMs: 0, measured on hardware 2026-07-08 — the mic side reads
    # 160ms ALSA batches, which eats most of the speaker's write-to-ear
    # latency; the filter tail absorbs the remainder. (The original 250
    # guess made the echo arrive *before* its reference — non-causal, zero
    # cancellation.) aecTailMs is the adaptive filter length (residual
    # delay + room reverb). Device clamps: delay 0–1000, tail 50–500.
    "aecEnabled":       True,
    "aecDelayMs":       0,
    "aecTailMs":        300,
    "startupVolume":    85,
    # vadThreshold: 0.001 (normalised RMS pre-AGC).
    # Q2 fix (2026-07-05 review, tracked as B6): this was drifted to 0.003 in
    # a previous "reconciliation" that got it backwards — 0.003 sits *above*
    # the measured conversational speech range (0.0004–0.0010 at 1.3m per
    # SETUP.md's handoff table), meaning a fresh device or a config reset
    # would fail to gate speech at all at normal distance. 0.001 is the
    # value actually validated during the v2.6.3 speech-quality session and
    # confirmed working in both quiet-office and TV-on-lounge testing — it's
    # also what the dashboard slider's own fallback already defaulted to
    # (config.vadThreshold ?? 0.001 in dashboard.jsx), so this closes a
    # three-way mismatch between the DB default, this comment, and the UI
    # rather than resolving it in favour of the wrong side. Raise to
    # 0.003–0.005 only in genuinely noisy rooms (TV, music) via the
    # dashboard, not as the shipped default.
    "vadThreshold":     0.001,
    "vadSpeechMs":      32,
    # vadSilenceMs: 900ms — up from 600. Prevents premature endpoint on
    # natural mid-sentence pauses. Dashboard UI already defaults to 800;
    # this closes the DB/UI mismatch. Tune up to 1200 if sentences still
    # get clipped; 600 caused the "must finish quickly" behaviour.
    "vadSilenceMs":     900,
    # True: a tap fires "single" on the HA event entity instead of starting a
    # turn. Gives up cancel-by-tap; mute still cancels. Hold is unaffected.
    "buttonSingleTapEvent": False,
    # Window (ms) for coalescing taps into double/triple; 0 disables. Delays
    # every tap by this much, which is why it needs buttonSingleTapEvent — the
    # delay is only acceptable once a tap is an event rather than speech.
    # 300ms is a reasonable starting point.
    "buttonMultiTapMs": 0,
    # owwThreshold: 0.5 — openwakeword's own recommended default, and what
    # this fleet independently converged on. It was 0.3, which is measurably
    # too sensitive: of 519 fleet-hours that recorded a near miss while
    # running 0.5, 212 peaked at >= 0.3, clustered hard against the ceiling
    # (0.4992, 0.4987, 0.4971, ...). Every one of those would have fired a
    # spurious turn on a default install — ring up, HA pipeline run, most
    # likely ending no_speech. Lower it per device if a custom wake model
    # trades recall for false positives (see oww_forge/README.md).
    "owwThreshold":     0.5,
    # Barge-in (§3.2, controller-side): wake word spoken during TTS playback
    # cancels it and starts a fresh turn. Requires device AEC (aecEnabled)
    # on — with barge-in the mic streams through playback, and AEC is what
    # stops the device hearing itself — so aecEnabled above defaults on with
    # it, and the two move together. bargeInThreshold is used as-is,
    # deliberately BELOW the normal wake threshold: the echo at the mic is
    # ~25dB louder than the person talking over it, so speech-over-TTS wake
    # scores are inherently depressed (~0.10–0.12 measured), while post-AEC
    # self-echo scores only 0.004 (0.055 worst-case unconverged) — there is
    # no self-trigger risk down to ~0.08.
    #
    # That 0.004 is STALE and the margin is wider than it says: v2.7.8 made
    # the filter hold convergence across turns, so self-echo measures
    # 0.002–0.003. 0.10 sat awkwardly close to real speech-over-TTS scores,
    # which is the wrong way to be wrong — barge-in that never fires is
    # undiagnosable from the outside ("it just ignores me"), while barge-in
    # that fires too eagerly is self-evident and adjustable. 0.05 is the
    # dashboard slider floor, so the only way to tune from here is UP, which
    # is the direction the visible failure asks for.
    #
    # Watch one interaction: the beamformer locks a different mic per turn
    # and each has its own echo path, so AEC re-converges per turn (per-
    # channel filter states are the unbuilt fix) — and the one measured
    # self-echo figure above 0.05 is that unconverged case at 0.055. The
    # symptom would be a device cutting its own response short. This fleet
    # runs 0.05 with beamforming and AEC both on and does not do that.
    "bargeInEnabled":   True,
    "bargeInThreshold": 0.05,
    # How far music is attenuated while a voice turn plays OVER it, on
    # firmware that can mix the two planes (the "audio_mix" capability).
    # Ducking replaces pausing there: the music feed runs 4s ahead of
    # realtime, so pausing costs a seek that a Music Assistant flow stream
    # cannot perform. A taste parameter — it wants tuning by ear in a real
    # room, like the LED meter curve, not a firmware push per attempt.
    "duckDb": -18.0,
    "owwModel":         "hey_jarvis_v0.1",
    # Multi-device wake SUPPRESSION window (ms), not a wait. The first
    # device to detect answers immediately; any other device detecting
    # within this window stands down. 0 disables. Costs no latency to
    # anyone — see em_arbiter for why first-detector beats best-SNR.
    # 700ms: the observed spread between devices hearing one utterance is
    # ~200ms (device mic batching is 160ms), and the window also needs to
    # cover a device that hears the winner's TTS a moment later.
    "wakeArbitrationMs": 700,
    # owwSpeexNs: openwakeword's built-in speexdsp noise suppressor (Q1,
    # 2026-07-05 review). 16kHz-native, applied controller-side, only to
    # the wake-word detection path — cannot affect STT audio since STT
    # never sees it. Defaults False, but no longer for the original reason:
    # the packaging blocker cleared (pinned speexdsp-ns==0.1.2 in
    # requirements.txt, imports cleanly in the image), so the noisy-room
    # A/B this is waiting on is now actually runnable. Off until someone
    # runs it — not off because it cannot be turned on.
    "owwSpeexNs":       False,
    # nsAsr: DTLN noise suppression (em_ns.py), controller-side, applied
    # ONLY to the turn audio streamed to HA's STT — the wake stream and
    # all noise-floor measurement stay raw. Helps steady noise (fan, AC,
    # hum) at marginal SNR; does little against competing speech (TV) —
    # that's the beamformer's job. Default off, and the A/B that was
    # "pending" has since run with a result that is NOT yet reconciled: with
    # NS on, recordings showed 8-15% of samples at exact digital zero at a
    # healthy signal level (the gate chewing speech); with it off, 0.3%.
    # That is a candidate cause of the choppy-audio reports (#137). This
    # fleet nonetheless runs it on, with one device explicitly overriding to
    # off — so treat "on" as unvalidated rather than recommended until
    # someone listens to a pair of recordings and decides.
    # Models are vendored into the Docker image, so if the
    # files are missing (bare-metal without NS_MODEL_DIR) the flag
    # degrades to raw streaming with a warning.
    "nsAsr":            False,
    # saveUtterances: keep the mic audio of the last few voice turns
    # (em_recordings.KEEP_PER_DEVICE) as WAVs, downloadable from the
    # Activity tab. Diagnostic tooling for "is my mic any good" — the
    # question you otherwise have to answer by inference from a bad
    # transcript. Default OFF and deliberately so: this is the only feature
    # that writes recognisable speech to disk, and turning it on should be
    # a decision, not a default someone discovers later. Controller-side
    # only (the device never sees the audio again); the key rides the
    # config channel and the device ignores it, same as wakeArbitrationMs.
    "saveUtterances":   False,
    # bleProxyEnabled: BLE proxy (device-side passive scan over the raw HCI
    # transport, forwarded to HA as a separate ESPHome bluetooth_proxy
    # device — em_ble_proxy.py). Default off: enabling durably disables the
    # Android Bluetooth stack on the device (required — /dev/stpbt is
    # single-owner) and brings up a second ESPHome listener + mDNS entry.
    "bleProxyEnabled":  False,
    # beamformingEnabled: True — ch6 (centre/omni) hears the wake word, then
    # the turn locks to the best perimeter mic. The flag ONLY gates Lock():
    # unlocked is always ch6 and the wake path never locks, so the wake
    # stream is ch6 either way (beamformer.go). It cannot splice wake audio.
    #
    # This was False, on a comment describing the every-32ms reselection that
    # be2f16d (v2.6.3 P0-2) had already fixed in the same commit. The real
    # reason recorded there was that onset discrimination was unreliable at
    # <=1.5m, "re-enable once P0-3/P0-4 are addressed" — P0-3 closed
    # 2026-07-12 (DTLN) and v2.7.2's lock-back selection fixed the decayed-
    # spike picks that caused most of the wrong-lock risk. Nobody went back.
    # Meanwhile this fleet has run True since the 2026-07-20 config restore
    # with no reported regression, so True is the value with field evidence
    # behind it and False is the one that has not been run in months.
    "beamformingEnabled": True,
    "beamAngle":        -1,
    "eqBands":          [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "eqLoudness":       False,
    # Output limiter. On by default: the EQ chain hard-clipped anything it
    # boosted past full scale (#231), and a default that leaves that in place
    # protects nobody. Threshold/release are config rather than constants
    # because limiter character wants tuning by ear in a real room — the same
    # reasoning as duckDb and the LED meter curve.
    "limiterEnabled":   True,
    "limiterThreshold": -1.0,
    "limiterRelease":   150,
    # LED ring scene (controller-side rendering — see em_scenes.py).
    # ledListenColor/ledThinkColor only apply when ledScene is "custom".
    "ledScene":         "standard",
    "ledListenColor":   "#00b400",
    "ledThinkColor":    "#00c800",
    # Playback "meter" ring response curve — how hard the ring throbs with
    # the speaker level. Device-side defaults live in animator.go
    # (meterDefaults) and these mirror them; both are clamped independently.
    # Exposed as config because it is a taste parameter that needs several
    # passes in a real room, and a firmware OTA per pass is not a sane
    # tuning loop. See docs/led-ring-states.md.
    "meterAttack":      0.6,   # envelope rise per 40ms tick
    "meterDecay":       0.30,  # fall per tick; ~133ms tracks syllable rate
    "meterFloor":       0.06,  # perceptual brightness at silence
    "meterGamma":       2.2,   # >1 expands the dark end so the swing reads
    "meterRef":         0.22,  # speaker RMS mapped to full brightness
    "meterCurve":       0.7,   # <1 lifts quiet consonants
    # agcEnabled: automatic gain control (lockMic/button turn streams only).
    # Disable to hear raw mic levels. (nsEnabled/RNNoise removed 2026-07-12
    # with the device-side RNNoise code — a stale nsEnabled key in stored
    # configs is harmless: new firmware ignores unknown fields, and old
    # firmware keeps honouring the stored False until it's OTA'd.)
    "agcEnabled":       True,
}

# Maximum log rows retained per device. Older rows are pruned on insert.
LOG_RETENTION = 10_000

# Maximum voice-turn rows retained per device (pruned on insert). At even
# 100 turns/day this is many months of history.
TURN_RETENTION = 20_000

# Hourly wake_counters rows older than this are pruned on upsert.
WAKE_COUNTER_RETENTION_DAYS = 180

# ─── Migrations ───────────────────────────────────────────────────────────────
#
# Rules:
#   - Append-only. Never edit an existing entry.
#   - Each migration must update schema_version as its final statement.
#   - Use CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE for idempotency.
#   - SQLite ALTER TABLE only supports ADD COLUMN. Renaming or dropping
#     columns requires a create-copy-drop migration.

# Stored in password_hash for accounts authenticated by Home Assistant. Not a
# valid bcrypt hash, so verify_password can only ever return False for it —
# these accounts have no password and must not acquire one by accident.
HA_USER_PASSWORD_SENTINEL = "!ha-authenticated-no-password"

MIGRATIONS: list[str] = [
    # ── v1 — initial schema ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id         TEXT    PRIMARY KEY,
        label             TEXT,
        approved          INTEGER NOT NULL DEFAULT 0,
        ip                TEXT,
        firmware_ver      TEXT,
        firmware_previous TEXT,
        first_seen        INTEGER,
        last_seen         INTEGER,
        config            TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS device_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id  TEXT    NOT NULL,
        ts         INTEGER NOT NULL,
        level      TEXT    NOT NULL,
        source     TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        FOREIGN KEY (device_id) REFERENCES devices(device_id)
    );
    CREATE INDEX IF NOT EXISTS idx_device_logs_device_ts
        ON device_logs(device_id, ts DESC);

    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    UNIQUE NOT NULL,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'readonly',
        created_at    INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT    PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS system_config (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT OR IGNORE INTO system_config VALUES ('schema_version',        '1');
    INSERT OR IGNORE INTO system_config VALUES ('device_approval',       'strict');
    INSERT OR IGNORE INTO system_config VALUES ('session_expiry_days',   '30');
    INSERT OR IGNORE INTO system_config VALUES ('update_check_interval', '3600');
    INSERT OR IGNORE INTO system_config VALUES ('github_repo',           'wilbowes/EchoMuse');
    INSERT OR IGNORE INTO system_config VALUES ('latest_version',        NULL);
    INSERT OR IGNORE INTO system_config VALUES ('latest_binary_url',     NULL);
    INSERT OR IGNORE INTO system_config VALUES ('last_update_check',     NULL);
    """,

    # ── v2 — ESPHome native API integration ─────────────────────────────────
    """
    ALTER TABLE devices ADD COLUMN esphome_api_port INTEGER;
    ALTER TABLE devices ADD COLUMN esphome_noise_psk TEXT;

    INSERT OR IGNORE INTO system_config VALUES ('next_esphome_port', '16001');

    UPDATE system_config SET value = '2' WHERE key = 'schema_version';
    """,

    # ── v3 — Global device config and per-device override flag ───────────────
    #
    # use_global_config=1 (default): device inherits fleet-wide defaults.
    # use_global_config=0: device has its own config stored in the config column.
    #
    # global_device_config: JSON blob in system_config, same shape as
    # DEFAULT_DEVICE_CONFIG. Seeded from DEFAULT_DEVICE_CONFIG at migration time
    # so existing installs get the same values they were already using.
    f"""
    ALTER TABLE devices ADD COLUMN use_global_config INTEGER NOT NULL DEFAULT 1;

    INSERT OR IGNORE INTO system_config VALUES ('global_device_config', '{json.dumps(DEFAULT_DEVICE_CONFIG)}');

    UPDATE system_config SET value = '3' WHERE key = 'schema_version';
    """,

    # ── v4 — BLE proxy (second ESPHome device per Echo) ──────────────────────
    #
    # ble_proxy_port: TCP port of the device's bluetooth_proxy ESPHome
    # listener. Allocated lazily on first enable from the same
    # next_esphome_port counter as the voice satellite (same never-reuse
    # invariant), so voice and BT ports share one sparse range.
    """
    ALTER TABLE devices ADD COLUMN ble_proxy_port INTEGER;

    UPDATE system_config SET value = '4' WHERE key = 'schema_version';
    """,

    # ── v5 — persistent activity stats ────────────────────────────────────────
    #
    # turns: one row per voice turn (wake/button/barge-in/continuation),
    # written at turn completion from the TurnTrace in em_esphome. Survives
    # controller and device restarts — the in-memory Device.turn_history is
    # hydrated from here on connect. trigger_type not "trigger": TRIGGER is
    # an SQLite keyword. underruns/playback_periods arrive asynchronously
    # (device playback_stats message, firmware >= v2.9) and are NULL until
    # then — NULL means "not reported", 0 means "clean playback".
    #
    # wake_counters: hourly per-device rollups for signals too frequent to
    # store per-event — near-misses (wake score > 0.05 but below threshold)
    # and underruns from non-turn playback (announcements). One UPSERT at
    # most every ~2s per device (piggybacks the existing rate-limited
    # near-miss log path), so the instrumentation cost is negligible.
    #
    # device_metrics: hourly rollup of the device's ~30s hardware stats
    # report (CPU, RAM, storage, WiFi RSSI) for historic trend review.
    # Sums + extremes are upserted in place per report (averages computed
    # at read time), so an hour is one row however many samples land in it.
    # cpu_max keeps short spikes visible through the hourly average;
    # rssi_min keeps marginal-WiFi dips visible the same way.
    """
    CREATE TABLE IF NOT EXISTS turns (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id        TEXT    NOT NULL,
        ts               REAL    NOT NULL,
        trigger_type     TEXT,
        wake_model       TEXT,
        wake_score       REAL,
        wake_threshold   REAL,
        noise_floor      REAL,
        outcome          TEXT,
        stt_text         TEXT,
        total_ms         INTEGER,
        vad_end_ms       INTEGER,
        stt_ms           INTEGER,
        tts_url_ms       INTEGER,
        tts_fetch_ms     INTEGER,
        playback_ms      INTEGER,
        audio_ms         INTEGER,
        tts_bytes        INTEGER,
        underruns        INTEGER,
        playback_periods INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_turns_device_ts ON turns(device_id, ts DESC);

    CREATE TABLE IF NOT EXISTS wake_counters (
        device_id     TEXT    NOT NULL,
        hour_ts       INTEGER NOT NULL,
        near_misses   INTEGER NOT NULL DEFAULT 0,
        near_miss_max REAL    NOT NULL DEFAULT 0,
        underruns     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (device_id, hour_ts)
    );

    CREATE TABLE IF NOT EXISTS device_metrics (
        device_id        TEXT    NOT NULL,
        hour_ts          INTEGER NOT NULL,
        samples          INTEGER NOT NULL DEFAULT 0,
        cpu_sum          REAL    NOT NULL DEFAULT 0,
        cpu_max          REAL    NOT NULL DEFAULT 0,
        mem_used_sum     REAL    NOT NULL DEFAULT 0,
        mem_total_mb     REAL,
        storage_used_mb  REAL,
        storage_total_mb REAL,
        rssi_sum         REAL    NOT NULL DEFAULT 0,
        rssi_samples     INTEGER NOT NULL DEFAULT 0,
        rssi_min         REAL,
        PRIMARY KEY (device_id, hour_ts)
    );

    UPDATE system_config SET value = '5' WHERE key = 'schema_version';
    """,

    # ── v6 — device-link auth token (TLS rollout) ─────────────────────────────
    #
    # token: shared secret the device presents in the X-EM-Token header on
    # all three WebSocket planes (/control, /data, /shell). Minted by
    # ensure_device_token() when credentials are first pushed (provisioning
    # wizard or the dashboard "Secure link" action) and stored on the device
    # at /data/local/etc/echomuse/token. NULL = no credentials issued yet —
    # such devices connect unauthenticated (legacy posture) until
    # REQUIRE_DEVICE_TLS=1 flips the controller to enforcing.
    """
    ALTER TABLE devices ADD COLUMN token TEXT;

    UPDATE system_config SET value = '6' WHERE key = 'schema_version';
    """,

    # ── v7 — delivery instrumentation (playback stall diagnosis) ─────────────
    #
    # Added 2026-07-20 after playback underruns appeared with no identifiable
    # cause: every metric available at the time measured the wrong thing (the
    # controller's "Streaming took Xs" times a socket write, which completes
    # instantly regardless of how slowly the device is actually fed).
    #
    # Device-reported per stream (firmware >= v2.9.6, NULL on older):
    #   min_depth      fewest periods left in the device buffer mid-stream.
    #                  The margin metric — 0 means it starved, 2 means it
    #                  nearly did. Underruns are rare and binary; this is
    #                  continuous and shows degradation before it is audible.
    #   prime_wait_ms  first frame arriving -> first frame played.
    #   recv_span_ms   first -> last frame arrival. Compare against audio
    #                  duration: longer means delivery was slower than
    #                  realtime, i.e. the wire could not keep up.
    #   max_gap_ms     worst single inter-arrival gap (brief stall vs
    #                  uniformly slow link).
    #   bytes_recv     wire bytes the device actually received.
    #
    # Controller-measured:
    #   send_ms        time to write every frame into the socket. Kept for
    #                  contrast with delivery_ms — the gap between them is
    #                  exactly the illusion that misled the 07-20 analysis.
    #   delivery_ms    first frame sent -> device's playback_stats arrival.
    #                  The true end-to-end delivery window.
    #   eq_ms          EQ compute time (executor), to rule the controller in
    #                  or out as the source of a late start.
    """
    ALTER TABLE turns ADD COLUMN min_depth     INTEGER;
    ALTER TABLE turns ADD COLUMN prime_wait_ms INTEGER;
    ALTER TABLE turns ADD COLUMN recv_span_ms  INTEGER;
    ALTER TABLE turns ADD COLUMN max_gap_ms    INTEGER;
    ALTER TABLE turns ADD COLUMN bytes_recv    INTEGER;
    ALTER TABLE turns ADD COLUMN send_ms       INTEGER;
    ALTER TABLE turns ADD COLUMN delivery_ms   INTEGER;
    ALTER TABLE turns ADD COLUMN eq_ms         INTEGER;

    ALTER TABLE device_metrics ADD COLUMN link_speed_last  INTEGER;
    ALTER TABLE device_metrics ADD COLUMN link_speed_min   INTEGER;
    ALTER TABLE device_metrics ADD COLUMN wifi_freq_last   INTEGER;
    ALTER TABLE device_metrics ADD COLUMN wifi_bssid_last  TEXT;
    ALTER TABLE device_metrics ADD COLUMN tx_bytes_sum     INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN rx_bytes_sum     INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN tx_errors_sum    INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN tx_dropped_sum   INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN rx_crc_sum       INTEGER NOT NULL DEFAULT 0;

    UPDATE system_config SET value = '7' WHERE key = 'schema_version';
    """,

    # ── v8 — per-section fleet/device config scoping ─────────────────────────
    #
    # use_global_config was one boolean for the whole config, so overriding a
    # single setting on one device forked every other setting too and froze
    # them against future fleet changes. config_sections replaces it with the
    # set of sections a device overrides (ids from em_config_sections).
    #
    # The backfill is lossless in both directions:
    #   use_global_config = 1  ->  '[]'    (inherits everything, as before)
    #   use_global_config = 0  ->  all ids (overrides everything, as before)
    # so every device's effective config is byte-identical across the upgrade.
    #
    # use_global_config is KEPT rather than dropped: SQLite cannot drop a
    # column without a table rebuild, and it stays useful as the compat view
    # (no sections overridden == following the fleet), which older API
    # consumers and the migration itself both rely on.
    """
    ALTER TABLE devices ADD COLUMN config_sections TEXT NOT NULL DEFAULT '[]';

    UPDATE devices
       SET config_sections = '["playback","wakeword","microphones","ring","advanced","bluetooth"]'
     WHERE use_global_config = 0;

    UPDATE system_config SET value = '8' WHERE key = 'schema_version';
    """,

    # ── v9 — control-plane RTT ───────────────────────────────────────────────
    #
    # The RF layer is opaque on this hardware: the MTK driver leaves
    # retry/discard/missed-beacon at zero in /proc/net/wireless and reports
    # NOISE=9999, so tx_errors/tx_dropped/rx_crc (added in v7) are
    # STRUCTURALLY zero here and prove nothing either way. RTT measures the
    # latency that actually degrades the experience, needs no driver support,
    # and separates the hypotheses: contention makes latency track load,
    # power-save makes it spike when idle. Hence excursions are split by
    # whether the device was busy when the probe went out.
    """
    ALTER TABLE device_metrics ADD COLUMN rtt_sum_ms          INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN rtt_samples         INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN rtt_min_ms          INTEGER;
    ALTER TABLE device_metrics ADD COLUMN rtt_max_ms          INTEGER;
    ALTER TABLE device_metrics ADD COLUMN rtt_excursions      INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN rtt_excursions_idle INTEGER NOT NULL DEFAULT 0;

    UPDATE system_config SET value = '9' WHERE key = 'schema_version';
    """,

    # ── v10 — RTT idle-sample denominator ───────────────────────────────────
    #
    # Added immediately after v9 because v9 shipped without it and the
    # excursion split is meaningless without a denominator: "every excursion
    # happened while idle" is vacuous when almost every SAMPLE is idle.
    #
    # This is its own migration rather than an edit to v9 for the reason
    # stated at the top of this list — migrations are APPEND-ONLY. Editing v9
    # after a DB had already reached schema_version 9 meant the column was
    # never created, and every stats report then failed with "no column named
    # rtt_samples_idle", disconnecting all three devices in a loop
    # (2026-07-25, caught within a minute by the post-deploy error check).
    """
    ALTER TABLE device_metrics ADD COLUMN rtt_samples_idle INTEGER NOT NULL DEFAULT 0;

    UPDATE system_config SET value = '10' WHERE key = 'schema_version';
    """,

    # ── v11 — prune configs to match their scoping ──────────────────────────
    #
    # v8 backfilled config_sections but left the config column alone, so a
    # device that had been fully inheriting still stored a value for every
    # key. Harmless to the DEVICE (get_effective_device_config only reads
    # in-scope keys) but not to the dashboard, which merged the stored dict
    # over the fleet config and so displayed a device's stale settings while
    # every section claimed to be following the fleet — Office showed
    # hey_rhasspy/standard while actually running hey_mycroft/malevolent.
    #
    # The display bug is fixed properly in dashboard.jsx (filter, don't
    # merge). This makes the stored state honest as well, so the invariant
    # asserted in set_device_config_sections holds for migrated rows too.
    # Marked as a no-op SQL statement; the real work runs in Python below,
    # because pruning needs the section map.
    """
    UPDATE system_config SET value = '11' WHERE key = 'schema_version';
    """,

    # ── v12 — utterance recordings ──────────────────────────────────────────
    #
    # Filename (not a blob) of the saved mic audio for this turn. The WAV
    # itself lives in recordings/ beside this DB and is retained by file
    # count per device (em_recordings.KEEP_PER_DEVICE), which is a much
    # shorter window than TURN_RETENTION — so a non-NULL audio_file on an
    # older row is a claim to CHECK, not to trust. Every reader resolves
    # through em_recordings and treats a missing file as "no recording".
    """
    ALTER TABLE turns ADD COLUMN audio_file TEXT;

    UPDATE system_config SET value = '12' WHERE key = 'schema_version';
    """,

    # ── v13 — on-device wake word shadow mode ───────────────────────────────
    #
    # Two levels, because they answer different questions.
    #
    # Per turn (dev_wake_*): when the CONTROLLER woke, did the device agree,
    # and how far apart were they? This is the comparison that decides whether
    # on-device detection is good enough to trust. NULL means one of three
    # things and they are not the same: shadow mode was off, the device's
    # firmware predates it, or the device genuinely did not cross the threshold
    # for this utterance. The third is a finding; the first two are absence of
    # data. dev_shadow distinguishes them — 1 when the device was known to be
    # scoring at the time, so a NULL score alongside it is a real miss.
    #
    # Per hour (wake_counters.dev_*): what did the device see when the
    # controller did NOT wake? Crossings with no corresponding turn are the
    # false-accept side of the comparison, which per-turn rows structurally
    # cannot show. dev_frames is the denominator that makes the rest legible,
    # and dev_drops says whether the device kept up at all — a shadow run that
    # dropped half its frames is not evidence about detection quality.
    """
    ALTER TABLE turns ADD COLUMN dev_wake_score REAL;
    ALTER TABLE turns ADD COLUMN dev_wake_delta_ms INTEGER;
    ALTER TABLE turns ADD COLUMN dev_shadow INTEGER;

    ALTER TABLE wake_counters ADD COLUMN dev_frames    INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE wake_counters ADD COLUMN dev_drops     INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE wake_counters ADD COLUMN dev_crossings INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE wake_counters ADD COLUMN dev_max_score REAL    NOT NULL DEFAULT 0;

    UPDATE system_config SET value = '13' WHERE key = 'schema_version';
    """,

    # ── v14 — thermals and CPU topology ─────────────────────────────────────
    #
    # cpu_temp_* / max_temp_max: this SoC has 11 thermal zones; mtktscpu is the
    # CPU and the max-of-all catches a PMIC or board sensor running hotter than
    # the CPU, which is where trouble shows up first.
    #
    # cores_online_* exists because cpu_pct is NOT self-describing: it comes
    # from the aggregate /proc/stat line, so it is a share of ONLINE capacity,
    # and MTK parks 3 of 4 cores when idle. The same absolute work reads as half
    # the percentage once a second core comes up — so a cpu_pct series without
    # the core count beside it can show a "drop" that is purely a change of
    # divisor. cores_online_min is the interesting end: it is the tightest the
    # device ever was.
    #
    # thermal_limit_min < cores_total means the thermal governor capped capacity
    # at some point in the hour, which is the throttling signal that matters
    # more than any single temperature.
    """
    ALTER TABLE device_metrics ADD COLUMN cpu_temp_sum     REAL    NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN cpu_temp_samples INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE device_metrics ADD COLUMN cpu_temp_max     REAL;
    ALTER TABLE device_metrics ADD COLUMN max_temp_max     REAL;
    ALTER TABLE device_metrics ADD COLUMN cores_online_last INTEGER;
    ALTER TABLE device_metrics ADD COLUMN cores_online_min  INTEGER;
    ALTER TABLE device_metrics ADD COLUMN cores_total       INTEGER;
    ALTER TABLE device_metrics ADD COLUMN thermal_limit_min INTEGER;

    UPDATE system_config SET value = '14' WHERE key = 'schema_version';
    """,

    # ── v15 — the device's own wake threshold, per turn ─────────────────────
    #
    # Needed to tell a real on-device miss from a comparison that was never
    # valid. The controller lowers its wake bar to bargeInThreshold while the
    # speaker is playing (echo at the mic is ~25dB louder than the person), so
    # a turn that fired at 0.055 was not something a device scoring against
    # 0.5 could have caught. Before this, those were counted as misses and the
    # agreement figure was pessimistic.
    #
    # NULL means the device did not report a threshold (firmware predating it),
    # in which case the turn is counted as neither agreed nor missed rather
    # than guessed at.
    """
    ALTER TABLE turns ADD COLUMN dev_threshold REAL;

    UPDATE system_config SET value = '15' WHERE key = 'schema_version';
    """,

    # ── v16 — what the shadow scorer's stalls actually were ─────────────────
    #
    # dev_drops says the scorer's 8-frame (640ms) queue overflowed. It does
    # not say WHY, and three explanations have already been falsified by
    # measurement (turn bursts, core hotplug, controller redeploys), so the
    # counter on its own has stopped being able to answer the question.
    #
    # A drop has exactly two causes and they call for opposite fixes:
    # dev_max_infer_ms is the slowest single inference in the window (the
    # CONSUMER stalling), dev_max_gap_ms the longest gap between frames
    # arriving (the PRODUCER bursting — the mic pipeline handing over its
    # 160ms batches late and in clumps).
    #
    # Maxima rather than averages, because a stall IS the tail: a 700ms event
    # averaged over a 30s window of 375 normal frames disappears entirely.
    # Default 0 reads as "not reported" for firmware predating this, which is
    # honest here — unlike a measurement, an absent maximum cannot be mistaken
    # for a good one, since 0 is also what a perfectly healthy window reports.
    """
    ALTER TABLE wake_counters ADD COLUMN dev_max_infer_ms INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE wake_counters ADD COLUMN dev_max_gap_ms   INTEGER NOT NULL DEFAULT 0;

    UPDATE system_config SET value = '16' WHERE key = 'schema_version';
    """,

    # ── v17 — the comparison, from the other side ───────────────────────────
    #
    # With owwOnDevice="on" the DEVICE triggers the turn, so dev_wake_score is
    # no longer the side that can be missing — this controller's is. These
    # record whether it agreed, matched the same way and against the same
    # clock, so the agreement figure that justified shipping on-device wake
    # keeps being answerable once the roles are swapped.
    #
    # Without them, turning a device "on" would silently end the measurement:
    # every turn would show a device score and nothing to compare it against,
    # which reads as perfect agreement rather than as no data.
    #
    # NULL means the controller did not detect this utterance within the match
    # window — a controller miss, which is the interesting direction and the
    # one that argues on-device wake is worth having. NULL on a
    # controller-triggered turn just means the column does not apply.
    """
    ALTER TABLE turns ADD COLUMN ctrl_wake_score REAL;
    ALTER TABLE turns ADD COLUMN ctrl_wake_delta_ms INTEGER;

    UPDATE system_config SET value = '17' WHERE key = 'schema_version';
    """,

    # ── v18 — Home Assistant identity on a user row ──────────────────────────
    #
    # Under the add-on, Home Assistant has already authenticated the person,
    # so the dashboard authenticates them from Supervisor's forwarded user id
    # rather than asking for a second password (em_ingressauth).
    #
    # Keyed on the HA user id, never the name: names are editable in Home
    # Assistant, and keying on one would silently hand a renamed user a fresh
    # account — or worse, hand somebody else's account to whoever took the
    # old name.
    #
    # NULL for every ordinary account, which is what the standalone container
    # has and keeps having. UNIQUE tolerates repeated NULLs in SQLite, so the
    # constraint costs local accounts nothing.
    """
    ALTER TABLE users ADD COLUMN ha_user_id TEXT;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ha_user_id
        ON users(ha_user_id) WHERE ha_user_id IS NOT NULL;

    UPDATE system_config SET value = '18' WHERE key = 'schema_version';
    """,
]

# Post-migration fixups that need Python rather than SQL. Keyed by the schema
# version they belong to; run once, immediately after that migration applies.
def _fixup_v11(conn) -> None:
    rows = conn.execute("SELECT device_id, config, config_sections FROM devices").fetchall()
    for row in rows:
        try:
            cfg  = json.loads(row["config"] or "{}") or {}
            secs = em_config_sections.normalise(json.loads(row["config_sections"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        keep = em_config_sections.keys_for(secs) | em_config_sections.STATE_KEYS
        pruned = {k: v for k, v in cfg.items() if k in keep}
        if len(pruned) != len(cfg):
            conn.execute(
                "UPDATE devices SET config = ? WHERE device_id = ?",
                (json.dumps(pruned), row["device_id"]),
            )
            log.info(
                f"[db] v11: pruned {len(cfg) - len(pruned)} out-of-scope config "
                f"key(s) from {row['device_id']}"
            )


_MIGRATION_FIXUPS = {11: _fixup_v11}

# ─── Connection management ────────────────────────────────────────────────────

_db_path: str = ""
_conn: Optional[sqlite3.Connection] = None

# We share ONE sqlite3.Connection across the run_in_executor thread pool
# (check_same_thread=False). WAL allows concurrent readers only across
# *separate* connections — on a single shared connection object, ANY two
# concurrent operations (read-vs-write or write-vs-write) are a use-from-two-
# threads misuse that SQLite rejects with SQLITE_MISUSE ("bad parameter or
# other API misuse"). So every access — reads (_q/_q1) AND write transactions
# (_tx) — must serialise through this one lock. (Guarding only writes was a
# latent bug: a read racing a write-commit crashed concurrent OTAs during a
# fleet deploy-all — 2026-07-12.)
_db_lock = threading.Lock()


def init(path: str = "echomuse.db") -> None:
    """
    Initialise the database. Must be called once at startup before any
    other db function is used.

    Opens a single persistent connection, enables WAL mode and foreign
    key enforcement, then runs any pending migrations.
    """
    global _db_path, _conn
    _db_path = path
    log.info(f"Opening database: {os.path.abspath(path)}")
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.commit()
    _migrate(_conn)
    log.info("Database ready")


@contextmanager
def _tx():
    """
    Context manager for a write transaction.

    Commits on clean exit, rolls back on any exception and re-raises.
    Holds _db_lock for the whole transaction so no read or other write can
    touch the shared connection mid-transaction (see _db_lock).
    """
    assert _conn is not None, "db.init() has not been called"
    with _db_lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Execute a read query and return all rows."""
    assert _conn is not None, "db.init() has not been called"
    with _db_lock:
        return _conn.execute(sql, params).fetchall()


def _q1(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Execute a read query and return at most one row."""
    assert _conn is not None, "db.init() has not been called"
    with _db_lock:
        return _conn.execute(sql, params).fetchone()


# ─── Migrations ───────────────────────────────────────────────────────────────

def _backup_before_migrating(conn: sqlite3.Connection, current: int) -> None:
    """
    Copy the database before any migration touches it.

    Migrations run in place and every one so far is additive, which is about
    as safe as schema change gets — but a controller can be several versions
    behind (the version is just an index into an append-only list, so a v11
    database migrates to v16 in one startup), and the first migration that
    rewrites or drops data has no undo without this.

    Uses sqlite3's own backup API rather than copying the file: it is
    consistent against an open connection and correct under WAL, where the
    .db file alone is not the whole database.

    Named for the version being LEFT, not the time — a retry after a failed
    migration starts from the same state, so it should overwrite rather than
    accumulate a backup per attempt. One file per starting version is enough
    to get back to where you were, and cannot fill a disk on its own.

    A backup that cannot be written is a refusal to migrate, not a warning.
    Disk-full is precisely when you want the schema left alone, and a warning
    logged at startup is a warning nobody reads until afterwards. Set
    EM_SKIP_DB_BACKUP=1 to proceed anyway.
    """
    if current == 0:
        return  # fresh database — nothing to preserve

    if os.environ.get("EM_SKIP_DB_BACKUP") == "1":
        log.warning("[db] EM_SKIP_DB_BACKUP=1 — migrating without a backup")
        return

    dest = f"{_db_path}.pre-v{current}.bak"
    try:
        with sqlite3.connect(dest) as bck:
            conn.backup(bck)
        size = os.path.getsize(dest)
        log.info(f"[db] Backed up v{current} schema to {dest} ({size/1024:.0f}KB)")
    except Exception as e:
        raise RuntimeError(
            f"Could not back up the database before migrating from v{current} "
            f"({e}). Refusing to migrate: free some space or fix permissions "
            f"on {os.path.dirname(_db_path) or '.'}, or set EM_SKIP_DB_BACKUP=1 "
            f"to proceed without one."
        ) from e


def _migrate(conn: sqlite3.Connection) -> None:
    """Run any pending migrations in order."""
    # Determine current schema version. On a brand-new database the
    # system_config table doesn't exist yet, so we catch that case.
    try:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0
    except sqlite3.OperationalError:
        current = 0  # fresh database — system_config doesn't exist yet

    # ── Downgrade guard ──────────────────────────────────────────────────────
    #
    # A database from a NEWER controller. MIGRATIONS[current:] is empty here,
    # so without this the old build starts silently and runs against a schema
    # it does not know. That mostly works — the newer schema is a superset —
    # but "mostly" is the problem: the failure surfaces as odd behaviour
    # somewhere else rather than as an error here, and it happens exactly when
    # someone has rolled an image back and is already troubleshooting.
    #
    # Refusing costs a clear message; proceeding costs an afternoon.
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"Database is at schema v{current} but this controller only knows "
            f"v{len(MIGRATIONS)} — it was created by a newer version. Use a "
            f"controller at least that new, or restore a backup taken before "
            f"the upgrade (see {os.path.basename(_db_path)}.pre-v*.bak beside "
            f"the database)."
        )

    pending = MIGRATIONS[current:]
    if not pending:
        log.debug(f"Schema is current at v{current}")
        return

    _backup_before_migrating(conn, current)

    log.info(f"Running {len(pending)} migration(s) from v{current}")
    for i, sql in enumerate(pending):
        version = current + i + 1
        log.info(f"Applying migration v{version}")
        try:
            conn.executescript(sql)
            # Some migrations need data work that SQL cannot express (e.g.
            # pruning JSON config against the section map). Runs inside the
            # same transaction as its DDL so a failure rolls both back.
            fixup = _MIGRATION_FIXUPS.get(version)
            if fixup is not None:
                fixup(conn)
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Migration v{version} failed: {e}")
            raise RuntimeError(f"Database migration v{version} failed — cannot start") from e

    new_version = current + len(pending)
    log.info(f"Schema migrated to v{new_version}")


# ─── Device registry ──────────────────────────────────────────────────────────

def get_device(device_id: str) -> Optional[sqlite3.Row]:
    """
    Return the device row for device_id, or None if not registered.

    Row fields: device_id, label, approved, ip, firmware_ver,
                firmware_previous, first_seen, last_seen, config (JSON str)
    """
    return _q1(
        "SELECT * FROM devices WHERE device_id = ?",
        (device_id,),
    )


def get_all_devices() -> list[sqlite3.Row]:
    """Return all device rows ordered by first_seen."""
    return _q("SELECT * FROM devices ORDER BY first_seen ASC")


def get_pending_devices() -> list[sqlite3.Row]:
    """Return devices that have connected but not yet been approved."""
    return _q(
        "SELECT * FROM devices WHERE approved = 0 ORDER BY first_seen ASC"
    )


def register_new_device(device_id: str, ip: str, version: Optional[str]) -> None:
    """
    Insert a new device row with approved=0 (pending).

    Called when an unknown device_id connects for the first time.
    Config is seeded from DEFAULT_DEVICE_CONFIG.
    """
    now = _now()
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO devices
                (device_id, label, approved, ip, firmware_ver, first_seen, last_seen, config)
            VALUES (?, NULL, 0, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                ip,
                version,
                now,
                now,
                json.dumps(DEFAULT_DEVICE_CONFIG),
            ),
        )
    log.info(f"[db] New device registered (pending): {device_id}")


def approve_device(device_id: str, label: str, config: Optional[dict] = None) -> None:
    """
    Approve a pending device and optionally set label and config.

    If config is not supplied the existing seeded config is kept.
    Raises ValueError if the device is not found.
    """
    device = get_device(device_id)
    if device is None:
        raise ValueError(f"Device not found: {device_id}")

    effective_config = json.dumps(config) if config is not None else device["config"]

    with _tx() as conn:
        conn.execute(
            """
            UPDATE devices
            SET approved = 1,
                label    = ?,
                config   = ?
            WHERE device_id = ?
            """,
            (label, effective_config, device_id),
        )
    log.info(f"[db] Device approved: {device_id} label={label!r}")


def upsert_device_seen(
    device_id: str,
    ip: str,
    version: Optional[str],
) -> None:
    """
    Update ip, firmware_ver, and last_seen for a known device on each connection.

    Does not touch approval status, label, or config.
    """
    with _tx() as conn:
        conn.execute(
            """
            UPDATE devices
            SET ip           = ?,
                firmware_ver = ?,
                last_seen    = ?
            WHERE device_id = ?
            """,
            (ip, version, _now(), device_id),
        )


def touch_device_seen(device_id: str) -> None:
    """
    Refresh last_seen only — the device is alive right now.

    upsert_device_seen fires on connect, which made last_seen mean "last
    CONNECTED": a device online and healthy for hours still displayed a
    last_seen from whenever it last reconnected (observed 2026-07-25:
    all three devices reading 88-91 minutes stale while streaming audio
    fine). That is precisely backwards from what the field is for —
    knowing when a device went away.

    Called from the ~30s stats report, so last_seen is at most one report
    stale while connected and lands within ~30s of the truth when a device
    drops. It rides the same executor hop as record_device_stats, so it
    adds no write the loop wasn't already making.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET last_seen = ? WHERE device_id = ?",
            (_now(), device_id),
        )


def get_device_token(device_id: str) -> Optional[str]:
    """Return the device's link-auth token, or None if never issued."""
    row = _q1("SELECT token FROM devices WHERE device_id = ?", (device_id,))
    return row["token"] if row and row["token"] else None


def ensure_device_token(device_id: str) -> str:
    """
    Return the device's link-auth token, minting one if absent.

    Creates a pending device row first if the device has never connected —
    the provisioning wizard pushes credentials before first contact, so the
    row may not exist yet. Approval state is untouched either way.
    """
    import secrets

    existing = get_device_token(device_id)
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    now = _now()
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO devices
                (device_id, label, approved, ip, firmware_ver, first_seen, last_seen, config)
            VALUES (?, NULL, 0, NULL, NULL, ?, ?, ?)
            ON CONFLICT(device_id) DO NOTHING
            """,
            (device_id, now, now, json.dumps(DEFAULT_DEVICE_CONFIG)),
        )
        conn.execute(
            "UPDATE devices SET token = ? WHERE device_id = ?",
            (token, device_id),
        )
    log.info(f"[db] Link token minted for {device_id}")
    return token


def clear_device_token(device_id: str) -> None:
    """Revoke a device's link token (next credential push mints a new one)."""
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET token = NULL WHERE device_id = ?", (device_id,)
        )


def set_device_label(device_id: str, label: str) -> None:
    """Update the human-readable label for a device."""
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET label = ? WHERE device_id = ?",
            (label, device_id),
        )


def set_device_config(device_id: str, config: dict) -> None:
    """
    Persist updated config for a device.

    The caller is responsible for immediately pushing the config to the
    live device over the control WebSocket if it is currently connected.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET config = ? WHERE device_id = ?",
            (json.dumps(config), device_id),
        )


def get_device_config(device_id: str) -> dict:
    """
    Return the config dict for a device.

    Falls back to DEFAULT_DEVICE_CONFIG if the device is not found or
    config is empty/invalid — this should not normally happen.
    """
    row = _q1("SELECT config FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return dict(DEFAULT_DEVICE_CONFIG)
    try:
        return json.loads(row["config"]) or dict(DEFAULT_DEVICE_CONFIG)
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config JSON for {device_id} — using defaults")
        return dict(DEFAULT_DEVICE_CONFIG)


def get_global_device_config() -> dict:
    """
    Return the fleet-wide default device config.

    Falls back to DEFAULT_DEVICE_CONFIG if the key is missing or unparseable
    (should only occur on a fresh DB before migration v3 has run).
    """
    row = _q1("SELECT value FROM system_config WHERE key = 'global_device_config'")
    if row is None or not row["value"]:
        return dict(DEFAULT_DEVICE_CONFIG)
    try:
        stored = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        log.warning("[db] Invalid global_device_config JSON — using defaults")
        return dict(DEFAULT_DEVICE_CONFIG)
    if not stored:
        return dict(DEFAULT_DEVICE_CONFIG)
    # Underlay defaults so keys added after the stored config was last
    # saved (e.g. micGainDb) are still pushed with their default value
    # instead of silently falling back to whatever the device binary's
    # env default happens to be.
    return {**DEFAULT_DEVICE_CONFIG, **stored}


def get_global_device_config_raw() -> dict:
    """
    The stored fleet config with defaults NOT underlaid — i.e. exactly the
    keys an operator has persisted.

    Used by the config-clobber guard (em_api._dropped_keys). The guard must
    compare against what is really stored: if it compared against the
    defaults-underlaid view, a controller upgrade that introduces a new
    default key would make every save from an already-open dashboard tab
    look like it was deleting that key, and refuse a perfectly legitimate
    write. Returns {} when nothing has been saved yet.
    """
    row = _q1("SELECT value FROM system_config WHERE key = 'global_device_config'")
    if row is None or not row["value"]:
        return {}
    try:
        return json.loads(row["value"]) or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def set_global_device_config(config: dict) -> None:
    """Persist updated fleet-wide default device config."""
    with _tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value) VALUES ('global_device_config', ?)",
            (json.dumps(config),),
        )


def get_device_config_sections(device_id: str) -> list:
    """The section ids this device overrides. Empty list = follows the fleet."""
    row = _q1("SELECT config_sections FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return []
    try:
        return em_config_sections.normalise(json.loads(row["config_sections"]) or [])
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config_sections for {device_id} — treating as fleet")
        return []


def set_device_config_sections(device_id: str, section_ids) -> list:
    """
    Set which sections this device overrides, and drop the stored values for
    any section it no longer does.

    Discarding on revert is deliberate and matches the pre-v8 behaviour of
    set_device_use_global (which reset the whole config to a copy of global):
    a section that follows the fleet should hold no stale shadow values that
    silently reappear if it is toggled back months later.

    use_global_config is kept in step as the compat view for older readers.
    """
    sections = em_config_sections.normalise(section_ids)
    kept = em_config_sections.keys_for(sections) | em_config_sections.STATE_KEYS
    stored = get_device_config(device_id)
    pruned = {k: v for k, v in stored.items() if k in kept}
    with _tx() as conn:
        conn.execute(
            """
            UPDATE devices
               SET config_sections   = ?,
                   config            = ?,
                   use_global_config = ?
             WHERE device_id = ?
            """,
            (json.dumps(sections), json.dumps(pruned),
             0 if sections else 1, device_id),
        )
    return sections


def get_effective_device_config(device_id: str) -> dict:
    """
    Return the config that should be pushed to a device.

    Fleet config, with this device's own values layered over it for whichever
    sections it overrides (see em_config_sections). No overridden sections =
    pure fleet config. Every section overridden = fully device-specific, which
    is what the pre-v8 use_global_config=0 meant.

    STATE_KEYS (startupVolume) always come from the device when present,
    regardless of scoping — that is this device's own hardware state, and
    inheriting it from the fleet would bring a device back at another room's
    volume.

    This is the authoritative source for what config a device should
    actually run — use it in device_connected() and any config-push path.
    """
    row = _q1(
        "SELECT config_sections, config FROM devices WHERE device_id = ?",
        (device_id,),
    )
    if row is None:
        return get_global_device_config()

    try:
        per_device = json.loads(row["config"]) or {}
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config JSON for {device_id} — using global")
        per_device = {}

    try:
        sections = em_config_sections.normalise(
            json.loads(row["config_sections"]) or []
        )
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config_sections for {device_id} — using global")
        sections = []

    return em_config_sections.merge(
        get_global_device_config(), per_device, sections
    )


def set_device_use_global(device_id: str, enabled: bool) -> None:
    """
    Set the use_global_config flag for a device.

    When enabling (reverting to global): also resets the device's own
    config column to a copy of the current global config, so the stored
    value stays coherent if the flag is toggled again later.

    When disabling (enabling per-device override): the config column is
    left as-is; the caller is expected to immediately follow with
    set_device_config() to write the desired override values.
    """
    with _tx() as conn:
        if enabled:
            global_cfg = get_global_device_config()
            conn.execute(
                "UPDATE devices SET use_global_config = 1, config = ? WHERE device_id = ?",
                (json.dumps(global_cfg), device_id),
            )
        else:
            conn.execute(
                "UPDATE devices SET use_global_config = 0 WHERE device_id = ?",
                (device_id,),
            )
    log.info(f"[db] use_global_config={'1' if enabled else '0'}: {device_id}")


def set_firmware_previous(device_id: str, version: Optional[str]) -> None:
    """
    Record the version that server.old holds after an OTA update.

    Set to the old running version before the update is applied.
    Set to None once server.old is pruned or a rollback completes.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET firmware_previous = ? WHERE device_id = ?",
            (version, device_id),
        )


def delete_device(device_id: str) -> None:
    """
    Remove a device and all its logs from the registry.

    This is a hard delete — use with care. Logs are removed first to
    satisfy the foreign key constraint.

    Saved utterance recordings live on disk rather than in the DB, so no
    cascade reaches them — they are unlinked explicitly here. Leaving a
    deleted device's speech behind on the volume is the one leftover that
    actually matters.
    """
    with _tx() as conn:
        conn.execute("DELETE FROM device_logs WHERE device_id = ?", (device_id,))
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    try:
        removed = em_recordings.delete_device(device_id)
        if removed:
            log.info(f"[db] Removed {removed} recording(s) for {device_id}")
    except Exception as e:
        log.warning(f"[db] Recording cleanup failed for {device_id}: {e}")
    log.info(f"[db] Device deleted: {device_id}")


# ─── ESPHome port allocation ──────────────────────────────────────────────────

def get_esphome_port(device_id: str) -> Optional[int]:
    """
    Return the ESPHome API port assigned to this device, or None if unassigned.

    A None return means the device has never been assigned a port in esphome
    mode — call assign_esphome_port() to allocate one.
    """
    row = _q1("SELECT esphome_api_port FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return None
    return row["esphome_api_port"]  # may be None (unassigned)


def assign_esphome_port(device_id: str) -> int:
    """
    Allocate and persist an ESPHome API port for this device.

    Takes the next available port from next_esphome_port in system_config,
    increments the counter, persists both atomically, and returns the
    allocated port.

    Port allocation is monotonically increasing and never reuses freed ports
    (see ESPHOME_SPEC.md §2.2 for the rationale — sparse range is intentional
    to prevent silent misrouting if HA still holds a stale config entry for a
    deprovisioned device's old port number).

    Raises ValueError if the device is not found.
    Raises RuntimeError if a port is already assigned — caller should use
    get_esphome_port() first to check.
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT esphome_api_port FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Device not found: {device_id}")
        if row["esphome_api_port"] is not None:
            raise RuntimeError(
                f"Device {device_id} already has ESPHome port {row['esphome_api_port']} — "
                f"use get_esphome_port() to retrieve it"
            )

        next_row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'next_esphome_port'"
        ).fetchone()
        port = int(next_row["value"])

        conn.execute(
            "UPDATE devices SET esphome_api_port = ? WHERE device_id = ?",
            (port, device_id),
        )
        conn.execute(
            "UPDATE system_config SET value = ? WHERE key = 'next_esphome_port'",
            (str(port + 1),),
        )

    log.info(f"[db] ESPHome port assigned: {device_id} → {port}")
    return port


def free_esphome_port(device_id: str) -> None:
    """
    Clear the ESPHome API port assignment for a device.

    Called on device deprovisioning. The freed port number is NOT returned
    to the pool — next_esphome_port only ever increments (see assign_esphome_port).
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET esphome_api_port = NULL WHERE device_id = ?",
            (device_id,),
        )
    log.info(f"[db] ESPHome port freed: {device_id}")


# ─── BLE proxy port allocation ────────────────────────────────────────────────

# BLE proxy ports are aligned to the voice satellite port, not drawn from a
# separate pool: ble_proxy_port = esphome_api_port + BLE_PORT_OFFSET. This
# keeps the two ports for one device visibly paired (16001 voice / 17001 BT)
# and needs no allocator. The offset comfortably exceeds any realistic device
# count, so the voice range (16001+) and BT range (17001+) never overlap.
BLE_PORT_OFFSET = 1000


def get_ble_proxy_port(device_id: str) -> Optional[int]:
    """Return the BLE proxy port for this device, or None if unassigned."""
    row = _q1("SELECT ble_proxy_port FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return None
    return row["ble_proxy_port"]  # may be None (unassigned)


def ensure_ble_proxy_port(device_id: str) -> Optional[int]:
    """
    Return this device's BLE proxy port (esphome_api_port + BLE_PORT_OFFSET),
    persisting it on first call. Returns None if the device has no voice port
    yet (BLE proxy can't exist without the voice satellite it's paired to).
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT esphome_api_port, ble_proxy_port FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None or row["esphome_api_port"] is None:
            return None
        port = int(row["esphome_api_port"]) + BLE_PORT_OFFSET
        if row["ble_proxy_port"] != port:
            conn.execute(
                "UPDATE devices SET ble_proxy_port = ? WHERE device_id = ?",
                (port, device_id),
            )
            log.info(f"[db] BLE proxy port set: {device_id} → {port}")
    return port


def free_ble_proxy_port(device_id: str) -> None:
    """Clear the BLE proxy port assignment (deprovisioning)."""
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET ble_proxy_port = NULL WHERE device_id = ?",
            (device_id,),
        )
    log.info(f"[db] BLE proxy port freed: {device_id}")


# ─── Device logs ──────────────────────────────────────────────────────────────

def log_device(
    device_id: str,
    level: str,
    source: str,
    message: str,
) -> None:
    """
    Append a log entry for a device and prune old entries.

    level:  'info' | 'warn' | 'error'
    source: 'device' | 'controller'

    Pruning keeps the most recent LOG_RETENTION rows per device.
    The extra DELETE is cheap on SQLite at this row count.
    """
    now_ms = int(time.time() * 1000)
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO device_logs (device_id, ts, level, source, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, now_ms, level, source, message),
        )
        # Prune: delete all but the most recent LOG_RETENTION rows for this device.
        conn.execute(
            """
            DELETE FROM device_logs
            WHERE device_id = ?
              AND id NOT IN (
                  SELECT id FROM device_logs
                  WHERE device_id = ?
                  ORDER BY ts DESC
                  LIMIT ?
              )
            """,
            (device_id, device_id, LOG_RETENTION),
        )


def get_device_logs(
    device_id: str,
    limit: int = 100,
    before_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    """
    Return log entries for a device in reverse-chronological order.

    limit:     maximum rows to return (capped at 1000)
    before_ts: if set, return only entries with ts < before_ts
               (cursor-based pagination — pass the ts of the last row
               from the previous page)
    """
    limit = min(limit, 1000)
    if before_ts is not None:
        return _q(
            """
            SELECT * FROM device_logs
            WHERE device_id = ? AND ts < ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (device_id, before_ts, limit),
        )
    return _q(
        """
        SELECT * FROM device_logs
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, limit),
    )


# ─── Activity stats ───────────────────────────────────────────────────────────
#
# Persistent per-turn records + hourly rollups (near-misses, hardware
# metrics). Written from the voice pipeline via run_in_executor; read by the
# /api/devices/{id}/turns and /api/devices/{id}/activity endpoints.

# Turn dict keys ↔ column names. "trigger" is the dict key used everywhere
# in the pipeline and API (matches the pre-persistence turn_record shape);
# the column is trigger_type because TRIGGER is an SQLite keyword.
_TURN_COLUMNS = {
    "trigger":          "trigger_type",
    "wake_model":       "wake_model",
    "wake_score":       "wake_score",
    "wake_threshold":   "wake_threshold",
    "noise_floor":      "noise_floor",
    "outcome":          "outcome",
    "stt_text":         "stt_text",
    "total_ms":         "total_ms",
    "vad_end_ms":       "vad_end_ms",
    "stt_ms":           "stt_ms",
    "tts_url_ms":       "tts_url_ms",
    "tts_fetch_ms":     "tts_fetch_ms",
    "playback_ms":      "playback_ms",
    "audio_ms":         "audio_ms",
    "tts_bytes":        "tts_bytes",
    "underruns":        "underruns",
    "playback_periods": "playback_periods",
    # v7 delivery instrumentation — present only when the device's
    # playback_stats arrived before the turn row was written (the common
    # case: its buffer drains ahead of our overestimated drain sleep).
    "min_depth":        "min_depth",
    "prime_wait_ms":    "prime_wait_ms",
    "recv_span_ms":     "recv_span_ms",
    "max_gap_ms":       "max_gap_ms",
    "bytes_recv":       "bytes_recv",
    "send_ms":          "send_ms",
    "delivery_ms":      "delivery_ms",
    "eq_ms":            "eq_ms",
    # v13 — on-device shadow comparison. dev_shadow is the "was the device
    # scoring at all" flag that stops a NULL dev_wake_score from being read as
    # a miss when it is really absence of data.
    "dev_wake_score":    "dev_wake_score",
    "dev_wake_delta_ms": "dev_wake_delta_ms",
    "dev_shadow":        "dev_shadow",
    # v15 — the threshold the device was scoring against, so a non-crossing can
    # be judged rather than assumed to be a miss.
    "dev_threshold":     "dev_threshold",
    # v17 — the inverse comparison, populated only on device-triggered turns:
    # did THIS controller detect the same utterance, and how far apart.
    "ctrl_wake_score":    "ctrl_wake_score",
    "ctrl_wake_delta_ms": "ctrl_wake_delta_ms",
    # v12 — filename of the saved utterance WAV, written by set_turn_audio
    # after the insert (the name is keyed on the rowid). Always NULL at
    # insert time; listed here so get_turns returns it.
    "audio_file":       "audio_file",
}


def _py(v):
    """
    Coerce numpy scalars to Python natives. The voice pipeline handles
    numpy float32 scores; sqlite3 silently stores those as a 4-byte BLOB,
    which poisons the row for JSON serialisation and SQL MAX() comparisons
    (bit us 2026-07-14). Anything with .item() is a numpy scalar.
    """
    return v.item() if hasattr(v, "item") else v


def insert_turn(device_id: str, rec: dict) -> int:
    """
    Persist one completed voice turn. rec is the turn_record dict built in
    em_esphome (missing keys stored as NULL). Returns the new rowid so a
    late playback_stats report can attach to this turn. Prunes to
    TURN_RETENTION rows per device.
    """
    cols   = ["device_id", "ts"] + list(_TURN_COLUMNS.values())
    values = [device_id, _py(rec.get("ts", time.time()))] + [
        _py(rec.get(k)) for k in _TURN_COLUMNS
    ]
    with _tx() as conn:
        cur = conn.execute(
            f"INSERT INTO turns ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            values,
        )
        conn.execute(
            """
            DELETE FROM turns
            WHERE device_id = ?
              AND id NOT IN (
                  SELECT id FROM turns
                  WHERE device_id = ?
                  ORDER BY ts DESC
                  LIMIT ?
              )
            """,
            (device_id, device_id, TURN_RETENTION),
        )
        return cur.lastrowid


def set_turn_playback(turn_id: int, periods: int, underruns: int,
                      stats: Optional[dict] = None) -> None:
    """
    Attach the device's playback_stats report to a persisted turn.

    stats carries the v7 delivery-margin fields when the firmware sends them
    (>= v2.9.6); older firmware reports only periods/underruns and the extra
    columns stay NULL, which reads as "never reported" rather than zero.
    """
    stats = stats or {}
    with _tx() as conn:
        conn.execute(
            "UPDATE turns SET playback_periods = ?, underruns = ?, "
            "min_depth = ?, prime_wait_ms = ?, recv_span_ms = ?, "
            "max_gap_ms = ?, bytes_recv = ? WHERE id = ?",
            (
                periods, underruns,
                stats.get("minDepth"), stats.get("primeWaitMs"),
                stats.get("recvSpanMs"), stats.get("maxGapMs"),
                stats.get("bytesRecv"),
                turn_id,
            ),
        )


def set_turn_audio(turn_id: int, audio_file: Optional[str]) -> None:
    """
    Attach a saved utterance recording to a persisted turn.

    Separate from insert_turn because the filename is keyed on the rowid
    that insert_turn returns — naming by timestamp instead would collide
    across devices and give the download endpoint nothing to bind the file
    to its turn with.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE turns SET audio_file = ? WHERE id = ?",
            (audio_file, turn_id),
        )


def set_turn_delivery(turn_id: int, send_ms: int, delivery_ms: int,
                      eq_ms: int) -> None:
    """
    Attach controller-measured playback timings to a persisted turn.

    Separate from set_turn_playback because the two arrive from different
    sides at different moments: this is known as soon as the controller has
    finished sending, the device's report lands whenever its buffer drains.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE turns SET send_ms = ?, delivery_ms = ?, eq_ms = ? "
            "WHERE id = ?",
            (send_ms, delivery_ms, eq_ms, turn_id),
        )


def get_turns(
    device_id: str,
    limit: int = 50,
    since: Optional[float] = None,
) -> list[dict]:
    """
    Recent turns for a device as turn_record-shaped dicts (plus turn_id),
    oldest first (newest last — the order turn_history and the API use).
    since: optional epoch-seconds lower bound.
    """
    if since is not None:
        rows = _q(
            "SELECT * FROM turns WHERE device_id = ? AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?",
            (device_id, since, limit),
        )
    else:
        rows = _q(
            "SELECT * FROM turns WHERE device_id = ? ORDER BY ts DESC LIMIT ?",
            (device_id, limit),
        )
    out = []
    for row in reversed(rows):
        rec = {"turn_id": row["id"], "ts": row["ts"]}
        for key, col in _TURN_COLUMNS.items():
            rec[key] = row[col]
        out.append(rec)
    return out


def bump_wake_counters(
    device_id: str,
    near_misses: int = 0,
    near_miss_max: float = 0.0,
    underruns: int = 0,
    dev_frames: int = 0,
    dev_drops: int = 0,
    dev_crossings: int = 0,
    dev_max_score: float = 0.0,
    dev_max_infer_ms: int = 0,
    dev_max_gap_ms: int = 0,
) -> None:
    """
    Accumulate into the current hour's wake_counters row (upsert).

    The dev_* arguments are the on-device shadow window summary (schema v13),
    which rides the device's existing ~30s stats report — so on-device scoring
    adds one upsert per 30s per device and nothing per audio frame.
    """
    hour_ts = int(time.time()) // 3600 * 3600
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO wake_counters (device_id, hour_ts, near_misses, near_miss_max,
                                       underruns, dev_frames, dev_drops, dev_crossings,
                                       dev_max_score, dev_max_infer_ms, dev_max_gap_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (device_id, hour_ts) DO UPDATE SET
                near_misses   = near_misses + excluded.near_misses,
                near_miss_max = MAX(near_miss_max, excluded.near_miss_max),
                underruns     = underruns + excluded.underruns,
                dev_frames    = dev_frames + excluded.dev_frames,
                dev_drops     = dev_drops + excluded.dev_drops,
                dev_crossings = dev_crossings + excluded.dev_crossings,
                dev_max_score = MAX(dev_max_score, excluded.dev_max_score),
                dev_max_infer_ms = MAX(dev_max_infer_ms, excluded.dev_max_infer_ms),
                dev_max_gap_ms   = MAX(dev_max_gap_ms, excluded.dev_max_gap_ms)
            """,
            (device_id, hour_ts, _py(near_misses), _py(near_miss_max), _py(underruns),
             _py(dev_frames), _py(dev_drops), _py(dev_crossings), _py(dev_max_score),
             _py(dev_max_infer_ms), _py(dev_max_gap_ms)),
        )
        conn.execute(
            "DELETE FROM wake_counters WHERE hour_ts < ?",
            (hour_ts - WAKE_COUNTER_RETENTION_DAYS * 86400,),
        )


def get_wake_counters(device_id: str, since: float) -> list[sqlite3.Row]:
    """Hourly wake counters for a device from `since` (epoch s), oldest first."""
    return _q(
        "SELECT * FROM wake_counters WHERE device_id = ? AND hour_ts >= ? "
        "ORDER BY hour_ts",
        (device_id, since),
    )


def record_device_stats(device_id: str, stats: dict) -> None:
    """
    Fold one ~30s hardware stats report into the current hour's
    device_metrics rollup. Missing/None fields are skipped. Averages are
    computed at read time from the sums; gauges (storage, mem total) keep
    the latest value. Prunes rows older than WAKE_COUNTER_RETENTION_DAYS.
    """
    hour_ts = int(time.time()) // 3600 * 3600
    cpu     = stats.get("cpuPct")
    mem     = stats.get("memUsedMb")
    rssi    = stats.get("wifiRssi")
    link_speed = stats.get("linkSpeedMbps")
    cpu_temp   = stats.get("cpuTempC")
    max_temp   = stats.get("maxTempC")
    cores_on   = stats.get("coresOnline")
    cores_tot  = stats.get("coresTotal")
    therm_lim  = stats.get("thermalCoreLimit")
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO device_metrics (
                device_id, hour_ts, samples, cpu_sum, cpu_max, mem_used_sum,
                mem_total_mb, storage_used_mb, storage_total_mb,
                rssi_sum, rssi_samples, rssi_min,
                link_speed_last, link_speed_min, wifi_freq_last,
                wifi_bssid_last, tx_bytes_sum, rx_bytes_sum,
                tx_errors_sum, tx_dropped_sum, rx_crc_sum,
                rtt_sum_ms, rtt_samples, rtt_min_ms, rtt_max_ms,
                rtt_excursions, rtt_excursions_idle, rtt_samples_idle,
                cpu_temp_sum, cpu_temp_samples, cpu_temp_max, max_temp_max,
                cores_online_last, cores_online_min, cores_total,
                thermal_limit_min
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (device_id, hour_ts) DO UPDATE SET
                samples          = samples + 1,
                cpu_sum          = cpu_sum + excluded.cpu_sum,
                cpu_max          = MAX(cpu_max, excluded.cpu_max),
                mem_used_sum     = mem_used_sum + excluded.mem_used_sum,
                mem_total_mb     = COALESCE(excluded.mem_total_mb, mem_total_mb),
                storage_used_mb  = COALESCE(excluded.storage_used_mb, storage_used_mb),
                storage_total_mb = COALESCE(excluded.storage_total_mb, storage_total_mb),
                rssi_sum         = rssi_sum + excluded.rssi_sum,
                rssi_samples     = rssi_samples + excluded.rssi_samples,
                rssi_min         = CASE
                    WHEN excluded.rssi_min IS NULL THEN rssi_min
                    ELSE MIN(COALESCE(rssi_min, excluded.rssi_min), excluded.rssi_min)
                END,
                -- Link identity keeps the latest value (a band/AP change
                -- mid-hour should be visible as the new one); link_speed_min
                -- keeps the worst PHY rate seen, which is the number that
                -- matters when hunting a throughput collapse.
                -- Thermals: sum/samples for a mean that ignores reports where
                -- the sensor was unreadable, plus the peak. cores_online_min is
                -- the tightest the device ever ran; thermal_limit_min below
                -- cores_total means throttling happened this hour.
                cpu_temp_sum     = cpu_temp_sum + excluded.cpu_temp_sum,
                cpu_temp_samples = cpu_temp_samples + excluded.cpu_temp_samples,
                cpu_temp_max     = MAX(COALESCE(cpu_temp_max, excluded.cpu_temp_max),
                                       COALESCE(excluded.cpu_temp_max, cpu_temp_max)),
                max_temp_max     = MAX(COALESCE(max_temp_max, excluded.max_temp_max),
                                       COALESCE(excluded.max_temp_max, max_temp_max)),
                cores_online_last = COALESCE(excluded.cores_online_last, cores_online_last),
                cores_online_min  = CASE
                    WHEN excluded.cores_online_min IS NULL THEN cores_online_min
                    ELSE MIN(COALESCE(cores_online_min, excluded.cores_online_min),
                             excluded.cores_online_min)
                END,
                cores_total       = COALESCE(excluded.cores_total, cores_total),
                thermal_limit_min = CASE
                    WHEN excluded.thermal_limit_min IS NULL THEN thermal_limit_min
                    ELSE MIN(COALESCE(thermal_limit_min, excluded.thermal_limit_min),
                             excluded.thermal_limit_min)
                END,
                link_speed_last  = COALESCE(excluded.link_speed_last, link_speed_last),
                link_speed_min   = CASE
                    WHEN excluded.link_speed_min IS NULL THEN link_speed_min
                    ELSE MIN(COALESCE(link_speed_min, excluded.link_speed_min),
                             excluded.link_speed_min)
                END,
                wifi_freq_last   = COALESCE(excluded.wifi_freq_last, wifi_freq_last),
                wifi_bssid_last  = COALESCE(excluded.wifi_bssid_last, wifi_bssid_last),
                tx_bytes_sum     = tx_bytes_sum   + excluded.tx_bytes_sum,
                rx_bytes_sum     = rx_bytes_sum   + excluded.rx_bytes_sum,
                tx_errors_sum    = tx_errors_sum  + excluded.tx_errors_sum,
                tx_dropped_sum   = tx_dropped_sum + excluded.tx_dropped_sum,
                rx_crc_sum       = rx_crc_sum     + excluded.rx_crc_sum,
                -- RTT arrives pre-aggregated over the window between stats
                -- reports, so sums accumulate and the extremes take the
                -- better/worse of the two. NULL means no samples landed in
                -- that window and must not poison the running min.
                rtt_sum_ms       = rtt_sum_ms  + excluded.rtt_sum_ms,
                rtt_samples      = rtt_samples + excluded.rtt_samples,
                rtt_min_ms       = CASE
                    WHEN excluded.rtt_min_ms IS NULL THEN rtt_min_ms
                    ELSE MIN(COALESCE(rtt_min_ms, excluded.rtt_min_ms), excluded.rtt_min_ms)
                END,
                rtt_max_ms       = CASE
                    WHEN excluded.rtt_max_ms IS NULL THEN rtt_max_ms
                    ELSE MAX(COALESCE(rtt_max_ms, excluded.rtt_max_ms), excluded.rtt_max_ms)
                END,
                rtt_excursions      = rtt_excursions      + excluded.rtt_excursions,
                rtt_excursions_idle = rtt_excursions_idle + excluded.rtt_excursions_idle,
                rtt_samples_idle    = rtt_samples_idle    + excluded.rtt_samples_idle
            """,
            (
                device_id, hour_ts,
                float(cpu or 0.0), float(cpu or 0.0), float(mem or 0.0),
                stats.get("memTotalMb"),
                stats.get("storageUsedMb"), stats.get("storageTotalMb"),
                float(rssi) if rssi is not None else 0.0,
                1 if rssi is not None else 0,
                float(rssi) if rssi is not None else None,
                # linkSpeedMbps is omitempty device-side and refreshed on a
                # slower cadence, so 0/absent means "not sampled this tick"
                # and must not poison the running minimum.
                link_speed or None, link_speed or None,
                stats.get("wifiFreqMhz") or None,
                stats.get("wifiBssid") or None,
                int(stats.get("txBytes") or 0), int(stats.get("rxBytes") or 0),
                int(stats.get("txErrors") or 0), int(stats.get("txDropped") or 0),
                int(stats.get("rxCrcErrors") or 0),
                # RTT is controller-measured and folded into the same report
                # (see Device.drain_rtt). Absent when no probe completed in
                # the window — NULL rather than 0 so the min stays honest.
                int(stats.get("rttSumMs") or 0), int(stats.get("rttSamples") or 0),
                stats.get("rttMinMs"), stats.get("rttMaxMs"),
                int(stats.get("rttExcursions") or 0),
                int(stats.get("rttExcursionsIdle") or 0),
                int(stats.get("rttSamplesIdle") or 0),
                # Thermals. NULL, never 0, when a sensor was unreadable — a
                # zeroed temperature would drag a mean down and read as a cool
                # device, which is the wrong direction for a safety metric.
                float(cpu_temp) if cpu_temp is not None else 0.0,
                1 if cpu_temp is not None else 0,
                float(cpu_temp) if cpu_temp is not None else None,
                float(max_temp) if max_temp is not None else None,
                int(cores_on) if cores_on else None,
                int(cores_on) if cores_on else None,
                int(cores_tot) if cores_tot else None,
                int(therm_lim) if therm_lim else None,
            ),
        )
        conn.execute(
            "DELETE FROM device_metrics WHERE hour_ts < ?",
            (hour_ts - WAKE_COUNTER_RETENTION_DAYS * 86400,),
        )


def get_device_metrics(device_id: str, since: float) -> list[dict]:
    """
    Hourly hardware metrics for a device from `since` (epoch s), oldest
    first, with averages resolved from the stored sums.
    """
    rows = _q(
        "SELECT * FROM device_metrics WHERE device_id = ? AND hour_ts >= ? "
        "ORDER BY hour_ts",
        (device_id, since),
    )
    out = []
    for r in rows:
        n = r["samples"] or 1
        out.append({
            "hour_ts":          r["hour_ts"],
            "samples":          r["samples"],
            "cpu_avg":          round(r["cpu_sum"] / n, 1),
            "cpu_max":          r["cpu_max"],
            "mem_used_avg":     round(r["mem_used_sum"] / n, 1),
            "mem_total_mb":     r["mem_total_mb"],
            "storage_used_mb":  r["storage_used_mb"],
            "storage_total_mb": r["storage_total_mb"],
            "rssi_avg":         round(r["rssi_sum"] / r["rssi_samples"], 1) if r["rssi_samples"] else None,
            "rssi_min":         r["rssi_min"],
            # Link identity + throughput (v7). These were persisted but never
            # surfaced here, so the activity API could not answer "was the
            # link different when this went wrong?".
            # Thermals + topology (v14). cpu_temp_avg divides by its OWN
            # sample count, not `samples`: a report where the sensor was
            # unreadable must not dilute the mean.
            "cpu_temp_avg":      round(r["cpu_temp_sum"] / r["cpu_temp_samples"], 1) if r["cpu_temp_samples"] else None,
            "cpu_temp_max":      r["cpu_temp_max"],
            "max_temp_max":      r["max_temp_max"],
            # cores_online is what makes cpu_avg legible — that percentage is a
            # share of ONLINE capacity, so a series can appear to halve when
            # only the divisor changed.
            "cores_online_last": r["cores_online_last"],
            "cores_online_min":  r["cores_online_min"],
            "cores_total":       r["cores_total"],
            # Below cores_total means the thermal governor capped capacity.
            "thermal_limit_min": r["thermal_limit_min"],
            "link_speed_last":  r["link_speed_last"],
            "link_speed_min":   r["link_speed_min"],
            "wifi_freq_last":   r["wifi_freq_last"],
            "wifi_bssid_last":  r["wifi_bssid_last"],
            "tx_bytes":         r["tx_bytes_sum"],
            "rx_bytes":         r["rx_bytes_sum"],
            # NOTE: tx_errors/tx_dropped/rx_crc are deliberately NOT exposed.
            # The MTK driver leaves every one of them at zero regardless of
            # link quality (/proc/net/wireless reports no retries or missed
            # beacons, signal_poll reports NOISE=9999), so surfacing them
            # would invite reading "0 errors" as "healthy link" — which is
            # exactly the mistake they caused on 2026-07-25.
            #
            # Control-plane RTT (v9) is the latency signal that actually
            # works on this hardware. excursions_idle vs excursions is the
            # discriminator: contention makes latency track load, power-save
            # makes it spike when the device is doing nothing.
            "rtt_avg_ms":       round(r["rtt_sum_ms"] / r["rtt_samples"], 1) if r["rtt_samples"] else None,
            "rtt_min_ms":       r["rtt_min_ms"],
            "rtt_max_ms":       r["rtt_max_ms"],
            "rtt_samples":      r["rtt_samples"],
            "rtt_excursions":      r["rtt_excursions"],
            "rtt_excursions_idle": r["rtt_excursions_idle"],
            "rtt_samples_idle":    r["rtt_samples_idle"],
            # Excursion RATE per state — the actual discriminator. Comparing
            # raw counts is vacuous when almost every sample is idle.
            "rtt_excursion_pct_idle": (
                round(100.0 * r["rtt_excursions_idle"] / r["rtt_samples_idle"], 1)
                if r["rtt_samples_idle"] else None),
            "rtt_excursion_pct_busy": (
                round(100.0 * (r["rtt_excursions"] - r["rtt_excursions_idle"])
                      / (r["rtt_samples"] - r["rtt_samples_idle"]), 1)
                if (r["rtt_samples"] - r["rtt_samples_idle"]) else None),
        })
    return out


# ─── Users ────────────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    """Return a user row by username, or None."""
    return _q1("SELECT * FROM users WHERE username = ?", (username,))


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    """Return a user row by id, or None."""
    return _q1("SELECT * FROM users WHERE id = ?", (user_id,))


def create_user(username: str, password_hash: str, role: str = "readonly") -> int:
    """
    Insert a new user row and return the new user id.

    password_hash must already be bcrypt-hashed — this function does
    not hash passwords itself.
    Raises sqlite3.IntegrityError if username is already taken.
    """
    now = _now()
    with _tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, password_hash, role, now),
        )
        return cur.lastrowid


def get_user_by_ha_id(ha_user_id: str) -> Optional[sqlite3.Row]:
    """Return the user linked to a Home Assistant user id, or None."""
    return _q1("SELECT * FROM users WHERE ha_user_id = ?", (ha_user_id,))


def create_ha_user(ha_user_id: str, username: str, role: str) -> int:
    """
    Insert a user authenticated by Home Assistant and return its id.

    These accounts have no password. The stored hash is a sentinel that no
    bcrypt verification can match, so the account cannot be used on the
    password login form even if the standalone container is later pointed at
    the same database — em_auth.verify_password returns False for a
    malformed hash rather than raising.

    The username is made unique by suffixing, never by reusing an existing
    row: two Home Assistant users may share a display name, and merging them
    onto one account would silently give one person the other's session.
    """
    now = _now()
    with _tx() as conn:
        base = username
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (base,)
        ).fetchone() is not None:
            suffix += 1
            base = f"{username}-{suffix}"
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at,
                               ha_user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (base, HA_USER_PASSWORD_SENTINEL, role, now, ha_user_id),
        )
        return cur.lastrowid


def set_user_role(user_id: int, role: str) -> None:
    """
    Set a user's role. Validated here as well as at the caller, because this
    is the one write that can grant admin — em_ingressauth.role_for already
    constrains its output, and a second gate costs nothing.
    """
    if role not in ("admin", "readonly"):
        raise ValueError(f"unknown role: {role!r}")
    with _tx() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def update_user_password(user_id: int, new_hash: str) -> None:
    """
    Update the password hash for a user.

    new_hash must already be bcrypt-hashed — this function does not hash
    passwords itself. Raises ValueError if the user is not found.
    """
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"User not found: {user_id}")
    log.info(f"[db] Password updated for user id={user_id}")


def get_all_users() -> list[sqlite3.Row]:
    """Return all users (password_hash excluded in the API layer, not here)."""
    return _q("SELECT * FROM users ORDER BY created_at ASC")


def admin_count() -> int:
    """
    Number of admin users. The guard against a role change that leaves the
    install with nobody who can undo it — on the standalone container there
    is no other way back in, since local accounts are the only auth.
    """
    row = _q("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")
    return row[0]["n"] if row else 0


def user_count() -> int:
    """Return the total number of users. Used for first-run bootstrap check."""
    row = _q1("SELECT COUNT(*) AS n FROM users")
    return row["n"] if row else 0


# ─── Sessions ─────────────────────────────────────────────────────────────────

def create_session(token: str, user_id: int, expiry_days: int = 30) -> None:
    """Insert a new session row."""
    now = _now()
    expires = now + expiry_days * 86400
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, now, expires),
        )


def get_session(token: str) -> Optional[sqlite3.Row]:
    """
    Return a valid (non-expired) session row, or None.

    Expired sessions are not automatically deleted here — call
    prune_sessions() periodically from the controller.
    """
    now = _now()
    return _q1(
        "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
        (token, now),
    )


def delete_session(token: str) -> None:
    """Delete a session (logout)."""
    with _tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def prune_sessions() -> int:
    """
    Delete all expired sessions. Returns the number of rows deleted.

    Call from a periodic background task (e.g. once per hour).
    """
    now = _now()
    with _tx() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        deleted = cur.rowcount
    if deleted:
        log.debug(f"[db] Pruned {deleted} expired session(s)")
    return deleted


# ─── System config ────────────────────────────────────────────────────────────

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a system_config value by key, or default if not set."""
    row = _q1("SELECT value FROM system_config WHERE key = ?", (key,))
    if row is None:
        return default
    return row["value"]


def set_config(key: str, value: Optional[str]) -> None:
    """Insert or update a system_config key."""
    with _tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
            (key, value),
        )


def get_all_config() -> dict[str, Optional[str]]:
    """Return the full system_config as a plain dict."""
    rows = _q("SELECT key, value FROM system_config")
    return {row["key"]: row["value"] for row in rows}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> int:
    """Current time as a Unix timestamp (integer seconds)."""
    return int(time.time())
