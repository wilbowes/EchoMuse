"""
em_wifi: every SSID the standard allows, and every WPA2 passphrase.

The controller used to refuse any SSID or passphrase with a `"` or `\\` in
it, which are both valid; and it could not name an SSID that is not UTF-8.
"""
import pytest

import em_wifi


@pytest.mark.parametrize("name", [
    "My Home WiFi", " padded ", "Bob's", 'say "hi"', "back\\slash", "Café 🏠",
])
def test_every_valid_ssid_is_accepted(name):
    assert em_wifi.problem(em_wifi.ssid_bytes(name), "12345678") is None


def test_ssid_hex_names_bytes_that_are_not_text():
    b = em_wifi.ssid_bytes("shown as �", "ff01")
    assert b == b"\xff\x01"
    assert em_wifi.problem(b, "") is None


def test_ssid_hex_wins_over_the_display_name():
    assert em_wifi.ssid_bytes("Caf\\xc3\\xa9", "436166c3a9") == "Café".encode()


def test_malformed_ssid_hex_is_refused_not_ignored():
    with pytest.raises(ValueError):
        em_wifi.ssid_bytes("net", "abc")
    with pytest.raises(ValueError):
        em_wifi.ssid_bytes("net", "zz")


@pytest.mark.parametrize("ssid,why", [
    (b"", "empty"), (b"\x00\x00", "empty"), (b"x" * 33, "32"),
])
def test_ssid_limits(ssid, why):
    assert why in em_wifi.problem(ssid, "")


@pytest.mark.parametrize("psk", [
    "", "12345678", "p" * 63, 'pa"ss\\word\'!', "has spaces in it", "AB" * 32,
])
def test_valid_passphrases(psk):
    assert em_wifi.problem(b"net", psk) is None


@pytest.mark.parametrize("psk", [
    "short", "p" * 64, "has\nnewline", "café-pass", "tab\there!",
])
def test_invalid_passphrases(psk):
    assert em_wifi.problem(b"net", psk)


def test_the_endpoint_uses_em_wifi_and_forwards_the_bytes():
    """Source shape: em_api cannot be imported by this suite."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "em_api.py").read_text()
    start = src.index("async def _post_device_wifi(")
    body = src[start:src.index("\nasync def ", start + 1)]
    assert "em_wifi.problem(em_wifi.ssid_bytes(ssid, ssid_hex), psk)" in body
    assert 'change["ssid_hex"] = ssid_hex' in body
    assert '(\'"\', "\\\\")' not in body, "the old quote/backslash refusal is back"
