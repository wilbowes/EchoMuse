"""
A connect-time reconcile that does not finish must not hold its debounce.

Approving a device bounces its link about 3s after it connects, to hand it
credentials, which lands in the middle of the first wake word push. The redial
was then turned away by the failed run's stamp for 15 minutes, and the Echo
had no wake word (035T, 2026-10-24).
"""
import asyncio
import time

import em_oww_sync
import em_payloads
import em_reconcile
import em_state


class Live:
    android_userspace = False


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _steps(monkeypatch, oww):
    calls = []

    async def reconcile(device_id, live):
        calls.append(("oww", live))
        return await oww(device_id, live)

    async def ok(live, device_id):
        calls.append(("other", live))
        return True

    monkeypatch.setattr(em_oww_sync, "reconcile", reconcile)
    monkeypatch.setattr(em_payloads, "sync_start_script", ok)
    monkeypatch.setattr(em_payloads, "sync_endpoints", ok)
    return calls


def _fresh(monkeypatch, device_id, live):
    monkeypatch.setattr(em_reconcile, "stamps", {})
    monkeypatch.setattr(em_state, "devices", {device_id: live})


def test_a_finished_run_keeps_the_full_debounce(monkeypatch):
    live = Live()
    _fresh(monkeypatch, "D", live)

    async def fine(device_id, live):
        return True
    _steps(monkeypatch, fine)

    _run(em_reconcile.on_connect("D", live))
    now = time.monotonic()
    assert not em_reconcile._due("D", now + em_reconcile.RETRY_S + 5)


def test_a_failed_step_retries_after_a_minute_not_fifteen(monkeypatch):
    live = Live()
    _fresh(monkeypatch, "D", live)

    async def fails(device_id, live):
        return False
    calls = _steps(monkeypatch, fails)

    _run(em_reconcile.on_connect("D", live))
    assert [c[0] for c in calls] == ["oww", "other", "other"], "the others still run"
    now = time.monotonic()
    assert not em_reconcile._due("D", now + 1)
    assert em_reconcile._due("D", now + em_reconcile.RETRY_S + 1)


def test_a_raising_step_counts_as_not_finished(monkeypatch):
    live = Live()
    _fresh(monkeypatch, "D", live)

    async def boom(device_id, live):
        raise RuntimeError("shell gone")
    _steps(monkeypatch, boom)

    _run(em_reconcile.on_connect("D", live))
    assert em_reconcile._due("D", time.monotonic() + em_reconcile.RETRY_S + 1)


def test_a_device_that_redialled_mid_run_is_reconciled_again_at_once(monkeypatch):
    old, new = Live(), Live()
    _fresh(monkeypatch, "D", old)

    async def cut_off(device_id, live):
        if live is old:
            # The credential bounce: the device redials while this push runs,
            # and that connect is turned away by the stamp this run holds.
            em_state.devices["D"] = new
            assert not em_reconcile._due("D", time.monotonic())
            return False
        return True
    calls = _steps(monkeypatch, cut_off)

    async def go():
        await em_reconcile.on_connect("D", old)
        for _ in range(20):
            await asyncio.sleep(0)
    _run(go())
    assert ("oww", new) in calls, "the new connection must get its wake word now"
    assert not em_reconcile._due("D", time.monotonic() + em_reconcile.RETRY_S + 5), (
        "a rerun that finished keeps the full debounce")


def test_none_from_a_step_is_success(monkeypatch):
    live = Live()
    _fresh(monkeypatch, "D", live)

    async def silent(device_id, live):
        return None
    _steps(monkeypatch, silent)
    assert _run(em_reconcile._reconcile_payloads("D", live)) is True


def test_push_config_sends_the_effective_config(monkeypatch):
    sent = []

    class L:
        async def send_control(self, msg):
            sent.append(msg)

    monkeypatch.setattr(em_oww_sync.db, "get_effective_device_config",
                        lambda device_id: {"owwModel": "hey_jarvis_v0.1", "owwThreshold": 0.5})
    _run(em_oww_sync.push_config("D", L()))
    assert sent and sent[0]["type"] == "config"
    assert sent[0].get("owwModel") == "hey_jarvis_v0.1"


class Shadow:
    oww_shadow_capable = True
    mic_capable = True
    android_userspace = False

    async def send_control(self, msg):
        pass


def _oww(monkeypatch, live, state, result):
    import em_events
    monkeypatch.setattr(em_oww_sync.db, "get_effective_device_config",
                        lambda device_id: {"owwOnDevice": "on"})
    monkeypatch.setattr(em_oww_sync, "wanted_models", lambda device_id: ["hey_jarvis_v0.1"])

    async def device_state(live):
        if isinstance(state, Exception):
            raise state
        return state
    monkeypatch.setattr(em_oww_sync, "device_state", device_state)

    async def sync(live, device_id):
        return result
    monkeypatch.setattr(em_oww_sync, "sync", sync)

    async def push_log(*a, **k):
        pass
    monkeypatch.setattr(em_events, "push_log", push_log)
    monkeypatch.setattr(em_state, "devices", {"D": live})
    A = em_oww_sync.em_oww_assets
    monkeypatch.setattr(A, "desired_assets", lambda models: ([], []))
    monkeypatch.setattr(A, "missing_selected_classifier", lambda d, i: "Hey_Verona.onnx")
    monkeypatch.setattr(A, "missing_assets", lambda d, i: ["Hey_Verona.onnx"])
    monkeypatch.setattr(A, "reconcile_action", lambda off, missing, gaps: "deaf")


def test_the_wake_word_step_reports_a_failed_install(monkeypatch):
    live = Shadow()
    _oww(monkeypatch, live, {"installed": {}, "free_mb": 500},
         {"ok": False, "error": "md5 mismatch"})
    assert _run(em_oww_sync.reconcile("D", live)) is False


def test_the_wake_word_step_reports_a_device_it_could_not_read(monkeypatch):
    live = Shadow()
    _oww(monkeypatch, live, RuntimeError("no shell yet"), {"ok": True})
    assert _run(em_oww_sync.reconcile("D", live)) is False


def test_the_wake_word_step_reports_a_good_install(monkeypatch):
    live = Shadow()
    _oww(monkeypatch, live, {"installed": {}, "free_mb": 500}, {"ok": True, "pushed": []})
    assert _run(em_oww_sync.reconcile("D", live)) is True
