"""
Support-bundle privacy contract.

A bundle exists to be attached to a public GitHub issue, so a leak here is
not recoverable — it is already on the internet by the time anyone notices.
These tests assert the contract rather than the implementation, and are
written as "this value must not appear ANYWHERE in the serialised output"
rather than "this field is absent", because a leak via an unexpected path
(a log line, a config value, a nested dict) is exactly the one nobody
predicts.
"""

import inspect
import json
import re
from pathlib import Path

import em_support as S


class Row(dict):
    """Stand-in for sqlite3.Row — supports .keys() and subscripting."""
    def keys(self):
        return list(super().keys())


SECRETS = {
    "token": "tok_9f3aSECRETdeviceauth",
    "psk": "psk_SECRETnoisekey==",
    "pwhash": "$2b$12$SECRETbcrypthash",
    "session": "sess_SECRETlogin",
    "speech": "turn off the bedroom light",
    "label": "Bedroom - Sam",
    "ssid": "Bowes-Family-5G",
    "bssid": "d6:9b:22:11:aa:bb",
    "ip": "10.10.1.104",
    # Dashboard account name. Reported against a REAL bundle on 2026-08-02:
    # "Shell session opened by wil" is ordinary prose with no quotes, no URL
    # and no identifier shape, so every other rule here passed it through.
    "username": "wilbowes",
}

ACCOUNTS = {SECRETS["username"]: "admin", "wil": "admin", "sam": "readonly"}


def _bundle():
    device = Row({
        "device_id": "G090LF1180130NJG",
        "label": SECRETS["label"],
        "approved": 1,
        "firmware_ver": "v2.9.13",
        "ip": SECRETS["ip"],
        "token": SECRETS["token"],
        "esphome_noise_psk": SECRETS["psk"],
    })
    turn = Row({
        "id": 1, "device_id": "G090LF1180130NJG", "outcome": "ok",
        "wake_score": 0.98, "total_ms": 1200,
        "stt_text": SECRETS["speech"],
    })
    metric = Row({
        "device_id": "G090LF1180130NJG", "hour_ts": 0, "rtt_samples": 100,
        "rtt_excursions": 20, "wifi_bssid_last": SECRETS["bssid"],
        "wifi_ssid": SECRETS["ssid"],
    })
    return S.build(
        controller_version="v2.12.1",
        devices=[device],
        fleet_config={"owwThreshold": 0.5, "wifiPsk": SECRETS["psk"]},
        schema_version=16,
        turns=[turn],
        metrics=[metric],
        counters=[],
        device_configs={"G090LF1180130NJG": {"owwModel": "hey_mycroft_v0.1",
                                             "some_token": SECRETS["token"]}},
        live_state={"G090LF1180130NJG": {
            "connected": True,
            "capabilities": ["mic", "ambient_light"],
            # A whole stats dict — the path that actually leaked in a REAL
            # bundle, because it was handed over unfiltered.
            "stats": S.redact_stats({
                "cpuPct": 27.5, "wifiRssi": -58, "ambientLux": 16,
                "wifiBssid": SECRETS["bssid"], "wifiSsid": SECRETS["ssid"],
            }),
        }},
        controller_log=[
            "10:00:01 [INFO] echomuse — Barge-in: cancelling playback",
            "10:00:02 [DEBUG] echomuse.esphome — MediaPlayerCommandRequest command=PAUSE",
            f"10:00:03 [INFO] echomuse — Shell session opened by {SECRETS['username']}",
        ],
        device_log=[
            f"[TURN] trigger=wakeword outcome=ok text='{SECRETS['speech']}'",
            "play_media: 'http://10.10.1.81:8097/flow/abc/media.flac'",
            "Playback chain: decoder spawned 3ms, first audio 194ms",
            "RTT excursion: 1450ms (idle)",
            # Bare identifiers in prose: neither quoted nor a URL, so the
            # quote and URL rules both miss them. Found by auditing a real
            # bundle against the live database — the synthetic fixture had
            # no such line, which is exactly why it passed.
            f"Device connected: G090LF1180130NJG v=v2.9.13 at {SECRETS['ip']} caps=[mic]",
            f"associated bssid {SECRETS['bssid']} freq 5805",
            f"Shell session opened by {SECRETS['username']}",
            "Shell session closed by wil",
        ],
        accounts=ACCOUNTS,
        controller_stats={"cpu_pct_1m": 12.4, "rss_mb": 210.5, "db_mb": 38.2,
                          "data_free_mb": 51204.0, "uptime_s": 3600,
                          # Not allowlisted: a data directory carries the
                          # account name on a bare-metal install.
                          "data_dir": f"/home/{SECRETS['username']}/echomuse"},
    )


