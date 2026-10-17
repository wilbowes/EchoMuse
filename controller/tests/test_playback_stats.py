"""
What one device `playback_stats` report contributes to a turn.

The decision these pin is the one that decides whether a stream's delivery
timings reach an aggregate at all, and it is in its own module because the
controller suite cannot import `em_esphome`, which is one of its two writers.
"""

import em_playback_stats as ps


def test_an_ordinary_stream_keeps_its_timings():
    m = ps.margin_fields({
        "minDepth": 24, "primeWaitMs": 310, "recvSpanMs": 4200,
        "maxGapMs": 120, "bytesRecv": 188416,
    })
    assert m["min_depth"] == 24
    assert m["prime_wait_ms"] == 310
    assert m["recv_span_ms"] == 4200
    assert m["max_gap_ms"] == 120
    assert m["bytes_recv"] == 188416
    assert m["spanned_reconnect"] is None


def test_a_stream_spanning_a_reconnect_stores_its_timings_as_null():
    """
    The #307 case. The device sends the three arrival timings as 0 and flags
    the row; storing those zeros would put a five-minute outage into an average
    of four-second streams, which is the failure the flag exists to stop.
    """
    m = ps.margin_fields({
        "minDepth": 24, "primeWaitMs": 0, "recvSpanMs": 0, "maxGapMs": 0,
        "bytesRecv": 188416, "spannedReconnect": True,
    })
    assert m["prime_wait_ms"] is None
    assert m["recv_span_ms"] is None
    assert m["max_gap_ms"] is None
    assert m["spanned_reconnect"] == 1


def test_the_counts_survive_the_flag():
    """
    A mid-stream outage genuinely drains the buffer, and that drain is the
    audible stutter the margin exists to predict — so min_depth keeps saying so.
    """
    m = ps.margin_fields({
        "minDepth": 0, "bytesRecv": 188416, "spannedReconnect": True,
    })
    assert m["min_depth"] == 0
    assert m["bytes_recv"] == 188416


def test_a_report_from_firmware_that_sends_no_timings_is_all_absent():
    """Pre-v2.9.6 firmware reports only periods/underruns. Every margin
    column must read NULL ('never reported'), not 0 ('a perfect stream')."""
    assert ps.margin_fields({}) == {
        "min_depth": None, "prime_wait_ms": None, "recv_span_ms": None,
        "max_gap_ms": None, "bytes_recv": None, "spanned_reconnect": None,
    }


def test_absent_is_not_the_same_as_false():
    """
    Both keys exist so an old firmware's silence and an explicit false do not
    collide: NULL means the device did not say, which for a device predating
    the flag is every ordinary turn.
    """
    assert ps.margin_fields({"spannedReconnect": False})["spanned_reconnect"] is None