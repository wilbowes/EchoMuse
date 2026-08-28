"""
em_support.py — support bundle generation
==========================================

Everything needed to diagnose a problem on someone else's fleet, in one file
they can attach to a GitHub issue.

Written because remote diagnosis was costing days per round trip: issue #62
("barge-in still resumes the music") could not be answered without knowing
which entity the user's pause actually reached, and asking one question at a
time across timezones is not a workflow.

**Built as an ALLOWLIST, and that is the whole design.** Fields are named
individually and everything else is dropped, so the failure mode of a new
column is that support loses a field — never that a credential or a
transcript of somebody's living room ends up attached to a public issue. A
denylist gets this wrong exactly once and it is unrecoverable, because the
bundle is already on the internet.

Three rules, in order of how badly they would be missed:

1. **No speech. Ever.** `turns.stt_text` and the recordings are excluded with
   no opt-in flag, because a flag is a thing people tick. A wake-word
   complaint that genuinely needs a transcript can have the user quote one
   line deliberately.
2. **No user-authored free text.** Device labels are written by the user and
   routinely contain names — "Bedroom - Sam" is a real example. Labels are
   replaced with positional pseudonyms; the user can tell us which is which
   if it matters.
3. **No network identifiers.** SSID, BSSID and IP addresses are excluded.
   An SSID is geolocatable from public wardriving databases, which makes a
   bundle attached to an issue a location disclosure.
4. **No account names.** Dashboard logins appear in ordinary log prose
   ("Shell session opened by wil") with nothing to key on, so they are
   replaced from the user table rather than pattern-matched — with the
   account's ROLE (`<admin>`), since that is the diagnostic content and a
   positional alias would still be one-to-one with a real person.

Log lines are sanitised rather than trusted: they are the richest diagnostic
in the bundle AND the most likely to contain speech, since turn lines carry
`text='...'` verbatim. Quoted strings and URLs are stripped, and lines from
known transcript-bearing sources are dropped entirely.

Serials are kept — they identify the user's own hardware to them, and
without them nothing correlates — but nothing else is.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from typing import Any

# Device columns safe to publish. Names are listed rather than filtered so a
# future column is excluded by default.
_DEVICE_FIELDS = (
    "device_id", "approved", "firmware_ver", "firmware_previous",
    "first_seen", "last_seen", "config_sections", "use_global_config",
    "esphome_port", "ble_proxy_port", "ble_proxy_enabled",
)

# Config keys are behaviour, not secrets — but the WiFi credential is neither
# stored here nor wanted, so the guard is explicit rather than assumed.
_CONFIG_DENY = ("psk", "password", "token", "secret", "key")

_TURN_FIELDS = (
    "id", "device_id", "ts", "trigger_type", "wake_model", "wake_score",
    "wake_threshold", "dev_shadow", "dev_wake_score", "dev_threshold",
    "noise_floor", "outcome", "total_ms", "vad_end_ms", "stt_ms",
    "tts_url_ms", "tts_fetch_ms", "playback_ms", "send_ms", "delivery_ms",
    "eq_ms", "underruns", "min_depth", "prime_wait_ms", "recv_span_ms",
    "max_gap_ms", "bytes_recv",
)

# Hourly device metrics, named as `db.get_device_metrics` RETURNS them, not as
# the table stores them. It resolves its sums into averages at read, so an
# allowlist written against the column names (`cpu_sum`, `mem_used_sum`,
# `rssi_sum`, `cpu_temp_sum`) silently matched nothing — every bundle shipped
# without device CPU or memory usage at all, which is most of the reason to
# look at metrics. `test_metric_fields_match_reader` fails if they drift again.
# `wifi_bssid_last` is returned and deliberately NOT listed: it is a network
# identifier.
_METRIC_FIELDS = (
    "device_id", "hour_ts", "samples",
    "cpu_avg", "cpu_max", "mem_used_avg", "mem_total_mb",
    "storage_used_mb", "storage_total_mb",
    "rssi_avg", "rssi_min", "link_speed_last", "link_speed_min",
    "wifi_freq_last", "tx_bytes", "rx_bytes",
    "rtt_avg_ms", "rtt_samples", "rtt_min_ms", "rtt_max_ms",
    "rtt_excursions", "rtt_excursions_idle", "rtt_samples_idle",
    "cpu_temp_avg", "cpu_temp_max", "max_temp_max",
    "cores_online_last", "cores_online_min", "cores_total",
    "thermal_limit_min",
)

# The controller's own resource use. Nothing here is private in itself, but it
# is allowlisted like everything else because the tempting things to add — the
# database path, the hostname, the working directory — carry a username on a
# bare-metal install. Sizes and counts only, never paths.
_CONTROLLER_STAT_FIELDS = (
    "uptime_s", "cpu_pct_1m", "cpu_pct_5m", "cpu_pct_1h", "cpu_pct_life",
    "rss_mb", "mem_total_mb", "mem_available_mb",
    "mem_limit_mb", "load_1", "load_5", "load_15", "cpu_count",
    "data_used_mb", "data_free_mb", "db_mb", "recordings_mb",
    "loop_lag_peak_ms", "python", "platform", "container",
)

# Live device stats. Allowlisted like everything else — an earlier version
# passed live.stats through as a whole dict and leaked wifiBssid/wifiSsid,
# which is the exact mistake the allowlist exists to prevent. Passing a
# nested structure through unfiltered defeats it just as surely as a denylist.
_STATS_FIELDS = (
    "cpuPct", "memUsedMb", "memTotalMb", "storageUsedMb", "storageTotalMb",
    "wifiRssi", "linkSpeedMbps", "wifiFreqMhz", "txBytes", "rxBytes",
    "cpuTempC", "maxTempC", "coresOnline", "coresTotal", "thermalCoreLimit",
    "ambientLux", "owwShadow",
)

_COUNTER_FIELDS = (
    "device_id", "hour_ts", "near_misses", "near_miss_max", "underruns",
    "dev_frames", "dev_drops", "dev_crossings", "dev_max_score",
    "dev_max_infer_ms", "dev_max_gap_ms",
)


# Log lines whose source is known to carry speech. Dropped whole rather than
# sanitised: a partial redaction of a line that quotes a transcript is a bet
# on the regex, and losing the line costs nothing we cannot get elsewhere.
_LOG_DROP = ("STT result", "text=", "Utterance saved", "stt_text")

# Quoted strings and URLs. Turn traces quote transcripts; media URLs carry
# provider paths and session tokens.
_QUOTED = re.compile(r"""(['"])(?:(?!\1).)*\1""")
_URL = re.compile(r"""https?://[^\s'"]+""")

# Bare network identifiers in log prose — "Device connected: ... at 10.10.1.60"
# is not quoted and is not a URL, so neither of the rules above catches it.
# Found by auditing a REAL bundle against the live database; the synthetic
# fixture had no such line.
_IPV4 = re.compile(r"""\b\d{1,3}(?:\.\d{1,3}){3}\b""")
_MAC = re.compile(r"""\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b""")


# Device log lines that are pure volume. `[mem]` heap dumps were 89% of the
# device_logs table and 87% of a real bundle's log tail — 294 lines of 339 —
# which is the diagnostic budget spent on a number already in `metrics` as
# `mem_used_avg`. They are THINNED, not dropped: the newest few still answer
# "what is the heap doing right now", and goroutine count is not recorded
# anywhere else, so a leak hunt would lose its only source.
_LOG_NOISE = ("[mem]",)


def is_noise(line: str) -> bool:
    """True for a line that is volume rather than evidence."""
    return any(marker in line for marker in _LOG_NOISE)


def thin_noise(items: list, keep: int = 3, key=lambda x: x) -> list:
    """
    Keep the first `keep` noise items and drop the rest, order preserved.

    `items` must be NEWEST FIRST, which is how `db.get_device_logs` returns
    them — "first" is then "most recent", and the caller sorts for display.
    `key` extracts the text, so callers can carry a timestamp alongside it
    rather than parsing one back out of the formatted line.
    """
    out, seen = [], 0
    for item in items:
        if is_noise(key(item)):
            seen += 1
            if seen > keep:
                continue
        out.append(item)
    return out


class LogRing(logging.Handler):
    """
    The controller's own recent log, in memory, for support bundles.

    The bundle shipped without it: `controller_log_tail` was in fact the
    per-device `device_logs` table, while every line that would have
    explained issue #62 — the media state pushed to HA, the ESPHome command
    flow, barge-in decisions — goes to stdout and was nowhere in the file. A
    bundle that cannot answer the issue it was built for is worth fixing
    before it is released.

    Bounded by line count, not bytes, and formatted on emit: holding the
    LogRecord instead would keep every argument object alive for the length
    of the ring, which is a memory leak wearing a cache's clothing.

    Sized against the measured rate, not a guess: this controller emits ~38
    lines a minute, of which **65% is `aiohttp.access`** — the dashboard
    polling itself, which says nothing about a device. Dropping that and
    holding 2000 lines covers a couple of hours, so someone who hits a
    problem and then goes to collect a bundle still has the event in it. At
    600 lines including access logs it was sixteen minutes, which is a ring
    that reliably contains everything except the thing you wanted.
    """

    IGNORED = ("aiohttp.access",)

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self.IGNORED):
            return
        try:
            self.lines.append(self.format(record))
        except Exception:
            # A logging handler that raises takes down the call site it was
            # only supposed to observe.
            pass

    def tail(self) -> list[str]:
        return list(self.lines)


class CpuHistory:
    """
    Controller CPU over the windows `uptime` and `top` would give you.

    A lifetime average alone cannot tell "busy for the last minute" from
    "busy for an hour four hours ago", and those want opposite investigations.
    Three windows are enough to point somewhere without becoming a metrics
    system: 1m says what is happening now, 1h says whether it is new.

    Deliberately cheap: one `os.times()` read every `INTERVAL_S` on a ticker
    that already exists, into a ring bounded by the longest window. No task,
    no allocation per sample beyond the tuple, nothing per request.

    Fed from outside rather than reading the clock itself, so the maths is
    testable without sleeping.
    """

    INTERVAL_S = 30.0
    # (seconds, key). Sorted shortest first so a short run still reports
    # something rather than nothing.
    WINDOWS = ((60, "cpu_pct_1m"), (300, "cpu_pct_5m"), (3600, "cpu_pct_1h"))

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, t: float, cpu_seconds: float) -> None:
        """Record (monotonic seconds, process CPU seconds)."""
        self._samples.append((t, cpu_seconds))
        horizon = t - self.WINDOWS[-1][0] - self.INTERVAL_S
        while len(self._samples) > 2 and self._samples[0][0] < horizon:
            self._samples.popleft()

    def windows(self, t: float, cpu_seconds: float) -> dict[str, float]:
        """
        Percent of ONE core over each window, from the oldest sample inside
        it. >100% means more than one core's worth, as in `top`.

        A window is omitted unless it holds at least half its span of
        history: reporting 40 seconds of data as `cpu_pct_1h` is a wrong
        answer, and a missing key is one someone can see is missing.
        """
        out: dict[str, float] = {}
        for span, key in self.WINDOWS:
            oldest = None
            for ts, cpu in self._samples:
                if ts >= t - span:
                    oldest = (ts, cpu)
                    break
            if oldest is None:
                continue
            elapsed = t - oldest[0]
            if elapsed < span / 2:
                continue
            out[key] = round(100.0 * (cpu_seconds - oldest[1]) / elapsed, 1)
        return out


