"""FireOS 5 to emOS: the WiFi pre-flight (em_emos_migrate)."""
import em_emos_migrate as m

# The shape of a conf FireOS 5's own framework rewrote, read off VVV
# 2026-11-10 with the SSID and PSK replaced. CRLF as it arrives through a pty.
FIREOS_CONF = (
    "ctrl_interface=/data/misc/wifi/sockets\r\n"
    "driver_param=use_p2p_group_interface=1\r\n"
    "update_config=1\r\n"
    "device_name=csm_biscuit\r\n"
    "manufacturer=Amazon\r\n"
    "p2p_no_group_iface=1\r\n"
    "external_sim=1\r\n"
    "wowlan_triggers=disconnect\r\n"
    "\r\n"
    "network={\r\n"
    '\tssid="Home Net"\r\n'
    "\tscan_ssid=1\r\n"
    '\tpsk="not-the-real-one"\r\n'
    "\tkey_mgmt=WPA-PSK\r\n"
    "\tpriority=6\r\n"
    "\tlast_connect_time=1794113186\r\n"
    "}\r\n"
)

STATUS = (
    "bssid=00:11:22:33:44:55\nfreq=5785\nssid=Home Net\nid=0\n"
    "mode=station\npairwise_cipher=CCMP\ngroup_cipher=CCMP\n"
    "key_mgmt=WPA2-PSK\nwpa_state=COMPLETED\n"
)

SCAN = (
    "bssid / frequency / signal level / flags / ssid\n"
    "00:11:22:33:44:55\t5785\t-50\t[WPA2-PSK-CCMP][ESS]\tHome Net\n"
    "00:11:22:33:44:56\t2437\t-60\t[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP+TKIP][ESS]\tHome Net\n"
    "66:77:88:99:aa:bb\t2412\t-70\t[WPA-PSK-TKIP][ESS]\tNeighbour\n"
)


# The same vectors as device/internal/wifi/ssid_test.go, so the two decoders
# can only agree.
UNESCAPE = [
    ("plain", b"plain"),
    ("My Home WiFi", b"My Home WiFi"),
    (" padded ", b" padded "),
    ("Bob's", b"Bob's"),
    (r"say \"hi\"", b'say "hi"'),
    (r"back\\slash", b"back\\slash"),
    (r"Caf\xc3\xa9", "Café".encode()),
    (r"\xf0\x9f\x8f\xa0 home", "🏠 home".encode()),
    (r"tab\there", b"tab\there"),
    (r"\e\n\r", b"\x1b\n\r"),
    (r"\xff\xfe", b"\xff\xfe"),
    (r"\x00\x00", b"\x00\x00"),
    ("trailing\\", b"trailing\\"),
    (r"bad\xZZ", b"bad\\xZZ"),
]


def test_unescape_matches_the_firmware_vectors():
    for given, want in UNESCAPE:
        assert m.unescape_ssid(given) == want, given


def test_conf_ssid_takes_all_three_forms():
    assert m.conf_ssid('"Home Net"') == b"Home Net"
    # Quoted ends at the LAST quote, so quotes and backslashes inside are
    # literal (wpa_config_parse_string).
    assert m.conf_ssid('"say "hi""') == b'say "hi"'
    assert m.conf_ssid(r'"back\slash"') == b"back\\slash"
    assert m.conf_ssid('"Café"') == "Café".encode()
    assert m.conf_ssid('""') == b""
    assert m.conf_ssid(r'P"tab\there"') == b"tab\there"
    assert m.conf_ssid(r'P"\xff"') == b"\xff"
    assert m.conf_ssid("486f6d65") == b"Home"
    assert m.conf_ssid("486F6D65") == b"Home"
    assert m.conf_ssid("00" * 32) == b"\x00" * 32
    # None of the forms: unterminated quote, odd hex, not hex.
    assert m.conf_ssid('"Home') is None
    assert m.conf_ssid('P"Home') is None
    assert m.conf_ssid("486") is None
    assert m.conf_ssid("Home") is None
    assert m.conf_ssid("") is None


