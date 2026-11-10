"""
A barge-in over a reply flushes the speaker BEFORE arbitration.

Every barge during playback stops the reply, whoever wins the claim, and the
claim holds up to 250ms (em_listen.ARB_HOLD_S). Flushing after it left the
reply playing into the barge session's first frames: on VVV, 2026-10-21,
"never mind" over a reply reached HA as " one quince. Never mind.", HA's own
sentence did not match, and the LLM answered it 11s later.
"""

import asyncio

import pytest

import _oww_stub

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub
import em_state  # noqa: E402
from _sources import code_only, def_source  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    yield
    _oww_stub.uninstall()


def _barging_device(events, monkeypatch):
    d = em_controller.Device("dev1", "10.0.0.1", [], None)
    d.speaking = True
    d.barge_in_enabled = True
    d.wake_arb_ms = 700

    async def send_control(msg):
        events.append(msg["type"])

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(d, "send_control", send_control)
    monkeypatch.setattr(d, "listen_ack", nothing)
    monkeypatch.setattr(d, "listen_close", nothing)
    monkeypatch.setitem(em_state.devices, "dev1", d)
    monkeypatch.setitem(em_state.devices, "dev2", object())
    return d


def _wake_event():
    return {"session": 7, "score": 0.991, "threshold": 0.25, "floor": None,
            "age_ms": 80, "arrived": 0.0, "captured_mono": None,
            "level": None, "peak": None}


@pytest.mark.parametrize("winner", ["dev1", "dev2"])
def test_a_private_barge_flushes_before_it_claims(monkeypatch, winner):
    events = []
    d = _barging_device(events, monkeypatch)

    async def claim(*_a, **_k):
        events.append("claim")
        return winner

    monkeypatch.setattr(em_controller, "_claim_wake", claim)
    monkeypatch.setattr(em_controller.esphome, "can_serve_turn", lambda _id: True)
    monkeypatch.setattr(em_controller.esphome, "abort_ha_run", lambda _id: None)
    monkeypatch.setattr(em_controller, "_wake_heard_at", lambda *_a: 0.0)

    asyncio.run(em_controller._private_barge(d, _wake_event()))

    assert events == ["speaker_flush", "claim"]
    assert d.barge_ceded is (winner != "dev1")


