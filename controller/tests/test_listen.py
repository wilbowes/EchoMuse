"""em_listen — private listening (docs/listening.md)."""

import em_listen as L


# ── resolve ──────────────────────────────────────────────────────────────────

NEW = {"mic", "oww_shadow", "oww_trigger", L.CAPABILITY}
OLD = {"mic", "oww_shadow", "oww_trigger"}


def test_controller_mode_streams():
    v = L.resolve("off", NEW, "local")
    assert v.state == L.STATE_CONTROLLER and v.streams is True


def test_shadow_is_a_streaming_diagnostic():
    v = L.resolve("shadow", NEW, None)
    assert v.state == L.STATE_DIAGNOSTIC and v.streams is True


def test_private_only_on_the_devices_word():
    """Configuration alone never makes an Echo private: only its report."""
    assert L.resolve("on", NEW, "local").streams is False
    assert L.resolve("on", NEW, None).streams is None
    assert L.resolve("on", NEW, "stream").streams is True


def test_degraded_does_not_stream_and_says_why():
    v = L.resolve("on", NEW, "degraded", "model missing")
    assert v.state == L.STATE_DEGRADED and v.streams is False
    assert "model missing" in v.reason
    assert v.private


def test_old_firmware_is_shown_streaming():
    v = L.resolve("on", OLD, "local")   # a stray report must not override
    assert v.state == L.STATE_LEGACY and v.streams is True
    assert "firmware" in v.reason
    assert not v.private


def test_fleet_summary():
    views = [L.resolve("on", NEW, "local"), L.resolve("off", NEW, None),
             L.resolve("on", NEW, None), L.resolve("on", NEW, "degraded")]
    s = L.fleet_summary(views)
    assert s == {"total": 4, "streaming": 1, "private": 2, "unknown": 1,
                 "degraded": 1}


# ── frames ───────────────────────────────────────────────────────────────────

def test_frame_round_trip():
    pcm = bytes(range(10))
    f = L.build_frame(0xFFFFFFFF, 65537, pcm)
    assert L.parse_frame(f) == (0xFFFFFFFF, 1, pcm)


def test_parse_rejects_other_frames_and_session_zero():
    assert L.parse_frame(b"\x01\x00\x00" + b"\x00" * 10) is None
    assert L.parse_frame(L.build_frame(0, 0, b"xx")) is None
    assert L.parse_frame(b"\x07\x00") is None


def test_frame_layout_matches_the_firmware():
    """[0x07][session u32 BE][seq u16 BE][PCM] — data.go sendListenFrame."""
    f = L.build_frame(1, 2, b"P")
    assert f == b"\x07\x00\x00\x00\x01\x00\x02P"


# ── routing ──────────────────────────────────────────────────────────────────

def test_audio_that_beats_its_wake_is_held_then_delivered_in_order():
    r = L.SessionRouter()
    assert r.frame(1, b"a", 0.0) == []
    assert r.frame(1, b"b", 0.1) == []
    assert r.open(1, 0.2) == [b"a", b"b"]
    assert r.frame(1, b"c", 0.3) == [b"c"]


def test_stragglers_of_a_closed_session_never_reach_the_next():
    r = L.SessionRouter()
    r.open(1, 0.0)
    r.close(1)
    assert r.frame(1, b"late", 0.1) == []
    r.open(2, 0.2)
    assert r.frame(1, b"later", 0.3) == []
    assert r.frame(2, b"x", 0.3) == [b"x"]


def test_opening_a_new_session_closes_the_old():
    r = L.SessionRouter()
    r.open(1, 0.0)
    r.open(2, 0.1)
    assert r.frame(1, b"old", 0.2) == []


def test_unannounced_audio_expires():
    r = L.SessionRouter()
    r.frame(9, b"a", 0.0)
    r.frame(8, b"b", L.PENDING_MAX_S + 0.5)    # triggers expiry of 9
    assert r.open(9, L.PENDING_MAX_S + 0.6) == []
    assert r.dropped >= 1


