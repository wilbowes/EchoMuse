"""
FireOS 5 to emOS over the network: the WiFi pre-flight.

Pure, like em_emos_update's verdicts: the shell transport gathers the text and
this decides. Nothing here writes anything.

WHY THERE IS NO WIFI WRITER. Every FireOS-native Echo is amonet 1 / FireOS 5
(the FireOS flow refuses amonet 2). A FireOS 5 emOS image ships no /sbin
userspace, so emOS runs Amazon's own /system/bin/wpa_supplicant, and init's
wpa_conf() falls back to Android's /data/misc/wifi/wpa_supplicant.conf when
/data/emos/wpa.conf does not exist. The same binary that wrote the conf reads
it, so FireOS's own fields (p2p_no_group_iface, last_connect_time, ...) are
safe there — unlike on FireOS 6, where emOS runs our 2.10 build and drops the
block. Measured on C95, 2026-11-10: no /data/emos/wpa.conf, Amazon's
supplicant running on Android's conf.

So the pre-flight only has to prove that Android's conf will take emOS back to
the network the Echo is on now:

- the Echo is associated, and its network has a block in the conf that the
  supplicant will use (not `disabled=1`);
- no BSS of that network is WPA-only. #1144 (a kernel panic joining a WPA-only
  AP) was seen on FireOS 6's kernel with our supplicant, not on FireOS 5's,
  but a refusal costs a user one message and a panic loop costs a cable.

A stale /data/emos/wpa.conf (an Echo that ran emOS before and went back) would
be preferred over Android's; the migration moves it aside, which is a write
and so not decided here.

wpa_supplicant's own grammar, hostap 2.x:
- wpa_cli prints SSIDs through printf_encode; unescape_ssid reverses it and
  mirrors device/internal/wifi/ssid.go UnescapeSSID, vector for vector.
- A conf `ssid=` is `"quoted"` (bytes up to the LAST quote, literal),
  `P"printf"` (printf_decode), or hex (wpa_config_parse_string).
"""
import re

_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_SIMPLE = {"\\": 0x5C, '"': 0x22, "e": 0x1B, "n": 0x0A, "r": 0x0D, "t": 0x09}


def unescape_ssid(s: str) -> bytes:
    """wpa_cli's printf_encode output back to the SSID bytes. A malformed
    escape is kept literally, as the firmware's UnescapeSSID does."""
    raw = s.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c != 0x5C or i + 1 >= len(raw):
            out.append(c)
            i += 1
            continue
        n = chr(raw[i + 1])
        if n in _SIMPLE:
            out.append(_SIMPLE[n])
            i += 2
        elif n == "x" and i + 3 < len(raw) and _HEX.fullmatch(
                raw[i + 2:i + 4].decode("latin-1")):
            out.append(int(raw[i + 2:i + 4], 16))
            i += 4
        else:
            out.append(c)
            i += 1
    return bytes(out)


def conf_ssid(value: str):
    """A conf `ssid=` value to bytes, or None when it is none of the three
    forms wpa_supplicant accepts."""
    if value.startswith('"'):
        end = value.rfind('"')
        if end <= 0:
            return None
        return value[1:end].encode("utf-8", "surrogateescape")
    if value.startswith('P"'):
        end = value.rfind('"')
        if end <= 1:
            return None
        return unescape_ssid(value[2:end])
    if _HEX.fullmatch(value):
        return bytes.fromhex(value)
    return None


def _lines(text: str):
    """Conf lines as wpa_config_get_line sees them: whitespace trimmed both
    ends, blank lines and `#` comments dropped. CR is whitespace, so a conf
    read through a pty parses the same."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def parse_conf_networks(text: str) -> list:
    """Each `network={...}` block: `ssid` (bytes, or None if unparseable),
    `disabled` (only `disabled=1` counts, as in hostap) and `key_mgmt`."""
    nets, cur = [], None
    for line in _lines(text):
        if cur is None:
            if line == "network={":
                cur = {"ssid": None, "disabled": False, "key_mgmt": None}
            continue
        if line == "}":
            nets.append(cur)
            cur = None
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "ssid":
            cur["ssid"] = conf_ssid(value)
        elif key == "disabled":
            cur["disabled"] = value == "1"
        elif key == "key_mgmt":
            cur["key_mgmt"] = value
    return nets


def parse_status(text: str) -> dict:
    """`wpa_cli status`: every `key=value`, with `ssid` as bytes."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.rstrip("\r").partition("=")
        if sep:
            out[key] = value
    if "ssid" in out:
        out["ssid"] = unescape_ssid(out["ssid"])
    return out


def parse_scan(text: str) -> list:
    """`wpa_cli scan_results` rows as (flags, ssid bytes). Tab-separated; the
    SSID is the fifth column and may itself be empty."""
    rows = []
    for line in text.splitlines():
        cols = line.rstrip("\r").split("\t")
        if len(cols) < 4 or cols[0].startswith("bssid"):
            continue
        rows.append((cols[3], unescape_ssid(cols[4]) if len(cols) > 4 else b""))
    return rows


def wpa_only(flags: str) -> bool:
    """A BSS that offers WPA (v1) and no RSN: `[WPA-PSK-CCMP]`, never
    `[WPA-PSK-CCMP][WPA2-PSK-CCMP]`."""
    return "[WPA-" in flags and "[WPA2-" not in flags and "[RSN-" not in flags


def wifi_problems(conf_text: str, status_text: str, scan_text: str) -> list:
    """Why emOS might not get this Echo back on its network, or []."""
    st = parse_status(status_text)
    if st.get("wpa_state") != "COMPLETED" or not st.get("ssid"):
        return ["This Echo is not connected to a WiFi network right now "
                f"(state {st.get('wpa_state') or 'unknown'})."]
    ssid = st["ssid"]
    problems = []

    nets = parse_conf_networks(conf_text)
    matching = [n for n in nets if n["ssid"] == ssid]
    if not matching:
        problems.append(
            "FireOS's WiFi settings have no entry for the network this Echo "
            "is on, so emOS would not find it.")
    elif all(n["disabled"] for n in matching):
        problems.append(
            "FireOS's WiFi settings mark the network this Echo is on as "
            "disabled, so emOS would not join it.")

    if st.get("key_mgmt") == "WPA-PSK" or any(
            s == ssid and wpa_only(f) for f, s in parse_scan(scan_text)):
        problems.append(
            "An access point on this network offers only WPA (not WPA2). "
            "Switch it to WPA2 first (#1144).")
    return problems
