"""The stall watchdog is off unless EM_LOOP_WATCHDOG is set."""

import asyncio
import logging
import time

import em_ota
import em_stallwatch
import em_state


def test_the_watchdog_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EM_LOOP_WATCHDOG", raising=False)
    assert em_stallwatch.enabled() is False
    monkeypatch.setenv("EM_LOOP_WATCHDOG", "0")
    assert em_stallwatch.enabled() is False
    monkeypatch.setenv("EM_LOOP_WATCHDOG", "1")
    assert em_stallwatch.enabled() is True


def test_arm_is_a_noop_while_the_option_is_off(monkeypatch):
    import faulthandler
    monkeypatch.delenv("EM_LOOP_WATCHDOG", raising=False)
    called = []
    monkeypatch.setattr(
        faulthandler, "dump_traceback_later",
        lambda *args, **kwargs: called.append(args))
    em_stallwatch.arm(0.2)
    em_stallwatch.disarm()
    assert called == []


def test_a_stall_over_a_second_dumps_the_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("EM_LOOP_WATCHDOG", "1")
    path = tmp_path / "tb.txt"
    handle = path.open("w")
    try:
        em_stallwatch.arm(0.15, file=handle)
        time.sleep(0.4)
    finally:
        em_stallwatch.disarm()
        handle.close()
    text = path.read_text()
    assert "File" in text or "Thread" in text


def test_disarm_cancels_a_dump_that_has_not_fired(tmp_path, monkeypatch):
    monkeypatch.setenv("EM_LOOP_WATCHDOG", "1")
    path = tmp_path / "tb.txt"
    handle = path.open("w")
    try:
        em_stallwatch.arm(2.0, file=handle)
        em_stallwatch.disarm()
        time.sleep(0.2)
    finally:
        em_stallwatch.disarm()
        handle.close()
    assert path.read_text() == ""


def test_a_long_stall_names_the_turn_and_the_upload(caplog, monkeypatch):
    monkeypatch.setenv("EM_LOOP_WATCHDOG", "1")

    class Dev:
        device_id = "dot-a"

        class Lock:
            def locked(self):
                return True

        voice_lock = Lock()

    em_state.devices["dot-a"] = Dev()
    em_ota.in_progress.add("dot-b")
    try:
        caplog.set_level(logging.WARNING, logger="echomuse")
        em_stallwatch.note(1500)
        assert "stall 1500ms blocking" in caplog.text
        assert "dot-a" in caplog.text
        assert "dot-b" in caplog.text
        caplog.clear()
        em_stallwatch.note(200)
        assert caplog.text == ""
        monkeypatch.setenv("EM_LOOP_WATCHDOG", "0")
        em_stallwatch.note(1500)
        assert caplog.text == ""
    finally:
        em_state.devices.pop("dot-a", None)
        em_ota.in_progress.discard("dot-b")


def test_the_monitor_measures_through_the_watchdog():
    import _sources
    body = _sources.def_source("event_loop_lag_monitor")
    assert "em_stallwatch.measure" in body
    assert "em_state.loop_lag_peak_ms = " in body


def test_measure_rearms_around_the_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(
        em_stallwatch, "arm", lambda timeout, file=None: calls.append(("arm", timeout)))
    monkeypatch.setattr(
        em_stallwatch, "disarm", lambda: calls.append(("disarm",)))

    async def main():
        loop = asyncio.get_running_loop()
        return await em_stallwatch.measure(loop, 0.01)

    lag = asyncio.run(main())
    assert lag < 500
    assert calls[0] == ("arm", 1.01)
    assert calls[1] == ("disarm",)
