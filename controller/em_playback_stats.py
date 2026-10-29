"""What a device's `playback_stats` report says about a turn's delivery margin.

The device reports per stream: periods played, mid-stream underruns, the
buffer's low-water mark, and three ARRIVAL timings — `primeWaitMs` (first frame
arriving to first frame played), `recvSpanMs` (first to last frame arrival) and
`maxGapMs` (worst single inter-arrival gap). Those three exist because
underruns are a rare binary event, so the margin is what shows a link degrading
before it breaks audibly (added 2026-07-20, after underruns appeared with no
measurable cause).

The timings are measurements of an arrival clock, and an arrival clock keeps
running through an outage. A data connection that drops mid-stream leaves
first-frame and last-frame either side of it, so all three report the gap
rather than the audio: field data 2026-08-23 (#307) has a turn whose buffer
never came close to starving — `underruns` 0, `min_depth` 24 — reporting
`prime_wait_ms` 351813, which is 5.9 minutes to prime a stream carrying four
seconds of audio. Its neighbours on the same device were 47–1366. One such row
does not degrade an average, it destroys one, and alerting on `max_gap_ms`
fires on the artefact instead of on the fault.

The device knows when its connection dropped and the controller does not, so
the device sends `spannedReconnect` and this is where that becomes storage.
The three timings store as **NULL, never 0**: a stream that could not be
measured must not read as a stream measured at zero. `min_depth`, `bytes_recv`
and the counts beside them are left alone — they are counts of what happened,
and a mid-stream outage genuinely drains the buffer, which is the stutter the
margin exists to predict.

Extracted from `em_db.set_turn_playback` and `em_esphome._persist_turn` because
there are two writers (a report arriving after the turn row exists, and the
common case where it lands first) and the rule has to be one copy: a guard in
only one of them stores the outage back into the average through the other.
Pure and dependency-free, so it is testable — the controller suite cannot
import `em_esphome`, which is where the other half of this rule lives.
"""


def margin_fields(pstats: dict) -> dict:
    """
    The `turns` columns one device playback_stats report contributes.

    pstats is the message's `stats` object, or {} on firmware too old to send
    it — in which case every column is absent (NULL), which reads as "never
    reported" rather than "measured at zero".

    Keys are the turn_record / SQL column names, so a caller spreads the
    result straight into what it is writing.
    """
    flagged = bool(pstats.get("spannedReconnect"))
    return {
        "min_depth":        pstats.get("minDepth"),
        "prime_wait_ms":    None if flagged else pstats.get("primeWaitMs"),
        "recv_span_ms":     None if flagged else pstats.get("recvSpanMs"),
        "max_gap_ms":       None if flagged else pstats.get("maxGapMs"),
        "bytes_recv":       pstats.get("bytesRecv"),
        # 1 rather than a boolean: the column is an INTEGER flag alongside
        # dev_shadow, and NULL means "the device did not say", which for a
        # device predating the flag is every ordinary turn.
        "spanned_reconnect": 1 if flagged else None,
    }