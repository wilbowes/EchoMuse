"""
An Echo's diag snapshot: asked for only when the firmware announces it, never
answered by another Echo, and redacted before it reaches a bundle.
"""

import asyncio
import json

import em_diag_snapshot as D
import em_support as S

SSID = "Bowes-Family-5G"
BSSID = "d6:9b:22:11:aa:bb"
IP = "10.10.1.104"


class Live:
    def __init__(self, device_id="A", caps=("mic", "diag_snapshot"), answer=True):
        self.device_id = device_id
        self.capabilities = list(caps)
        self.answer = answer
        self.sent = []

    async def send_control(self, msg):
        self.sent.append(msg)
        if self.answer:
            await D.on_snapshot(self, {"type": "diag_snapshot", "id": msg["id"],
                                       "snapshot": {"board": {"id": "biscuit"}}})


def _run(coro):
    return asyncio.run(coro)


def test_a_capable_echo_is_asked_and_answers():
    live = Live()
    out = _run(D.request(live))
    assert out == {"status": "ok", "snapshot": {"board": {"id": "biscuit"}}}
    assert live.sent[0]["type"] == "diag_request"
    assert not D._waiting


def test_older_firmware_is_not_asked():
    live = Live(caps=("mic",))
    assert _run(D.request(live)) == {"status": "unsupported"}
    assert live.sent == []


def test_offline_and_timeout():
    assert _run(D.request(None)) == {"status": "offline"}
    assert _run(D.request(Live(answer=False), timeout=0.01)) == {"status": "timeout"}
    assert not D._waiting


def test_one_echo_cannot_answer_for_another():
    async def go():
        target = Live("A", answer=False)
        task = asyncio.create_task(D.request(target, timeout=0.05))
        await asyncio.sleep(0)
        (rid,) = D._waiting
        await D.on_snapshot(Live("B"), {"id": rid, "snapshot": {"forged": True}})
        return await task
    assert _run(go()) == {"status": "timeout"}


def test_a_reply_nobody_asked_for_is_ignored():
    # A late answer to a request that already timed out, or a made-up id.
    _run(D.on_snapshot(Live(), {"id": "stale", "snapshot": {}}))
    assert not D._waiting


def _raw():
    return {"status": "ok", "snapshot": {
        "board": {"id": "biscuit", "notes": [f"wlan at {IP}"], "problems": []},
        "idme": {"device_type_id": "A3S5BH2HU6VAYF", "alscal": "ams_0_0 1 2",
                 "serial": "G090SECRET", "mac_addr": BSSID},
        "mixer": [{"name": "DIF1 Switch", "want": "On", "got": "Off", "match": False}],
        "files": {"/proc/asound/pcm": "00-00: MultiMedia1", "/etc/passwd": "root"},
        "kernel": ["tas2770: probe deferred", f"wlan: joined {SSID}",
                   f"snd: card for {SSID} ready"],
        "wpaStatus": f"bssid={BSSID}\nfreq=5805\nssid={SSID}\nip_address={IP}\n"
                     "wpa_state=COMPLETED\nkey_mgmt=WPA2-PSK\n",
        "netLog": [f"wlan0: Trying to associate with SSID '{SSID}'",
                   f"dhcpcd: leased {IP}"],
        "errors": {"kernel": "EPERM", "unknown": "x"},
    }}


def test_the_snapshot_leaks_nothing_identifying():
    out = S.redact_snapshot(_raw(), SSID)
    text = json.dumps(out)
    for secret in (SSID, BSSID, IP, "G090SECRET", "root"):
        assert secret not in text, secret


def test_the_snapshot_keeps_what_explains_a_fault():
    out = S.redact_snapshot(_raw(), SSID)
    assert out["board"]["id"] == "biscuit"
    assert out["idme"] == {"device_type_id": "A3S5BH2HU6VAYF", "alscal": "ams_0_0 1 2"}
    assert out["mixer"] == [{"name": "DIF1 Switch", "want": "On", "got": "Off",
                             "match": False}]
    assert out["files"] == {"/proc/asound/pcm": ["00-00: MultiMedia1"]}
    assert out["kernel"][0] == "tas2770: probe deferred"
    assert out["wpa_status"] == {"freq": "5805", "wpa_state": "COMPLETED",
                                 "key_mgmt": "WPA2-PSK"}
    assert out["net_log"][1] == "dhcpcd: leased <ip>"
    assert out["errors"] == {"kernel": "EPERM"}


def test_the_ssid_is_caught_even_where_no_parser_expects_it():
    # The kernel line names the network in prose the device's filter passed.
    out = S.redact_snapshot(_raw(), SSID)
    assert "snd: card for <network> ready" in out["kernel"]


def test_without_the_ssid_the_parsers_still_hold():
    out = S.redact_snapshot(_raw(), None)
    assert SSID not in json.dumps(out["wpa_status"]) + json.dumps(out["net_log"])


def test_a_short_ssid_does_not_mangle_the_snapshot():
    out = S.redact_snapshot(_raw(), "On")
    assert out["mixer"][0]["want"] == "On"


def test_unsupported_and_garbage_pass_as_status_only():
    assert S.redact_snapshot({"status": "unsupported"}) == {"status": "unsupported"}
    assert S.redact_snapshot({"status": "ok", "snapshot": "junk"}) == {"status": "ok"}
    assert S.redact_snapshot(None) is None
