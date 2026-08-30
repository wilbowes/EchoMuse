"""How far ahead of realtime a device audio stream may be queued.

One rule, extracted so it can be tested: em_controller imports websockets
and aiohttp and cannot be imported by the CI test job, the same reason
em_volume, em_linkauth, em_ble_health and em_wsclose live on their own.

WHY PACING IS NOT OPTIONAL. The voice path used to send every period as fast
as the socket would take it, and that cut long responses off mid-sentence.
The device's WebSocket read goroutine calls PumpPeriod inline for each audio
frame, and pump() ends in a BLOCKING send on a channel 128 periods (~5.5s)
deep. Fill that channel and the read goroutine stops calling ReadMessage —
which is the only place gorilla fires the pong handler, so the device cannot
answer a keepalive ping. The controller pings every 20s and closes after 10s
without a pong, and the buffer drains at realtime, so the block outlasts the
timeout. Measured 2026-08-30: 35.4s of audio sent in 21.3s, 9.8s of it
blocked in socket writes, `1011 keepalive ping timeout` five seconds later.

WHY IT COSTS NOTHING. The device can hold ~5.5s and no more. Everything sent
beyond that sits in TCP buffers, which are lost on a reconnect exactly like
audio that was never sent — so the excess never bought stall resilience. It
only bought the block.
"""


def lead_delay(sent_seconds: float, elapsed_seconds: float,
               lead_seconds: float) -> float:
    """
    Seconds to wait before queueing more audio, to hold the stream at
    `lead_seconds` ahead of realtime.

    `sent_seconds` is audio handed to the socket measured in playback time;
    `elapsed_seconds` is wall time since the first period went out. Their
    difference is the lead currently held.

    Never negative. A stream that has fallen BEHIND realtime — a slow
    producer, a link stall — must not be told to wait, and must not be
    handed a negative timeout, which asyncio.wait_for treats as an immediate
    timeout rather than an error and would hide the condition.
    """
    ahead = sent_seconds - elapsed_seconds
    if ahead <= lead_seconds:
        return 0.0
    return ahead - lead_seconds