def test_pending_sessions_are_bounded():
    r = L.SessionRouter()
    for s in range(1, L.PENDING_MAX_SESSIONS + 3):
        r.frame(s, b"x", 0.01 * s)
    assert len(r._pending) == L.PENDING_MAX_SESSIONS
    assert r.open(1, 0.1) == []     # the oldest went


def test_close_with_no_active_session_is_harmless():
    r = L.SessionRouter()
    assert r.close() is None


# ── capture time ─────────────────────────────────────────────────────────────

def test_heard_at_subtracts_age_and_one_way_delay():
    assert L.heard_at(10.0, 500, 200) == 10.0 - 0.5 - 0.1


def test_heard_at_without_information_is_arrival():
    assert L.heard_at(10.0, None, None) == 10.0
    assert L.heard_at(10.0, -40, None) == 10.0   # a negative age is noise


def test_rtt_estimator_follows_rfc6298():
    e = L.RttEstimator()
    assert e.rto is None
    e.add(100)
    assert e.srtt == 100 and e.rto == 100 + 4 * 50
    e.add(200)
    assert e.srtt == 0.875 * 100 + 0.125 * 200


def test_parse_wake():
    ev = L.parse_wake({"session": 3, "score": 0.8, "threshold": 0.5,
                       "ageMs": 120, "floor": 0.002, "barge": True}, 5.0)
    assert ev == {"session": 3, "score": 0.8, "threshold": 0.5, "age_ms": 120,
                  "floor": 0.002, "barge": True, "arrived": 5.0,
                  "captured_mono": None, "level": None, "peak": None}


def test_parse_wake_carries_the_wake_level():
    ev = L.parse_wake({"session": 3, "score": 0.8, "level": -60.5, "peak": -50}, 5.0)
    assert (ev["level"], ev["peak"]) == (-60.5, -50.0)


def test_parse_wake_refuses_what_it_cannot_act_on():
    assert L.parse_wake({"score": 0.8}, 0) is None
    assert L.parse_wake({"session": 0, "score": 0.8}, 0) is None
    assert L.parse_wake({"session": 1 << 32, "score": 0.8}, 0) is None
    assert L.parse_wake({"session": 2, "score": "x"}, 0) is None


def test_parse_wake_tolerates_missing_optionals():
    ev = L.parse_wake({"session": 2, "score": 0.9}, 1.0)
    assert ev["floor"] is None and ev["threshold"] is None and ev["age_ms"] == 0


def test_frame_is_bytes_to_every_other_reader():
    f = L.Frame(b"\x01\x02\x03\x04", 12.5)
    assert f == b"\x01\x02\x03\x04" and isinstance(f, bytes)
    assert not isinstance(f, str)          # queue sentinels are str
    buf = bytearray(); buf.extend(f)
    assert bytes(buf[:2]) == b"\x01\x02"
    assert L.arrival(f, 99.0) == 12.5


def test_unstamped_audio_arrives_now():
    assert L.arrival(b"\x00\x00", 99.0) == 99.0
    assert L.arrival("listen_mode", 99.0) == 99.0


def test_a_session_the_echo_closed_is_known_closed():
    r = L.SessionRouter()
    assert not r.is_closed(8)
    r.close(8)                     # listen_end before its wake was acted on
    assert r.is_closed(8)
    assert r.frame(8, b"\0\0", 0.0) == []


# ── CaptureClock: dating stream frames through a lossy link ─────────────────

def _feed(clock, frames):
    """frames: [(seq, arrived)] -> [captured]"""
    return [clock.observe(s, a) for s, a in frames]


def test_capture_clock_on_a_clean_link_is_arrival():
    c = L.CaptureClock()
    got = _feed(c, [(n, 100.0 + n * 0.08 + 0.004) for n in range(50)])
    assert all(abs(g - (100.004 + n * 0.08)) < 1e-9 for n, g in enumerate(got))