_ROLE_RE = re.compile(r"^[a-z_]{1,16}$")


def _account_pattern(accounts: dict[str, str]) -> tuple[re.Pattern | None, dict[str, str]]:
    """
    Match this install's account names, longest first, mapped to their role.

    Longest first matters: with users `wil` and `wilbowes`, alternation in the
    wrong order rewrites the prefix and leaves `<admin>bowes` behind.
    """
    names = sorted((n for n in (accounts or {}) if n and len(n) >= 2),
                   key=len, reverse=True)
    if not names:
        return None, {}
    roles = {}
    for n in names:
        role = (accounts.get(n) or "user").strip().lower()
        # The role is written into published text, so it is checked rather
        # than trusted — it comes from a database column.
        roles[n.lower()] = role if _ROLE_RE.match(role) else "user"
    return (re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b",
                       re.IGNORECASE), roles)


def sanitise_log(lines: list[str], accounts: dict[str, str] | None = None) -> list[str]:
    """
    Make controller log lines safe to publish.

    Logs are the richest thing in a bundle and the likeliest to contain
    speech — a turn trace carries `text='...'` verbatim — so they are
    filtered, never passed through. Lines from transcript-bearing sources go
    entirely; everything else keeps its structure with quoted strings and
    URLs replaced, since the timings and message types are the diagnostic
    value, not the payload.

    Account names are replaced too: `Shell session opened by wil` is ordinary
    log prose with no quotes, no URL and no pattern to key on, so it survived
    every other rule here. The names are passed in rather than guessed at,
    because the only reliable definition of "a username on this install" is
    the user table. Reported by Wil against a real bundle, 2026-08-02.

    A name becomes its ROLE — `<admin>`, not `user-1` — because the role is
    the whole diagnostic value ("an admin opened a shell") and a positional
    pseudonym still distinguishes people. This is a single-operator system
    today, so `user-1` would have been a one-to-one alias for a real person's
    account.
    """
    users, roles = _account_pattern(accounts or {})
    out = []
    for ln in lines:
        if any(marker in ln for marker in _LOG_DROP):
            continue
        # Quotes first: a quoted URL is handled as a quoted string, and a
        # URL pattern run first would eat the closing quote and leave the
        # line malformed.
        ln = _QUOTED.sub("<redacted>", ln)
        ln = _URL.sub("<url>", ln)
        ln = _IPV4.sub("<ip>", ln)
        ln = _MAC.sub("<mac>", ln)
        if users is not None:
            ln = users.sub(lambda m: f"<{roles.get(m.group(0).lower(), 'user')}>", ln)
        out.append(ln)
    return out