def test_parse_conf_reads_fireos_own_conf():
    nets = m.parse_conf_networks(FIREOS_CONF)
    assert nets == [{"ssid": b"Home Net", "disabled": False,
                     "key_mgmt": "WPA-PSK"}]


def test_parse_conf_comments_disabled_and_several_blocks():
    conf = (
        "# a comment\n"
        "network={\n  ssid=\"A\"\n  disabled=1\n}\n"
        "network={\n  # ssid=\"commented\"\n  ssid=42\n  disabled=0\n}\n"
        "network={\n  ssid=\"unterminated\n}\n"
        "network={\n  ssid=\"cut off\"\n"            # no closing brace
    )
    nets = m.parse_conf_networks(conf)
    assert [(n["ssid"], n["disabled"]) for n in nets] == [
        (b"A", True), (b"B", False), (None, False)]


def test_status_and_scan_decode_ssids():
    st = m.parse_status("ssid=Caf\\xc3\\xa9\r\nwpa_state=COMPLETED\r\n")
    assert st["ssid"] == "Café".encode() and st["wpa_state"] == "COMPLETED"
    rows = m.parse_scan(SCAN + "aa:aa:aa:aa:aa:aa\t2412\t-80\t[ESS]\t\n")
    assert rows[0] == ("[WPA2-PSK-CCMP][ESS]", b"Home Net")
    assert rows[-1] == ("[ESS]", b"")


def test_wpa_only_flags():
    assert m.wpa_only("[WPA-PSK-CCMP][ESS]")
    assert m.wpa_only("[WPA-PSK-TKIP]")
    assert not m.wpa_only("[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP+TKIP][ESS]")
    assert not m.wpa_only("[WPA2-PSK-CCMP][ESS]")
    assert not m.wpa_only("[RSN-SAE-CCMP][MESH]")
    assert not m.wpa_only("[ESS]")


def test_a_healthy_fireos_echo_passes():
    # The neighbour's WPA-only AP is not ours to worry about.
    assert m.wifi_problems(FIREOS_CONF, STATUS, SCAN) == []


def test_refuses_when_not_associated():
    st = STATUS.replace("COMPLETED", "SCANNING")
    [p] = m.wifi_problems(FIREOS_CONF, st, SCAN)
    assert "not connected" in p and "SCANNING" in p
    [p] = m.wifi_problems(FIREOS_CONF, "", SCAN)
    assert "unknown" in p


def test_refuses_when_the_network_is_not_in_the_conf():
    conf = FIREOS_CONF.replace('"Home Net"', '"Other"')
    [p] = m.wifi_problems(conf, STATUS, SCAN)
    assert "no entry" in p


def test_matches_by_bytes_not_by_text():
    # A hex conf entry for the same bytes is the same network; a UTF-8 name
    # that only looks alike is not.
    conf = FIREOS_CONF.replace('"Home Net"', b"Home Net".hex())
    assert m.wifi_problems(conf, STATUS, SCAN) == []
    st = STATUS.replace("ssid=Home Net", r"ssid=Caf\xc3\xa9")
    conf = FIREOS_CONF.replace('"Home Net"', '"Cafe"')
    [p] = m.wifi_problems(conf, st, "")
    assert "no entry" in p


def test_refuses_when_every_matching_block_is_disabled():
    conf = FIREOS_CONF.replace("\tpriority=6", "\tdisabled=1")
    [p] = m.wifi_problems(conf, STATUS, SCAN)
    assert "disabled" in p
    # One enabled block among disabled ones is enough.
    conf2 = conf + 'network={\n\tssid="Home Net"\n\tkey_mgmt=WPA-PSK\n}\n'
    assert m.wifi_problems(conf2, STATUS, SCAN) == []


def test_refuses_a_wpa_only_access_point_on_this_network():
    scan = SCAN + "00:11:22:33:44:57\t2412\t-75\t[WPA-PSK-CCMP][ESS]\tHome Net\n"
    [p] = m.wifi_problems(FIREOS_CONF, STATUS, scan)
    assert "#1144" in p
    # Associated over WPA1 right now, whatever the scan shows.
    st = STATUS.replace("WPA2-PSK", "WPA-PSK")
    [p] = m.wifi_problems(FIREOS_CONF, st, "")
    assert "#1144" in p