def test_a_frame_held_in_a_retransmit_is_dated_to_its_capture():
    """The 2026-09-22 bench case: a second of 15LE's stream arrived late and
    read as a separate utterance. Frames 20..32 held 1s, then a burst."""
    c = L.CaptureClock()
    frames = [(n, 10.0 + n * 0.08 + 0.003) for n in range(20)]
    burst_at = 10.0 + 32 * 0.08 + 0.003
    frames += [(n, max(burst_at, 10.0 + n * 0.08 + 1.0)) for n in range(20, 33)]
    got = _feed(c, frames)
    for n in range(20, 33):
        assert abs(got[n] - (10.003 + n * 0.08)) < 1e-9, n
        assert frames[n][1] - got[n] > 0.5          # arrival would be wrong


def test_capture_clock_never_postdates_arrival_or_reaches_past_the_slack():
    c = L.CaptureClock()
    c.observe(0, 0.0)
    assert c.observe(1, 0.05) <= 0.05              # early arrival: capped at arrival
    c2 = L.CaptureClock()
    c2.observe(0, 0.0)
    # 60s of frames that all "arrive" at once — a count we cannot trust
    assert c2.observe(750, 0.1) >= 0.1 - L.MAX_ARB_SLACK_S


def test_a_new_stream_restarts_the_count():
    c = L.CaptureClock()
    _feed(c, [(n, 5.0 + n * 0.08) for n in range(100)])   # old stream, seq 0..99
    got = c.observe(0, 50.0)                              # new stream from 0
    assert got == 50.0
    assert abs(c.observe(1, 50.08) - 50.08) < 1e-9


def test_sequence_wraps_without_a_reset():
    c = L.CaptureClock()
    t0 = 1000.0
    got = _feed(c, [((65530 + i) & 0xFFFF, t0 + i * 0.08) for i in range(12)])
    assert abs(got[-1] - (t0 + 11 * 0.08)) < 1e-9


def test_capture_clock_follows_the_echo_clock_drifting():
    """ALSA runs ~345ppm fast on these Echoes. Over ten minutes, with one
    frame in 97 held 300ms, the estimate must stay within the window's worth
    of drift (30s x 345ppm ~ 10ms), not accumulate all of it — either way."""
    for ppm in (-345e-6, 345e-6):
        c = L.CaptureClock()
        period = 0.08 * (1 + ppm)
        worst = 0.0
        for n in range(int(600 / period)):
            arrived = n * period + 0.002 + (0.3 if n % 97 == 5 else 0.0)
            err = abs(c.observe(n & 0xFFFF, arrived) - (n * period + 0.002))
            worst = max(worst, err)
        assert worst < 0.015, (ppm, worst)


# ── DeviceClock: an Echo's monotonic clock in ours ──────────────────────────

def _pings(clock, skew, samples):
    """samples: [(sent, out_delay, back_delay)]; the Echo's clock = ours - skew."""
    for sent, out, back in samples:
        clock.add(sent, sent + out + back, (sent + out - skew) * 1000)


def test_device_clock_maps_through_the_cleanest_exchange():
    c = L.DeviceClock()
    skew = 1234.5
    # Mostly retransmit-delayed, asymmetric exchanges; one clean one.
    _pings(c, skew, [(0, 0.9, 0.002), (5, 0.002, 1.4), (10, 0.0015, 0.0015),
                     (15, 0.4, 0.3), (20, 2.1, 0.002)])
    t = c.to_local((30.0 - skew) * 1000, arrived=30.5)
    assert abs(t - 30.0) < 0.002


