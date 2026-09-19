"""
emos/device/em-wifi — the console WiFi tool on emOS — run for real.

Its scan parsing and password check are awk inside a shell script, so they are
lifted out and run against wpa_cli-shaped fixtures. It used to split
scan_results on whitespace, which cut "My Home WiFi" to "My", and wrote the
SSID and password into the conf unescaped.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "emos" / "device" / "em-wifi"
AWK = shutil.which("awk")
pytestmark = pytest.mark.skipif(AWK is None, reason="no awk on this host")


def _scan_awk() -> str:
    s = SCRIPT.read_text()
    start = s.index("scan_results 2>/dev/null | awk '") + len("scan_results 2>/dev/null | awk '")
    return s[start:s.index("' | sort -rn | head -20", start)]


def _psk_awk() -> str:
    m = re.search(r"awk '(\{ exit !\(length.*?\})'", SCRIPT.read_text())
    assert m, "em-wifi no longer checks the passphrase with awk"
    return m.group(1)


SCAN = (
    "bssid / frequency / signal level / flags / ssid\n"
    "aa:aa\t2412\t-40\t[WPA2-PSK-CCMP][ESS]\tMy Home WiFi\r\n"
    "bb:bb\t2437\t-50\t[WPA2-PSK-CCMP][ESS]\tCaf\\xc3\\xa9\n"
    "cc:cc\t5180\t-60\t[WPA2-PSK-CCMP][ESS]\tBob's \\\"Net\\\" \\\\ x\n"
    "dd:dd\t2462\t-70\t[ESS]\t\\x00\\x00\n"
    "ee:ee\t2412\t-45\t[WPA2-PSK-CCMP][ESS]\tMy Home WiFi\n"
    "ff:ff\t2412\t-80\t[ESS]\t\n"
    "gg:gg\t2412\t-55\t[ESS]\t padded \n"
)


def test_scan_gives_the_exact_bytes_of_every_ssid():
    out = subprocess.run([AWK, _scan_awk()], input=SCAN, capture_output=True,
                         text=True, check=True).stdout
    rows = [line.split("\t") for line in out.splitlines()]
    by_hex = {r[2]: r for r in rows}
    for name in ["My Home WiFi", "Café", 'Bob\'s "Net" \\ x', " padded "]:
        assert name.encode().hex() in by_hex, f"{name!r} not decoded exactly: {rows}"
    assert len(rows) == 4, f"hidden, empty and duplicate SSIDs must be dropped: {rows}"


@pytest.mark.parametrize("psk,ok", [
    ("12345678", True), ('pa"ss\\word\'!', True), (" spaced pass ", True),
    ("short", False), ("p" * 64, False), ("café-pass", False), ("tab\there!", False),
])
def test_passphrase_check(psk, ok):
    r = subprocess.run([AWK, _psk_awk()], input=psk + "\n", capture_output=True, text=True)
    assert (r.returncode == 0) is ok


def test_the_conf_gets_hex_and_a_raw_read_password():
    s = SCRIPT.read_text()
    assert "printf '        ssid=%s\\n' \"$ssid_hex\"" in s
    assert "IFS= read -r psk" in s
    assert 'echo "        ssid=' not in s, "the SSID is written unescaped again"