def test_no_secret_value_appears_anywhere_in_the_bundle():
    """
    The load-bearing test. Serialise the whole thing and search it — a field
    check would miss a leak through a log line or a nested config value,
    which is the leak nobody anticipates.
    """
    text = S.to_json(_bundle())
    for name, value in SECRETS.items():
        assert value not in text, (
            f"{name} leaked into the support bundle: {value!r}"
        )


def test_speech_is_excluded_with_no_opt_in():
    """
    No flag, deliberately. A flag is a thing people tick, and this one cannot
    be untickled once the bundle is on a public issue.
    """
    import inspect
    sig = inspect.signature(S.build)
    for param in sig.parameters:
        assert "transcript" not in param.lower(), (
            f"build() grew a {param!r} parameter — speech must have no opt-in"
        )
    assert "stt_text" not in S._TURN_FIELDS


def test_labels_are_replaced_with_positional_pseudonyms():
    """Device labels are user-authored and routinely contain names."""
    b = _bundle()
    d = b["devices"][0]
    assert d["name"] == "device-1"
    assert "label" not in d


def test_the_useful_diagnostics_survive():
    """
    Privacy that removes the diagnostic value is not a win — the bundle has
    to still answer the questions it exists for.
    """
    b = _bundle()
    d = b["devices"][0]
    assert d["device_id"] == "G090LF1180130NJG", "serials correlate everything"
    assert d["firmware_ver"] == "v2.9.13"
    assert d["live"]["capabilities"] == ["mic", "ambient_light"], \
        "capabilities decide which HA entities exist — the #62 question"
    assert d["config"]["owwModel"] == "hey_mycroft_v0.1"
    assert b["turns"][0]["outcome"] == "ok"
    assert b["turns"][0]["wake_score"] == 0.98
    assert b["metrics"][0]["rtt_excursions"] == 20
    assert b["controller"]["version"] == "v2.12.1"
    assert b["controller"]["schema_version"] == 16

    log = "\n".join(b["device_log_tail"])
    assert "Playback chain" in log and "194ms" in log, \
        "timings are the point of shipping logs at all"
    assert "RTT excursion: 1450ms" in log


def test_transcript_bearing_log_lines_are_dropped_whole():
    b = _bundle()
    log = "\n".join(b["device_log_tail"])
    assert "[TURN]" not in log, \
        "turn traces quote transcripts verbatim and must not be sanitised in place"


def test_live_stats_are_allowlisted_not_passed_through():
    """
    Regression: live.stats was handed over as a whole dict and leaked
    wifiBssid/wifiSsid into a real bundle. Passing a nested structure through
    unfiltered defeats an allowlist just as surely as using a denylist.
    """
    out = S.redact_stats({"cpuPct": 27.5, "wifiRssi": -58,
                          "wifiBssid": "aa:bb:cc:dd:ee:ff",
                          "wifiSsid": "Some-Home-Network"})
    assert out == {"cpuPct": 27.5, "wifiRssi": -58}, \
        "stats must be projected onto the allowlist, not filtered"


def test_bare_identifiers_in_log_prose_are_stripped():
    """
    "Device connected: ... at 10.10.1.60" is neither quoted nor a URL, so the
    first two rules both miss it. Real logs are full of these.
    """
    out = "\n".join(S.sanitise_log([
        "Device connected: ABC v=v2.9.13 at 10.10.1.60 caps=[mic]",
        "associated bssid 24:5a:4c:83:7b:e5 freq 5805",
    ]))
    assert "10.10.1.60" not in out and "<ip>" in out
    assert "24:5a:4c:83:7b:e5" not in out and "<mac>" in out
    assert "v=v2.9.13" in out, "the diagnostic content must survive"


