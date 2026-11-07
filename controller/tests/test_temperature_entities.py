"""
The Echo's temperatures as Home Assistant sensors, the way ambient light is.

The wiring is read as source, like test_mic_muted_and_wake_word.py. The push
is run for real where zeroconf is installed.

- The entities exist only on firmware announcing `thermal`. Older firmware
  reports the same stats fields, so the push has to check as well.
- Keys are append-only: HA keys its registry on them.
- A reading the device could not take is missing_state, never 0.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
ESPHOME = ROOT / "controller" / "em_esphome.py"
CONTROLLER = ROOT / "controller" / "em_controller.py"
CONTROL_GO = ROOT / "device" / "internal" / "client" / "control.go"


def _block(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_the_keys_are_appended():
    src = ESPHOME.read_text()
    assert re.search(r"^CPU_TEMP_KEY\s*=\s*5\b", src, re.M)
    assert re.search(r"^MAX_TEMP_KEY\s*=\s*6\b", src, re.M)


def test_the_entities_are_gated_on_the_capability():
    src = ESPHOME.read_text()
    entities = _block(src, "isinstance(msg, api_pb2.ListEntitiesRequest)",
                      "ListEntitiesDoneResponse()")
    gate = entities.index("if self._thermal_capable:")
    assert gate < entities.index("CPU_TEMP_KEY") < entities.index("MAX_TEMP_KEY")
    assert 'device_class="temperature"' in entities
    assert 'unit_of_measurement="°C"' in entities


def test_the_firmware_announces_what_the_controller_checks():
    assert '"thermal"' in _block(CONTROL_GO.read_text(),
                                 "func capabilities() []string", "\n}\n")


def test_a_missing_reading_is_never_sent_as_zero():
    push = _block(ESPHOME.read_text(), "def update_temperatures(", "\ndef ")
    assert "missing_state=celsius is None" in push


def test_the_push_checks_the_capability():
    """Old firmware sends cpuTempC with no entity on the other end."""
    push = _block(ESPHOME.read_text(), "def update_temperatures(", "\ndef ")
    assert "_thermal_capable" in push


def test_the_stats_report_reaches_the_push():
    stats = _block(CONTROLLER.read_text(), "async def _on_stats(",
                   "async def _on_playback_stats(")
    assert 'esphome.update_temperatures(device_id, msg.get("cpuTempC"), ' \
           'msg.get("maxTempC"))' in stats


class _Satellite:
    def __init__(self, capable: bool):
        self._thermal_capable = capable
        self.sent = []

    def _send_one(self, msg):
        self.sent.append(msg)


class _Server:
    def __init__(self, satellite):
        self.satellite = satellite

    def get_satellite(self):
        return self.satellite


@pytest.fixture
def esphome(monkeypatch):
    pytest.importorskip("zeroconf")
    import em_esphome
    monkeypatch.setattr(em_esphome, "_servers", {})
    return em_esphome


def test_both_readings_reach_their_sensors(esphome):
    sat = _Satellite(capable=True)
    esphome._servers["dev"] = _Server(sat)
    esphome.update_temperatures("dev", 41.5, 52)
    assert [(m.key, m.state, m.missing_state) for m in sat.sent] == [
        (esphome.CPU_TEMP_KEY, 41.5, False),
        (esphome.MAX_TEMP_KEY, 52.0, False),
    ]


def test_a_reading_of_zero_is_a_reading(esphome):
    sat = _Satellite(capable=True)
    esphome._servers["dev"] = _Server(sat)
    esphome.update_temperatures("dev", 0.0, None)
    assert [m.missing_state for m in sat.sent] == [False, True]


def test_firmware_without_the_capability_sends_nothing(esphome):
    sat = _Satellite(capable=False)
    esphome._servers["dev"] = _Server(sat)
    esphome.update_temperatures("dev", 41.5, 52.0)
    assert sat.sent == []


def test_no_connection_is_not_an_error(esphome):
    esphome.update_temperatures("nobody", 41.5, 52.0)
    esphome._servers["dev"] = _Server(None)
    esphome.update_temperatures("dev", 41.5, 52.0)