def _pick(row: Any, fields: tuple[str, ...]) -> dict:
    """Project a sqlite3.Row onto an allowlist, skipping absent columns."""
    keys = set(row.keys())
    return {f: row[f] for f in fields if f in keys}


def redact_stats(stats: Any) -> dict | None:
    """Project a device's live stats onto the allowlist, dropping the rest."""
    if not isinstance(stats, dict):
        return None
    return {k: stats[k] for k in _STATS_FIELDS if k in stats}


def redact_config(config: dict) -> dict:
    """
    Drop anything credential-shaped from a device/fleet config.

    Config is behaviour (thresholds, EQ, LED scenes) and is the most useful
    part of a bundle, so it is included — but a key whose NAME suggests a
    secret is dropped without needing to know what it is. Belt and braces
    against a future config key nobody thought about.
    """
    out = {}
    for k, v in (config or {}).items():
        if any(bad in k.lower() for bad in _CONFIG_DENY):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def build(
    *,
    controller_version: str,
    devices: list,
    fleet_config: dict,
    schema_version: int,
    turns: list,
    metrics: list,
    counters: list,
    device_configs: dict[str, dict],
    live_state: dict[str, dict],
    controller_log: list[str],
    device_log: list[str],
    accounts: dict[str, str] | None = None,
    controller_stats: dict | None = None,
) -> dict:
    """
    Assemble the bundle. Pure — callers do the I/O, so this is testable
    without a database, and the redaction can be asserted directly.
    """
    bundle: dict = {
        "generated_at": int(time.time()),
        # 2: controller_log_tail became the CONTROLLER's log; the relayed
        # per-device lines moved to device_log_tail.
        "format": 2,
        "redaction": (
            "Allowlisted fields only. Contains no speech, no transcripts, no "
            "audio, no device labels, no account names, and no network "
            "identifiers (SSID, BSSID, IP). Credentials — device tokens, "
            "ESPHome PSKs, password hashes, session tokens — are never "
            "included. Device serials ARE included, so this identifies your "
            "own hardware to you."
        ),
        "controller": {
            "version": controller_version,
            "schema_version": schema_version,
            # The controller's own resources. Without them a bundle can show
            # a device starving for audio and give no way to tell whether the
            # host it streams from was out of CPU, memory or disk at the time.
            "stats": {k: controller_stats[k] for k in _CONTROLLER_STAT_FIELDS
                      if k in (controller_stats or {})},
        },
        "fleet_config": redact_config(fleet_config),
        "devices": [],
        "turns": [_pick(t, _TURN_FIELDS) for t in turns],
        "metrics": [_pick(m, _METRIC_FIELDS) for m in metrics],
        "wake_counters": [_pick(c, _COUNTER_FIELDS) for c in counters],
        "controller_log_tail": sanitise_log(controller_log, accounts),
        "device_log_tail": sanitise_log(device_log, accounts),
    }

    for n, d in enumerate(devices, start=1):
        entry = _pick(d, _DEVICE_FIELDS)
        # Positional pseudonym: labels are user-authored and routinely carry
        # names. The user can say which is which if it ever matters.
        entry["name"] = f"device-{n}"
        did = entry.get("device_id")
        entry["config"] = redact_config(device_configs.get(did, {}))
        # Live state matters more than stored state for "it is behaving
        # oddly right now": capabilities decide which HA entities exist,
        # and connected/link tell you whether to trust anything else.
        entry["live"] = live_state.get(did, {})
        bundle["devices"].append(entry)

    return bundle