def test_config_redaction_catches_credential_shaped_keys():
    out = S.redact_config({"owwThreshold": 0.5, "wifiPsk": "x", "api_token": "y",
                           "somePassword": "z", "eqBands": [1, 2]})
    assert out["owwThreshold"] == 0.5
    assert out["eqBands"] == [1, 2]
    for k in ("wifiPsk", "api_token", "somePassword"):
        assert out[k] == "<redacted>", f"{k} should have been redacted"


def test_account_names_are_stripped_from_log_prose():
    """
    Regression, from a real bundle: "Shell session opened by wil" carries the
    dashboard account name in plain prose. Nothing about it is quoted, and it
    has no identifier shape to match, so it survived every rule here.
    """
    out = "\n".join(S.sanitise_log(
        ["Shell session opened by wil", "Login failed for wilbowes"],
        accounts=ACCOUNTS))
    assert "wil" not in out and "wilbowes" not in out
    assert out.count("<admin>") == 2, "a name becomes its role, not an alias"
    assert "Shell session opened by" in out, "the event must survive the name"


def test_longer_usernames_are_replaced_before_their_prefixes():
    """
    With users `wil` and `wilbowes`, replacing the short one first leaves
    `<user>bowes` — the leak, still legible, wearing a redaction marker.
    """
    out = "\n".join(S.sanitise_log(["opened by wilbowes"],
                                   accounts={"wil": "admin", "wilbowes": "admin"}))
    assert out == "opened by <admin>"


def test_username_redaction_does_not_eat_substrings():
    """
    Word boundaries: a user called `sam` must not turn `samples=120` into
    `<user>ples=120` and destroy the diagnostic.
    """
    out = "\n".join(S.sanitise_log(["stats samples=120 for sam"], accounts={"sam": "readonly"}))
    assert "samples=120" in out and out.endswith("<readonly>")


def test_no_usernames_is_not_an_error():
    """A fresh install with no users must not blow up the bundle."""
    assert S.sanitise_log(["nothing to redact"], accounts={}) == ["nothing to redact"]
    assert S.sanitise_log(["nothing to redact"]) == ["nothing to redact"]


def test_controller_stats_are_allowlisted():
    """
    System stats are not secret in themselves, but the tempting neighbours —
    data directory, hostname, cwd — carry an account name on a bare-metal
    install, which is the same leak in a different field.
    """
    b = _bundle()
    stats = b["controller"]["stats"]
    assert stats["cpu_pct_1m"] == 12.4 and stats["rss_mb"] == 210.5
    assert stats["db_mb"] == 38.2 and stats["uptime_s"] == 3600
    assert "data_dir" not in stats, "paths must never be reported"


def test_metric_fields_match_what_the_reader_returns():
    """
    The allowlist named the TABLE's columns (`cpu_sum`, `mem_used_sum`) while
    `db.get_device_metrics` resolves those sums into averages at read — so
    every device CPU and memory figure was silently dropped from every
    bundle, which is most of the reason to include metrics at all. An
    allowlist naming a key nothing produces fails silently and looks careful.
    """
    import em_db

    src = inspect.getsource(em_db.get_device_metrics)
    returned = set(re.findall(r'"(\w+)":\s', src))
    assert returned, "could not read the keys get_device_metrics returns"

    allow = set(S._METRIC_FIELDS) - {"device_id"}  # attached by the caller
    unknown = allow - returned
    assert not unknown, (
        f"allowlisted metric fields that get_device_metrics never returns: "
        f"{sorted(unknown)} — they will silently be dropped"
    )

    # The converse is not an error (wifi_bssid_last is excluded on purpose),
    # but the resource figures are the point of the section.
    for key in ("cpu_avg", "cpu_max", "mem_used_avg", "mem_total_mb",
                "storage_used_mb", "storage_total_mb"):
        assert key in allow, f"{key} is the reason metrics are in the bundle"


def test_metrics_carry_the_device_they_belong_to():
    """
    Metrics are pooled across the fleet into one flat list, so a row without
    a device_id cannot be attributed to anything — six devices' CPU and
    memory with no way to tell whose is whose.
    """
    b = _bundle()
    assert b["metrics"], "fixture should produce a metric row"
    for m in b["metrics"]:
        assert m.get("device_id"), "every metric row must name its device"


