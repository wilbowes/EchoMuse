"""em_endpoints: the controller address list, from the edges of what a device
can dial (RFC 1123 host names, IP literals, TCP ports)."""

from pathlib import Path

import pytest

import em_endpoints as e

FIXTURE = (Path(__file__).resolve().parents[2]
           / "device/internal/discovery/testdata/controller_managed.json")


@pytest.mark.parametrize("host", [
    "10.20.40.110", "0.0.0.0", "255.255.255.255",
    "::1", "fe80::1", "fe80::1%wlan0", "2001:db8::8a2e:370:7334",
    "controller", "echomuse-controller", "controller.example.internal",
    "controller.example.internal.",            # rooted name
    "a", "9controller", "1-2.example",          # RFC 1123 allows a leading digit
    "a" * 63 + ".example",                      # longest label
    ".".join(["a" * 63] * 3) + "." + "a" * 61,  # 253 characters
    "xn--bcher-kva.example",                    # IDN in its ASCII form
])
def test_hosts_a_device_can_dial(host):
    assert e.host_problem(host) is None


@pytest.mark.parametrize("host", [
    "", " ", "a" * 64 + ".example",             # label over 63
    ".".join(["a" * 63] * 3) + "." + "a" * 62,  # 254 characters
    "-controller", "controller-", "ctl.-x.example",
    "under_score.example", "has space", "ctl..example", ".example",
    "10.10.1.300", "999.1.1.1", "10.10.1",       # a mistyped IP is not a name
    "fe80::zz", "1:2:3:4:5:6:7:8:9",
    "bücher.example",                           # must be sent as xn--
    "http://10.0.0.1", "10.0.0.1:8767",
])
def test_hosts_refused(host):
    assert e.host_problem(host) is not None


def test_ports_default_to_the_controllers_own():
    eps, err = e.normalise([{"host": "10.0.0.2"}], 8767, 8770)
    assert err is None and eps == [{"host": "10.0.0.2", "port": 8767, "tlsPort": 8770}]


@pytest.mark.parametrize("port,ok", [
    (1, True), (65535, True), ("8767", True), (8767.0, True),
    (0, False), (65536, False), (-1, False), (80.5, False), ("8o", False), (True, False),
])
def test_port_edges(port, ok):
    _, err = e.normalise([{"host": "h", "port": port}], 8767, 8770)
    assert (err is None) == ok


@pytest.mark.parametrize("tls,ok", [(0, True), (1, True), (65535, True), (65536, False), (-1, False)])
def test_tls_port_zero_means_plain(tls, ok):
    _, err = e.normalise([{"host": "h", "tlsPort": tls}], 8767, 8770)
    assert (err is None) == ok


def test_bracketed_ipv6_is_stored_bare():
    eps, _ = e.normalise([{"host": "[fe80::1]"}], 8767, 0)
    assert eps[0]["host"] == "fe80::1"


def test_duplicates_and_length():
    _, err = e.normalise([{"host": "A.example"}, {"host": "a.example."}], 8767, 0)
    assert "twice" in err
    _, err = e.normalise([{"host": f"h{i}"} for i in range(e.MAX_ENDPOINTS + 1)], 8767, 0)
    assert err
    assert e.normalise(None, 1, 1) == ([], None)
    assert e.normalise("10.0.0.1", 1, 1)[1]


def test_empty_list_writes_nothing():
    assert e.file_bytes([]) is None


def test_file_is_marked_and_leaves_mdns_on():
    import json
    doc = json.loads(e.file_bytes([{"host": "h", "port": 1, "tlsPort": 0}]))
    assert doc[e.MANAGED_KEY] == e.MANAGED_BY
    assert "mdns" not in doc  # absent = on, in the firmware (static.go)
    assert doc["endpoints"] == [{"host": "h", "port": 1, "tls_port": 0}]


def test_go_fixture_is_what_the_controller_writes():
    eps, err = e.normalise([{"host": "10.20.40.110"},
                            {"host": "fe80::1", "port": 9000, "tlsPort": 0},
                            {"host": "controller.example.internal."}], 8767, 8770)
    assert err is None
    assert FIXTURE.read_bytes() == e.file_bytes(eps), (
        "regenerate device/internal/discovery/testdata/controller_managed.json")


def test_device_path_matches_the_firmware():
    src = (Path(__file__).resolve().parents[2] / "device/internal/discovery/static.go").read_text()
    assert f'StaticControllerPath = "{e.DEVICE_PATH}"' in src
