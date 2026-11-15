"""
The Echo's temperatures as Home Assistant sensors, the way ambient light is.

The wiring is read as source through _sources, so a function moving to
another module cannot leave a guard passing on nothing. The push is run for
real where zeroconf is installed.

- The entities exist only on firmware announcing `thermal`. Older firmware
  reports the same stats fields, so the push has to check as well.
- They read the chip, not the room: diagnostics, disabled by default.
- "Hottest Zone", not "Max": it is the hottest zone now, not a running peak.
- Keys are append-only: HA keys its registry on them.
- A reading the device could not take is missing_state, never 0.
"""

import re
from pathlib import Path

import pytest

import _sources

DEVICE = Path(__file__).resolve().parents[2] / "device"


def _thermal_entities() -> str:
    """The ListEntities block that advertises the temperature sensors."""
    code = _sources.all_code()
    assert code.count("self._thermal_capable:") == 1
    gate = code.index("self._thermal_capable:")
    start = code.rindex("isinstance(msg, api_pb2.ListEntitiesRequest)", 0, gate)
    return code[start:code.index("ListEntitiesDoneResponse()", gate)]


def test_the_keys_are_appended():
    code = _sources.all_code()
    assert re.search(r"^CPU_TEMP_KEY\s*=\s*5$", code, re.M)
    assert re.search(r"^MAX_TEMP_KEY\s*=\s*6$", code, re.M)


def test_the_entities_are_gated_on_the_capability():
    entities = _thermal_entities()
    gate = entities.index("if self._thermal_capable:")
    assert gate < entities.index("CPU_TEMP_KEY") < entities.index("MAX_TEMP_KEY")
    assert "device_class='temperature'" in entities
    assert "unit_of_measurement='°C'" in entities


def test_the_entities_are_diagnostics_disabled_by_default():
    sensors = _thermal_entities().split("if self._thermal_capable:")[1]
    assert "entity_category=api_pb2.ENTITY_CATEGORY_DIAGNOSTIC" in sensors
    assert "disabled_by_default=True" in sensors


def test_the_second_sensor_is_the_hottest_zone():
    entities = _thermal_entities()
    assert "'hottest_zone_temperature', MAX_TEMP_KEY, 'Hottest Zone Temperature'" \
        in entities
    assert "Max Temperature" not in entities


def test_the_firmware_announces_what_the_controller_checks():
    hits = [p for p in DEVICE.rglob("*.go")
            if "func capabilities() []string" in p.read_text()]
    assert len(hits) == 1, hits
    src = hits[0].read_text()
    start = src.index("func capabilities() []string")
    assert '"thermal"' in src[start:src.index("\n}\n", start)]


def test_a_missing_reading_is_never_sent_as_zero():
    push = _sources.code_only(_sources.def_source("update_temperatures"))
    assert "missing_state=celsius is None" in push


def test_the_push_checks_the_capability():
    """Old firmware sends cpuTempC with no entity on the other end."""
    push = _sources.code_only(_sources.def_source("update_temperatures"))
    assert "_thermal_capable" in push


def test_the_stats_report_reaches_the_push():
    stats = _sources.code_only(_sources.def_source("_on_stats"))
    assert "esphome.update_temperatures(device_id, msg.get('cpuTempC'), " \
           "msg.get('maxTempC'))" in stats


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