def test_the_role_is_published_not_the_person():
    """
    A single-operator install has one account, so `user-1` would be a
    one-to-one alias for a real person. `<admin>` says the thing worth
    knowing — who had the authority to do it — and says nothing about who.
    """
    out = "\n".join(S.sanitise_log(
        ["Shell session opened by wil", "config changed by sam"],
        accounts={"wil": "admin", "sam": "readonly"}))
    assert "<admin>" in out and "<readonly>" in out
    assert "wil" not in out and "sam" not in out


def test_a_junk_role_does_not_reach_the_published_file():
    """The role is a database column, so it is checked, not trusted."""
    out = "\n".join(S.sanitise_log(
        ["opened by bob"], accounts={"bob": "admin<script>alert(1)</script>"}))
    assert out == "opened by <user>"


class TestCpuWindows:
    """
    CPU over 1m/5m/1h. Fed explicit samples so the maths is tested without
    sleeping for an hour.
    """

    def _history(self, points):
        h = S.CpuHistory()
        for t, cpu in points:
            h.add(t, cpu)
        return h

    def test_windows_report_percent_of_one_core(self):
        # 30s of wall clock, 15s of CPU consumed = 50% of one core.
        h = self._history([(0.0, 0.0), (30.0, 15.0), (60.0, 30.0)])
        out = h.windows(60.0, 30.0)
        assert out["cpu_pct_1m"] == 50.0

    def test_over_one_hundred_percent_is_reported_not_clamped(self):
        """More than a core's worth is exactly what a runaway looks like."""
        h = self._history([(0.0, 0.0), (60.0, 150.0)])
        out = h.windows(60.0, 150.0)
        assert out["cpu_pct_1m"] == 250.0

    def test_a_window_without_enough_history_is_omitted(self):
        """
        Reporting 40 seconds of data as `cpu_pct_1h` is a wrong answer; a
        missing key is one the reader can see is missing.
        """
        h = self._history([(0.0, 0.0), (30.0, 5.0), (60.0, 10.0)])
        out = h.windows(60.0, 10.0)
        assert "cpu_pct_1m" in out
        assert "cpu_pct_5m" not in out and "cpu_pct_1h" not in out

    def test_recent_load_is_distinguishable_from_an_old_burst(self):
        """
        The whole reason for windows: an hour of idle after a busy hour must
        not read the same as busy now.
        """
        pts = [(float(t), 0.0) for t in range(0, 1800, 30)]          # idle
        pts += [(float(t), (t - 1800) * 0.9) for t in range(1800, 3660, 30)]  # busy
        h = self._history(pts)
        out = h.windows(3630.0, (3630 - 1800) * 0.9)
        assert out["cpu_pct_1m"] == 90.0, "recent load must show at 1m"
        assert out["cpu_pct_1h"] < out["cpu_pct_1m"], \
            "the hour includes the idle half and must read lower"

    def test_the_ring_stays_bounded(self):
        """A day of sampling must not accumulate a day of samples."""
        h = S.CpuHistory()
        for i in range(0, 86400, 30):
            h.add(float(i), i * 0.1)
        expected = S.CpuHistory.WINDOWS[-1][0] / S.CpuHistory.INTERVAL_S
        assert len(h._samples) <= expected + 3, "ring should hold ~1h of samples"


def test_the_controllers_own_log_is_in_the_bundle():
    """
    It was not, and that is why the bundle could not answer #62: the media
    state pushed to HA and the ESPHome command flow both go to stdout, while
    `controller_log_tail` in fact held the relayed per-device table.
    """
    b = _bundle()
    ctrl = "\n".join(b["controller_log_tail"])
    assert "Barge-in: cancelling playback" in ctrl
    assert "MediaPlayerCommandRequest command=PAUSE" in ctrl, \
        "the HA command flow is the evidence #62 turned on"
    assert "device_log_tail" in b, "the per-device log keeps its own key"
    assert b["format"] >= 2, "the shape changed; the format marker must say so"


def test_the_controller_log_is_sanitised_like_any_other():
    """A new log source is a new leak path unless it goes through the same rules."""
    b = _bundle()
    text = json.dumps(b["controller_log_tail"])
    assert SECRETS["username"] not in text and "<admin>" in text


