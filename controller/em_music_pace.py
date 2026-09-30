"""
How much music a device has buffered, from its own report.

The music feed used to pace itself on a clock estimate: bytes sent minus time
elapsed. That estimate is wrong after any loss — frames written to a
connection that then dropped still count as delivered — and it drifts, since
the device's audio clock is not the controller's. On 2026-09-27 a single data
reconnect left Dev Test 4 holding 1.7s while the feed believed it held 4s, so
it never sent more and the buffer never recovered for the rest of the stream.

A device announcing `music_buffer` reports, about once a second, its buffer
(`lead_ms`) and the music bytes it has received on the current data
connection (`recv`). This side counts the music bytes it wrote to that same
connection, so the difference is what is still in transit:

    buffer now = lead + (sent - recv) / rate - time since the report

Both counters restart with each connection, so bytes lost with an old one
never enter the sum. A report that cannot belong to the current connection
(it claims more than was sent on it) is discarded.
"""

# Older than this, a report no longer describes the device well enough to pace
# from, and the caller falls back to its clock estimate. Reports arrive every
# second, so this is several missed in a row.
REPORT_FRESH_S = 5.0


class MusicPace:
    def __init__(self):
        self._conn = None
        self._sent = 0
        self._report = None  # (arrival, lead_s, recv)

    def on_send(self, conn, nbytes: int) -> None:
        """Count music payload written to `conn`."""
        if conn is not self._conn:
            self._conn = conn
            self._sent = 0
            self._report = None
        self._sent += nbytes

    def on_report(self, conn, lead_ms, recv, now: float) -> bool:
        """Take a device report. False if it cannot describe `conn`."""
        try:
            lead_s = float(lead_ms) / 1000.0
            recv = int(recv)
        except (TypeError, ValueError):
            return False
        if conn is None or conn is not self._conn or recv < 0 or recv > self._sent:
            return False
        self._report = (now, lead_s, recv)
        return True

    def buffered_s(self, now: float, bytes_per_sec: float):
        """Seconds of music the device holds or will once in-transit bytes
        land, or None when there is no fresh report to go on."""
        if self._report is None:
            return None
        at, lead_s, recv = self._report
        age = now - at
        if age > REPORT_FRESH_S:
            return None
        return lead_s + (self._sent - recv) / bytes_per_sec - age