def to_json(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, default=str)


# ─── Provisioning diagnostics ─────────────────────────────────────────────────
#
# What the wizard collects when a step fails, packaged here rather than in the
# browser. The redaction rules and their tests already live in this module, and
# a second copy in JavaScript would drift from them without anyone noticing
# until a bundle carried something it should not have.
#
# The wizard collects RAW and posts it. Everything below decides what survives.
# That direction matters: the browser is talking to a device we know nothing
# about yet, so treating its output as untrusted input is the only safe
# posture, and it means adding a probe never needs the redaction re-reviewed.
#
# THIS IS AN ALLOWLIST, twice over: a probe name nobody listed is dropped whole,
# and inside the probes that carry key/value output the keys are listed too. A
# denylist here gets it wrong once, publicly, and cannot be taken back.

# Properties worth having, chosen against failures actually seen. Build
# identity dominates because firmware defaults differ between the six FireOS 5
# builds in circulation in ways that reach USB and ADB behaviour (#79), and
# "which build" was the first question on every provisioning issue so far.
_PROVISION_PROPS = (
    "ro.product.model",
    "ro.product.name",
    "ro.product.device",
    "ro.build.version.name",
    "ro.build.version.incremental",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.fingerprint",
    "ro.serialno",
    "ro.boot.slot_suffix",
    "sys.boot_completed",
    "persist.sys.usb.config",
    "persist.wifi.migrate.complete",
)