class TestNoiseThinning:
    def test_heap_dumps_are_thinned_not_dropped(self):
        """
        [mem] was 87% of a real bundle's log tail, for a number already in
        metrics — but goroutine count is recorded nowhere else, so a few of
        the newest must survive for a leak hunt.
        """
        lines = [f"[mem] goroutines=19 heap_alloc={i}KB" for i in range(50)]
        out = S.thin_noise(lines, keep=3)
        assert len(out) == 3
        assert out[0].endswith("heap_alloc=0KB"), "newest first must be kept"

    def test_evidence_is_never_thinned(self):
        lines = ["Wake word detected (score=1.000)"] + \
                [f"[mem] heap_alloc={i}KB" for i in range(20)] + \
                ["Device connected: ABC"]
        out = S.thin_noise(lines, keep=2)
        assert "Wake word detected (score=1.000)" in out
        assert "Device connected: ABC" in out
        assert sum(1 for l in out if "[mem]" in l) == 2

    def test_a_key_lets_the_caller_carry_a_timestamp(self):
        """So the caller sorts by real time instead of parsing it back out."""
        pairs = [(3, "[mem] a"), (2, "[mem] b"), (1, "Wake word detected")]
        out = S.thin_noise(pairs, keep=1, key=lambda p: p[1])
        assert out == [(3, "[mem] a"), (1, "Wake word detected")]


class TestLogRing:
    def test_it_holds_the_most_recent_lines_and_no_more(self):
        import logging
        ring = S.LogRing(capacity=5)
        ring.setFormatter(logging.Formatter("%(message)s"))
        for i in range(20):
            ring.emit(logging.LogRecord("x", logging.INFO, "f", 1, f"line {i}", None, None))
        assert ring.tail() == [f"line {i}" for i in range(15, 20)]

    def test_a_broken_record_cannot_take_down_its_call_site(self):
        """
        A logging handler that raises breaks the code it was only meant to
        observe — and it would do it inside the bundle's own logging.
        """
        import logging
        ring = S.LogRing(capacity=5)
        ring.setFormatter(logging.Formatter("%(message)s %(nonexistent)s"))
        ring.emit(logging.LogRecord("x", logging.INFO, "f", 1, "boom", None, None))
        assert ring.tail() == [], "the bad record is dropped, not raised"


def test_the_ring_ignores_the_dashboards_own_http_chatter():
    """
    aiohttp.access was 65% of the measured log rate and says nothing about a
    device — it is the dashboard polling itself. Keeping it meant the ring
    covered sixteen minutes, so it reliably held everything except the event
    someone opened a bundle to report.
    """
    import logging
    ring = S.LogRing(capacity=10)
    ring.setFormatter(logging.Formatter("%(message)s"))
    for name, msg in (("aiohttp.access", "GET /api/devices 200"),
                      ("echomuse", "Barge-in: cancelling playback")):
        ring.emit(logging.LogRecord(name, logging.INFO, "f", 1, msg, None, None))
    assert ring.tail() == ["Barge-in: cancelling playback"]


# ─── Provisioning diagnostics (#87) ───────────────────────────────────────────
#
# Realistic raw probe output, carrying every kind of thing that must not reach
# a public issue: an SSID, a BSSID, an IP, a MAC, and a home network name.

_RAW_PROPS = """
[ro.product.model]: [AEOBC]
[ro.build.version.incremental]: [272.6.8.0_user_680767620]
[ro.serialno]: [G090LF1180570SPJ]
[persist.sys.usb.config]: [mtp,adb]
[net.hostname]: [SamsPhone-Bedroom]
[wifi.interface]: [wlan0]
[dhcp.wlan0.ipaddress]: [10.10.1.188]
""".strip()

_RAW_STATUS = """
bssid=74:ac:b9:d6:9b:21
freq=5180
ssid=Neptune-Media
id=0
mode=station
pairwise_cipher=CCMP
group_cipher=CCMP
key_mgmt=WPA2-PSK
wpa_state=COMPLETED
ip_address=10.10.1.188
address=ac:63:be:11:22:33
uuid=6f1c2b3a-0000-1111-2222-333344445555
""".strip()