def test_a_wake_held_three_seconds_in_flight_is_dated_to_its_capture():
    """Bench, 14:59:35: VVV's wake arrived 3.1s after capture. Age plus half
    an RTT would put it at arrival; the Echo's clock puts it where it was."""
    c = L.DeviceClock()
    skew = 500.0
    _pings(c, skew, [(t, 0.002, 0.002) for t in range(0, 60, 5)])
    captured_dev_ms = (100.0 - skew) * 1000
    assert abs(c.to_local(captured_dev_ms, arrived=102.9) - 100.0) < 0.003
    # Never before the hold: past it, the session is gone anyway.
    assert c.to_local(captured_dev_ms, arrived=110.0) == 110.0 - L.MAX_ARB_SLACK_S
    # And never after arrival.
    assert c.to_local(captured_dev_ms, arrived=99.0) == 99.0


def test_device_clock_without_samples_or_field_says_unknown():
    c = L.DeviceClock()
    assert c.to_local(1000, arrived=5.0) is None
    c.add(0.0, 0.004, None)          # a pong from firmware that sends no mono
    assert c.to_local(1000, arrived=5.0) is None
    c.add(0.0, 0.004, 2.0)
    assert c.to_local(None, arrived=5.0) is None


def test_old_samples_leave_the_window():
    """An exchange older than the window no longer counts, however clean —
    otherwise the clocks drifting apart would never be followed."""
    c = L.DeviceClock()
    c.add(0.0, 0.001, 0.0)                    # superb, offset 0, but old
    c.add(200.0, 200.010, (200.005 - 7.0) * 1000)   # offset 7s now
    assert abs(c.to_local((250.0 - 7.0) * 1000, arrived=250.5) - 250.0) < 1e-6


def test_parse_wake_carries_the_capture_instant():
    ev = L.parse_wake({"type": "oww_wake", "session": 3, "score": 0.9,
                       "ageMs": 40, "capturedMono": 123456}, 7.0)
    assert ev["captured_mono"] == 123456
    old = L.parse_wake({"type": "oww_wake", "session": 3, "score": 0.9}, 7.0)
    assert old["captured_mono"] is None


# ── detection path and the mixed-fleet hold (em_arbiter.contest) ─────────────

def test_detector_by_state():
    V = L.ListenView
    assert L.detector(V(L.STATE_LOCAL, False), True) == "device"
    assert L.detector(V(L.STATE_CONTROLLER, True), True) == "controller"
    assert L.detector(V(L.STATE_DIAGNOSTIC, True), True) == "controller"
    assert L.detector(V(L.STATE_LEGACY, True), True) == "device"
    assert L.detector(V(L.STATE_LEGACY, True), False) == "controller"
    assert L.detector(V(L.STATE_DEGRADED, False), True) is None
    assert L.detector(V(L.STATE_UNKNOWN, None), True) == "unknown"


def test_hold_whenever_two_or_more_echoes_can_claim():
    """Choosing by loudness needs every claim in hand, so any fleet of two or
    more holds, however each Echo detects (Wil, 2026-10-05, #747). Until then
    only a mixed fleet did."""
    hold = L.arbitration_hold
    assert hold([]) == 0
    assert hold(["device"]) == 0
    assert hold(["device", "device"]) == L.ARB_HOLD_S
    assert hold(["controller", "controller"]) == L.ARB_HOLD_S
    assert hold(["device", "controller"]) == L.ARB_HOLD_S
    assert hold(["device", "unknown"]) == L.ARB_HOLD_S
    # A degraded Echo cannot claim, so there is nobody to wait for.
    assert hold(["device", None]) == 0
    assert hold(["controller", None, None]) == 0


def test_only_a_stream_the_controller_scored_has_a_wake_tail_to_drop():
    # "hey Verona tell me a joke" reached STT as "me a joke" (2026-10-09).
    assert L.tail_discard(True, True, 3) == 0
    assert L.tail_discard(True, False, 3) == 3
    assert L.tail_discard(False, False, 3) == 0
    assert L.tail_discard(False, True, 3) == 0


# ── skip_backlog (#946) ──────────────────────────────────────────────────────