# `wpa_cli status` keys. The state and the crypto are the diagnostic value;
# the identifiers are what makes an SSID geolocatable from public wardriving
# databases, so ssid, bssid, ip_address, address and uuid are simply absent.
_WPA_STATUS_KEYS = (
    "wpa_state",
    "freq",
    "key_mgmt",
    "pairwise_cipher",
    "group_cipher",
    "mode",
    "suppPortStatus",
)

# Probe names the wizard may post. Anything else is dropped.
_PROVISION_PROBES = (
    "props",          # getprop, filtered to _PROVISION_PROPS
    "root",           # su -c id
    "selinux",        # getenforce
    "pm_ready",       # pm path android
    "storage",        # df /data
    "wpa_status",     # wpa_cli status
    "wpa_scan",       # wpa_cli scan_results
    "wpa_caps",       # wpa_cli get_capability key_mgmt
    "services",       # getprop | grep init.svc
    "processes",      # wpa_supplicant / SmartHomeWifid counts
    "packages",       # how many of the disable/hide lists are still visible
    "data_property",  # filenames only
    "boot_target",    # what /dev/block/other-boot resolves to (TWRP steps)
)

# crown (Echo Show 8) has none of biscuit's TWRP/Magisk/Alexa/WiFi-step
# surface, so it gets its own, much shorter, allowlist rather than inheriting
# probes that would only ever read empty on it. `launcher_log` and
# `device_nodes` exist because they answer the two failure modes this
# hardware has already produced live and neither one is covered by anything
# above: the appops-after-reinstall trap (status-bar handoff) and the
# ueventd /dev/snd+/dev/input permission patch not surviving a reflash
# (device/crown_launcher/ueventd.rc.crown-orig).
_CROWN_PROVISION_PROBES = (
    "props",          # getprop, filtered to _PROVISION_PROPS (same allowlist)
    "root",           # id (already root — no su broker on crown)
    "selinux",        # getenforce
    "pm_ready",       # pm path android
    "storage",        # df /data
    "overlay_perm",   # appops get com.echomuse.crownlauncher SYSTEM_ALERT_WINDOW
    "device_nodes",   # ls -l on the ueventd-patched /dev/snd + /dev/input nodes
    "processes",      # crownlauncher / server process presence
    "launcher_log",   # tail of /data/local/tmp/echomuse.log — the daemon's own log
)

_INIT_SVC = re.compile(r"^\[init\.svc\.[a-z0-9_.-]+\]:\s*\[[a-z]+\]$", re.I)


def _scrub(text: str) -> list[str]:
    """
    Generic backstop applied to every probe, whatever else was done to it.

    Same substitutions as `sanitise_log`, minus the account names, which
    cannot appear in device shell output. Runs even on probes that have their
    own parser, because the parser is the thing most likely to be wrong about
    an output shape nobody has seen: these devices are the ones behaving
    unusually, by definition.
    """
    out = []
    for ln in (text or "").splitlines():
        ln = _QUOTED.sub("<redacted>", ln)
        ln = _URL.sub("<url>", ln)
        ln = _IPV4.sub("<ip>", ln)
        ln = _MAC.sub("<mac>", ln)
        if ln.strip():
            out.append(ln.rstrip())
    return out