_RAW_SCAN = (
    "bssid / frequency / signal level / flags / ssid\n"
    "74:ac:b9:d6:9b:21\t5180\t-52\t[WPA2-PSK-CCMP][ESS]\tNeptune-Media\n"
    "24:5a:4c:83:7b:e4\t2437\t-71\t[SAE-CCMP][ESS]\tPixel_7793\n"
    "aa:bb:cc:dd:ee:ff\t2412\t-80\t[ESS]\tSams Room 2.4\n"
)


def _diag(**kw):
    kw.setdefault("step", "configure_wifi")
    kw.setdefault("error", "Association failed")
    kw.setdefault("probes", {})
    return S.build_provision_diagnostics(**kw)


def test_provision_diagnostics_leak_nothing_identifying():
    """
    The whole point is that this gets attached to a public issue, so the
    check is on the SERIALISED output rather than field by field. A leak
    through a probe nobody thought to parse is the one that gets missed.
    """
    out = json.dumps(_diag(
        probes={
            "props":      _RAW_PROPS,
            "wpa_status": _RAW_STATUS,
            "wpa_scan":   _RAW_SCAN,
            "storage":    "/dev/block/mmcblk0p23 5.0G 3.1G 1.9G 62% /data",
        },
        transcript=["Writing config for \"Neptune-Media\"…",
                    "Connected! IP: 10.10.1.188"],
        selected_ssid="Neptune-Media",
    ))
    for secret in ("Neptune-Media", "Pixel_7793", "Sams Room",
                   "SamsPhone-Bedroom", "10.10.1.188",
                   "74:ac:b9:d6:9b:21", "ac:63:be:11:22:33",
                   "6f1c2b3a"):
        assert secret not in out, f"{secret!r} reached the diagnostics file"


def test_the_radio_facts_survive_the_scan_redaction():
    """
    Names out, radio in. `[SAE-CCMP]` is the entire answer to #82 and the
    reason scan results are included at all, so a redaction that took the
    flags with the SSID would leave the probe pointless.
    """
    d = _diag(probes={"wpa_scan": _RAW_SCAN}, selected_ssid="Neptune-Media")
    rows = d["probes"]["wpa_scan"]
    assert len(rows) == 3, rows
    assert [r["flags"] for r in rows] == [
        "[WPA2-PSK-CCMP][ESS]", "[SAE-CCMP][ESS]", "[ESS]"]
    assert [r["freq"] for r in rows] == ["5180", "2437", "2412"]
    assert [r["network"] for r in rows] == ["network-1", "network-2", "network-3"]
    # Which one they were trying to join is unrecoverable once names are gone.
    assert [r["selected"] for r in rows] == [True, False, False]


def test_props_are_allowlisted_not_filtered():
    """A property nobody listed does not appear, whatever it is called."""
    d = _diag(probes={"props": _RAW_PROPS})
    props = d["probes"]["props"]
    assert props["ro.build.version.incremental"] == "272.6.8.0_user_680767620"
    assert props["ro.serialno"] == "G090LF1180570SPJ"
    assert "net.hostname" not in props
    assert "dhcp.wlan0.ipaddress" not in props


def test_serials_are_kept():
    """
    Same call as the support bundle: nothing correlates without them, and
    they identify the reporter's own hardware to them.
    """
    assert "G090LF1180570SPJ" in json.dumps(_diag(probes={"props": _RAW_PROPS}))


def test_an_unlisted_probe_is_dropped_whole():
    """
    The wizard posts from a browser talking to a device we know nothing about
    yet, so an unrecognised key is untrusted input, not a new feature.
    """
    d = _diag(probes={"logcat": "anything at all", "root": "uid=0(root)"})
    assert "logcat" not in d["probes"]
    assert d["probes"]["root"] == ["uid=0(root)"]


def test_missing_probes_are_named():
    """
    "The wizard did not ask" and "the device had nothing to say" need
    opposite next questions, so they must not look the same in the file.
    """
    d = _diag(probes={"root": "uid=0(root)"})
    assert "wpa_scan" in d["probes_missing"]
    assert "root" not in d["probes_missing"]