import asyncio  # noqa: E402

import _sources  # noqa: E402


def _queue(*items):
    q = asyncio.Queue(maxsize=64)
    for item in items:
        q.put_nowait(item)
    return q


def test_a_scorer_keeping_up_skips_nothing():
    q = _queue(L.Frame(b"b", 9.95))
    item = L.Frame(b"a", 10.0 - L.SCORING_MAX_LAG_S)
    assert L.skip_backlog(q, item, 10.0) == (item, 0)
    assert q.qsize() == 1


def test_a_stale_backlog_is_skipped_to_the_last_half_second():
    now = 20.0
    fresh = L.Frame(b"f", now - L.SCORING_KEEP_S)
    q = _queue(*[L.Frame(b"s", now - 4.0 + i * 0.08) for i in range(40)], fresh,
               L.Frame(b"g", now - 0.1))
    item, dropped = L.skip_backlog(q, L.Frame(b"x", now - 5.0), now)
    assert item is fresh
    assert dropped == 41
    assert q.qsize() == 1


def test_a_stream_boundary_in_the_backlog_is_still_read():
    q = _queue(L.Frame(b"s", 1.0), "listen_mode", L.Frame(b"t", 1.1))
    item, dropped = L.skip_backlog(q, L.Frame(b"x", 0.9), 10.0)
    assert (item, dropped) == ("listen_mode", 2)
    assert q.qsize() == 1


def test_an_all_stale_queue_leaves_nothing_to_score():
    q = _queue(L.Frame(b"s", 1.0), L.Frame(b"t", 1.1))
    assert L.skip_backlog(q, L.Frame(b"x", 0.9), 10.0) == (None, 3)
    assert q.empty()


def test_unstamped_audio_is_never_stale():
    item = b"\x00\x01"
    assert L.skip_backlog(_queue(), item, 1e9) == (item, 0)


def _lag_after_hiccup(skip: bool) -> float:
    """
    The worst wait a frame scored 20-60s in had in mic_queue: an Echo sends
    a frame every 80ms into a 64-frame queue that drops the oldest when full,
    and the scorer takes 78ms a frame (2.5% faster than real time) with one
    6s stall at 10s, the shape #946 measured on a Pi with four Dots.
    """
    q = asyncio.Queue(maxsize=64)
    t, next_frame, worst = 0.0, 0.0, 0.0

    def produce_until(until):
        nonlocal next_frame
        while next_frame <= until:
            frame = L.Frame(b"", next_frame)
            if q.full():
                q.get_nowait()
            q.put_nowait(frame)
            next_frame += 0.08

    while t < 60.0:
        produce_until(t)
        if q.empty():
            t = next_frame
            continue
        item = q.get_nowait()
        if skip:
            item, _ = L.skip_backlog(q, item, t)
            if item is None:
                continue
        if t >= 20.0:
            worst = max(worst, t - item.arrived)
        t += 6.0 if 10.0 <= t < 10.078 else 0.078
    return worst


def test_without_the_skip_one_stall_leaves_every_wake_five_seconds_late():
    """The bug, reproduced: the queue stays full long after the stall."""
    assert _lag_after_hiccup(skip=False) > 4.5


def test_the_skip_bounds_the_wait_after_a_stall():
    assert _lag_after_hiccup(skip=True) <= L.SCORING_MAX_LAG_S


def test_the_wake_loop_skips_its_backlog():
    src = _sources.code_only(_sources.def_source("_stream_listen"))
    assert "em_listen.skip_backlog(device.mic_queue" in src


def test_skipping_is_reported_at_most_every_thirty_seconds():
    log = L.BacklogLog()
    assert "skipped 3 stale frames (0.2s" in log.add(3, 100.0)
    assert log.add(10, 101.0) is None
    assert log.add(2, 129.9) is None
    assert "skipped 14 stale frames" in log.add(2, 130.0)