def test_a_barge_while_thinking_sends_no_flush(monkeypatch):
    events = []
    d = _barging_device(events, monkeypatch)
    d.speaking = False
    d.thinking = True

    async def claim(*_a, **_k):
        events.append("claim")
        return "dev1"

    monkeypatch.setattr(em_controller, "_claim_wake", claim)
    monkeypatch.setattr(em_controller.esphome, "can_serve_turn", lambda _id: True)
    monkeypatch.setattr(em_controller.esphome, "cancel_voice_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(em_controller, "_wake_heard_at", lambda *_a: 0.0)

    asyncio.run(em_controller._private_barge(d, _wake_event()))

    assert events == ["claim"]


def test_the_stream_watcher_flushes_before_it_claims():
    # The watcher is a scoring loop over live audio, so its order is pinned
    # in the source: exactly one flush, ahead of the claim.
    src = code_only(def_source("_barge_watcher"))
    assert src.count("speaker_flush") == 1
    assert src.index("speaker_flush") < src.index("_claim_wake(")


# The flush ends the reply, so the device reports playback complete and the
# turn unwinds while the claim is still held. The loop started the
# interrupting turn then, and the claim's cancel_event killed it: on VVV,
# 2026-10-23, the reply was cut and the barge turn ended in 199ms with
# nothing sent to HA. A loser of the claim would have run the turn as well.
@pytest.mark.parametrize("winner", ["dev1", "dev2"])
def test_the_turn_loop_waits_for_the_claim_a_flush_outran(monkeypatch, winner):
    events = []
    d = _barging_device(events, monkeypatch)

    async def scenario():
        flushed = asyncio.Event()

        async def send_control(msg):
            events.append(msg["type"])
            flushed.set()

        async def claim(*_a, **_k):
            await asyncio.sleep(0.05)
            return winner

        monkeypatch.setattr(d, "send_control", send_control)
        monkeypatch.setattr(em_controller, "_claim_wake", claim)
        monkeypatch.setattr(em_controller.esphome, "can_serve_turn", lambda _id: True)
        monkeypatch.setattr(em_controller.esphome, "abort_ha_run", lambda _id: None)
        monkeypatch.setattr(em_controller, "_wake_heard_at", lambda *_a: 0.0)

        barge = asyncio.create_task(em_controller._private_barge(d, _wake_event()))
        await flushed.wait()  # the reply has ended; the turn unwinds now
        await em_controller._await_barge_decision(d)
        seen = (d.cancel_event.is_set(), d.barge_ceded)
        await barge
        return seen

    assert asyncio.run(scenario()) == (True, winner != "dev1")


def test_the_turn_loop_waits_before_it_stops_the_watcher():
    # stop_watcher cancels the watcher, and a watcher cancelled mid-claim
    # never decides the barge at all.
    src = code_only(def_source("_run_voice_locked"))
    assert src.index("_await_barge_decision(device)") < src.index("await stop_watcher()")
    assert src.index("_await_barge_decision(device)") < src.index("device.barge_detected and device.barge_ceded")


def test_every_barge_exit_marks_the_decision_made():
    watcher = code_only(def_source("_barge_watcher"))
    assert "finally:\n        device.barge_decided.set()" in watcher
    listen = code_only(def_source("_private_listen"))
    assert "await _private_barge(device, ev)\n            finally:\n                device.barge_decided.set()" in listen


# The flush also ends playback, and the device reports that inside the claim's
# hold, so the turn reached its outcome with neither flag set and recorded `ok`
# for a reply that was cut (VVV 2026-10-23, 10:24:17 and 11:58:48). The turn is
# marked barged before the flush goes out.
def test_a_private_barge_marks_the_turn_before_it_flushes(monkeypatch):
    events = []
    d = _barging_device(events, monkeypatch)

    async def claim(*_a, **_k):
        events.append("claim")
        return "dev1"

    monkeypatch.setattr(em_controller, "_claim_wake", claim)
    monkeypatch.setattr(em_controller.esphome, "can_serve_turn", lambda _id: True)
    monkeypatch.setattr(em_controller.esphome, "abort_ha_run", lambda _id: None)
    monkeypatch.setattr(em_controller.esphome, "note_barged", lambda _id: events.append("barged"))
    monkeypatch.setattr(em_controller, "_wake_heard_at", lambda *_a: 0.0)

    asyncio.run(em_controller._private_barge(d, _wake_event()))

    assert events == ["barged", "speaker_flush", "claim"]


def test_the_stream_watcher_marks_the_turn_before_it_flushes():
    src = code_only(def_source("_barge_watcher"))
    assert src.index("esphome.note_barged(device.device_id)") < src.index("speaker_flush")


def _satellite():
    import em_esphome
    s = em_esphome.EchoMuseSatellite(
        device_id="G090LF0000000001", label="Test", mac_address="02:EC:00:00:00:01",
        oww_model_id="hey_jarvis_v0.1", on_disconnected_cb=lambda *_: None,
        oww_model_info=None)
    s._turn_active = True
    return s


def test_a_marked_turn_reads_as_barged_and_keeps_running():
    s = _satellite()
    s.note_barged()
    # What the outcome branch reads; the turn itself is not cancelled, since
    # the reply was delivered up to the cut.
    assert s._turn_end_reason == "barged"
    assert not s._turn_cancelled
    s.abort_ha_run()
    assert s._turn_end_reason == "barged"


def test_the_first_reason_still_wins():
    s = _satellite()
    s.cancel_turn(reason="muted")
    s.note_barged()
    assert s._turn_end_reason == "muted"


def test_the_outcome_branch_reads_the_reason_not_only_the_flag():
    import inspect
    import em_esphome
    assert "if self._turn_cancelled or self._turn_end_reason:" in code_only(inspect.getsource(em_esphome))


def test_note_barged_reaches_the_live_turn(monkeypatch):
    import em_esphome
    s = _satellite()
    server = type("Server", (), {"get_satellite": lambda self: s})()
    monkeypatch.setattr(em_esphome, "get_server", lambda _id: server)
    em_esphome.note_barged("G090LF0000000001")
    assert s._turn_end_reason == "barged"
    monkeypatch.setattr(em_esphome, "get_server", lambda _id: None)
    em_esphome.note_barged("G090LF0000000001")  # no HA connection: nothing to mark