def test_a_probe_with_an_unexpected_shape_still_gets_scrubbed():
    """
    The parser is the thing most likely to be wrong about output nobody has
    seen, and these are by definition the devices behaving unusually. So the
    generic scrub runs regardless of whether a parser matched anything.
    """
    d = _diag(probes={"wpa_status": "garbage 10.10.1.188 aa:bb:cc:dd:ee:ff"})
    assert "10.10.1.188" not in json.dumps(d)
    assert "aa:bb:cc:dd:ee:ff" not in json.dumps(d)


def test_the_error_and_transcript_are_scrubbed_not_trusted():
    """
    Both are ours, but our error strings interpolate device output often
    enough that the distinction is not worth relying on.
    """
    d = _diag(error='Staged config does not contain ssid="Neptune-Media"',
              transcript=["Connected! IP: 10.10.1.188"])
    blob = json.dumps(d)
    assert "Neptune-Media" not in blob
    assert "10.10.1.188" not in blob


def test_no_transcripts_parameter_exists():
    """
    Same rule the support bundle has: speech must not become an opt-in,
    because a flag is a thing people tick. `transcript` here is the WIZARD's
    log, not speech, and there is no path for the latter.
    """
    params = set(inspect.signature(S.build_provision_diagnostics).parameters)
    assert "transcripts" not in params
    assert "stt_text" not in params
    assert "recordings" not in params


def test_the_wizard_probe_list_matches_the_allowlist():
    """
    `dashboard.jsx` collects the probes and `em_support` decides which survive,
    so the two lists must agree.

    Drift is silent in the direction that matters: a probe added to the wizard
    and not here is collected, posted, and dropped, so the operator waits for
    it, the file does not contain it, and nothing anywhere says so. The other
    direction just leaves a name in `probes_missing` forever.

    Parsed out of the JSX because the dashboard compiles to a single classic
    script with no module boundary to import across, the same reason
    CONFIG_SECTIONS is mirror-checked rather than shared.
    """
    jsx = (Path(__file__).resolve().parent.parent
           / "static" / "dashboard.jsx").read_text()
    block = re.search(r"const _PROVISION_PROBES = \{(.*?)\n  \};", jsx, re.S)
    assert block, "dashboard.jsx no longer defines _PROVISION_PROBES"
    in_jsx = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))
    in_py = set(S._PROVISION_PROBES)

    assert not (in_jsx - in_py), (
        f"the wizard collects {sorted(in_jsx - in_py)}, which em_support drops "
        f"on arrival — add them to _PROVISION_PROBES")
    assert not (in_py - in_jsx), (
        f"em_support allows {sorted(in_py - in_jsx)}, which the wizard never "
        f"collects")


def test_the_crown_wizard_probe_list_matches_the_allowlist():
    """
    Same contract as test_the_wizard_probe_list_matches_the_allowlist, for
    crown's separate, shorter allowlist — see _CROWN_PROVISION_PROBES's
    comment for why it isn't just biscuit's list.
    """
    jsx = (Path(__file__).resolve().parent.parent
           / "static" / "dashboard.jsx").read_text()
    block = re.search(r"const _CROWN_PROVISION_PROBES = \{(.*?)\n  \};", jsx, re.S)
    assert block, "dashboard.jsx no longer defines _CROWN_PROVISION_PROBES"
    in_jsx = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))
    in_py = set(S._CROWN_PROVISION_PROBES)

    assert not (in_jsx - in_py), (
        f"the crown wizard collects {sorted(in_jsx - in_py)}, which em_support "
        f"drops on arrival — add them to _CROWN_PROVISION_PROBES")
    assert not (in_py - in_jsx), (
        f"em_support allows {sorted(in_py - in_jsx)}, which the crown wizard "
        f"never collects")


def test_home_assistant_display_names_are_redacted():
    """
    Ingress login provisions accounts from Home Assistant's user record, so
    the user table now holds names that were never typed into EchoMuse —
    and they contain spaces, unlike every local username. A multi-word name
    reaching a public issue is the same leak as #62's, from a new source.
    """
    accounts = {"Wil Bowes": "admin", "wil": "admin"}
    out = S.sanitise_log([
        "Ingress login: Wil Bowes (admin)",
        "Shell session opened by wil",
    ], accounts=accounts)
    joined = " ".join(out)
    assert "Wil Bowes" not in joined
    assert "wil" not in joined.replace("<admin>", "")
    assert joined.count("<admin>") == 2