def _probe_props(text: str) -> dict:
    """`getprop` output projected onto the property allowlist."""
    found = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]$", ln.strip())
        if m and m.group(1) in _PROVISION_PROPS:
            found[m.group(1)] = m.group(2)
    return found


def _probe_wpa_status(text: str) -> dict:
    """`wpa_cli status` projected onto the key allowlist."""
    found = {}
    for ln in (text or "").splitlines():
        k, _, v = ln.strip().partition("=")
        if k in _WPA_STATUS_KEYS:
            found[k] = v
    return found


def _probe_wpa_scan(text: str, selected: str | None = None) -> list[dict]:
    """
    Scan results with the names taken out and the radio left in.

    This is the one probe where the tension is real. The flags and the
    frequency ARE the diagnosis — a `[SAE-CCMP]` network is one this radio
    cannot join, and that single fact explains #82 — while the names are the
    part that locates someone's house. So the row survives and the SSID
    becomes a positional placeholder.

    The network the operator was trying to join is marked, because "the one
    you wanted is WPA3" is the whole answer and is otherwise unrecoverable
    once the names are gone.
    """
    rows = []
    for ln in (text or "").splitlines():
        parts = ln.rstrip("\n").split("\t")
        if len(parts) < 4 or parts[0].strip().lower().startswith("bssid"):
            continue
        bssid, freq, signal, flags = (p.strip() for p in parts[:4])
        ssid = parts[4].strip() if len(parts) > 4 else ""
        if not _MAC.fullmatch(bssid):
            continue
        rows.append({
            "network":  f"network-{len(rows) + 1}",
            "freq":     freq,
            "signal":   signal,
            "flags":    flags,
            "selected": bool(selected) and ssid == selected,
        })
    return rows


def _probe_services(text: str) -> list[str]:
    """`init.svc.*` lines only. Their names and states, nothing else."""
    return [ln.strip() for ln in (text or "").splitlines()
            if _INIT_SVC.match(ln.strip())]


def build_provision_diagnostics(
    *,
    step: str,
    error: str,
    probes: dict,
    transcript: list[str] | None = None,
    selected_ssid: str | None = None,
    controller_version: str = "unknown",
    platform: str = "biscuit",
) -> dict:
    """
    One file the reporter can attach to a public issue after a failed step.

    Built because diagnosing #79 and #82 each took several rounds of asking
    someone to run `getprop` and `wpa_cli` by hand, and by the time they
    answered the device had usually been retried or rebooted, so the state at
    the moment of failure was gone. Collection is automatic; the download is
    deliberate.

    `step` and `error` are OURS — the step id comes from a fixed list and the
    error is our own message — but they are scrubbed anyway rather than
    trusted, because an error string interpolates device output often enough
    that the distinction is not worth relying on.

    The transcript is scrubbed line by line and is where a device's own words
    reach the file, so it gets the same treatment as everything else.

    `platform` selects the allowlist — "biscuit" (default, back-compat with
    every existing caller) or "crown". crown has none of biscuit's TWRP/
    Magisk/Alexa/WiFi-step surface, so it would otherwise report every one of
    those probes as missing, which reads as "the wizard failed to ask" rather
    than "this device has no such thing to ask about".
    """
    allowlist = _CROWN_PROVISION_PROBES if platform == "crown" else _PROVISION_PROBES
    out = {
        "format": 1,
        "kind": "provision_diagnostics",
        "generated": time.time(),
        "controller_version": controller_version,
        "platform": platform,
        "step": " ".join(_scrub(str(step))) or "unknown",
        "error": _scrub(str(error)),
        "probes": {},
    }

    raw = probes if isinstance(probes, dict) else {}
    for name in allowlist:
        if name not in raw:
            continue
        text = raw.get(name)
        if not isinstance(text, str):
            continue
        if name == "props":
            value = _probe_props(text)
        elif name == "wpa_status":
            value = _probe_wpa_status(text)
        elif name == "wpa_scan":
            value = _probe_wpa_scan(text, selected_ssid)
        elif name == "services":
            value = _probe_services(text)
        else:
            value = _scrub(text)
        out["probes"][name] = value

    # Named so a reader can tell "the wizard did not ask" from "the device had
    # nothing to say", which need opposite next questions.
    out["probes_missing"] = [n for n in allowlist if n not in out["probes"]]

    if transcript:
        out["transcript"] = [ln for t in transcript for ln in _scrub(str(t))]
    return out
