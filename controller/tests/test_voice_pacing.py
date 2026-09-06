"""
Pacing the voice stream, and why it has to exist at all.

The voice path sent every period as fast as the socket would take it. That
cut long responses off mid-sentence, by a chain that is mechanical rather
than probabilistic:

  the device's WS read goroutine calls PumpPeriod INLINE per audio frame →
  pump() ends in a BLOCKING send on a channel 128 periods (~5.5s) deep →
  once full the read goroutine stops calling ReadMessage → gorilla fires the
  pong handler only inside ReadMessage → the device cannot answer a ping →
  we close after 10s without one → 1011 mid-response.

Measured on Test Echo 1, 2026-08-30: 35.4s of audio sent in 21.3s, 9.8s of
it blocked in socket writes, connection closed five seconds later. Short
responses never reproduce it because they never fill 5.5s.

em_controller cannot be imported here (websockets, aiohttp), so the rule is
tested through em_pacing and the wiring is asserted against the source.
"""

import re
from pathlib import Path

import em_pacing

CONTROLLER = Path(__file__).resolve().parents[1]
DEVICE = CONTROLLER.parent / "device"


def test_no_delay_while_under_the_lead():
    assert em_pacing.lead_delay(sent_seconds=2.0, elapsed_seconds=0.0,
                                lead_seconds=4.0) == 0.0


def test_the_delay_is_exactly_the_excess():
    assert em_pacing.lead_delay(10.0, 3.0, 4.0) == 3.0


def test_a_stream_behind_realtime_is_never_told_to_wait():
    """
    A slow producer or a link stall leaves the stream behind realtime.
    Returning a negative here would be handed to asyncio.wait_for, which
    treats a negative timeout as an immediate timeout rather than an error —
    so the bug would not raise, it would just quietly stop pacing.
    """
    assert em_pacing.lead_delay(1.0, 9.0, 4.0) == 0.0
    assert em_pacing.lead_delay(0.0, 0.0, 4.0) == 0.0


def test_a_fast_producer_settles_at_the_lead_and_never_exceeds_it():
    """
    The property that actually matters: however fast audio is produced, the
    queue ahead of the device converges to the lead and stays there.
    """
    lead, period = 4.0, 0.0427
    now, sent, worst = 0.0, 0.0, 0.0
    for _ in range(2000):                     # ~85s of audio
        now += em_pacing.lead_delay(sent, now, lead)
        sent += period
        worst = max(worst, sent - now)
    assert worst <= lead + period + 1e-9, f"queued {worst:.2f}s ahead"
    assert sent - now > lead - period, "should hold the lead, not collapse to realtime"


def test_the_lead_stays_under_the_device_buffer():
    """
    Cross-language guard. VOICE_LEAD_S must stay below the device's own
    channel depth or the fix is void — the point is that the device's
    channel never fills, and a lead at or above it fills it by design.

    Read from the Go source rather than restated, so raising audioChanDepth
    on one side and not the other is caught here instead of on hardware.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    lead = float(re.search(r"^VOICE_LEAD_S\s*=\s*([\d.]+)", src, re.M).group(1))
    rate = int(re.search(r"^SPEAKER_RATE\s*=\s*(\d+)", src, re.M).group(1))
    period = int(re.search(r"^SPEAKER_PERIOD\s*=\s*(\d+)", src, re.M).group(1))

    go = (DEVICE / "internal/bindings/speaker/pcm_speaker.go").read_text()
    depth = int(re.search(r"audioChanDepth\s*=\s*(\d+)", go).group(1))

    device_seconds = depth * period / rate
    assert lead < device_seconds, (
        f"VOICE_LEAD_S={lead}s is not under the device's {device_seconds:.1f}s "
        f"channel ({depth} periods) — the queue would fill it and block the "
        f"read goroutine, which is the bug being fixed"
    )
    # Headroom, not a hairline pass: em_player.LEAD_S keeps ~1.4s for the
    # same reason on the music plane.
    assert device_seconds - lead >= 1.0, "less than 1s of headroom under the device buffer"


# ── The wiring ───────────────────────────────────────────────────────────────

def test_the_pacing_wait_is_raced_against_cancel():
    """
    A barge-in or mute must land INSIDE the pacing gap. A bare sleep would
    add pacing latency to the one thing that must never be delayed.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    block = src[src.index("async def send_period("):]
    block = block[:block.index("sent_seconds += SPEAKER_PERIOD_SECONDS")]
    assert "em_pacing.lead_delay(" in block, "send_period must consult the pacer"
    assert "cancel_event.wait()" in block, \
        "the pacing wait must be raced against cancel_event, not a bare sleep"
    assert "asyncio.sleep(" not in block, "a bare sleep here swallows a barge-in"


def test_the_first_period_is_never_paced():
    """
    Nothing may delay the start of playback; the lead builds at full speed
    until it is reached. Expressed as the pacer living in the `else` of the
    first-period branch.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    block = src[src.index("async def send_period("):]
    block = block[:block.index("sent_seconds += SPEAKER_PERIOD_SECONDS")]
    first = block.index("First streamed PCM period")
    assert block.index("em_pacing.lead_delay(") > first, \
        "pacing must come after the first-period branch, never before it"
