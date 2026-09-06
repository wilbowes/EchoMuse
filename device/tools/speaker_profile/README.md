# speaker_profile — measure the speaker, using the hardware echo reference

Measures what the Echo Dot's speaker actually does, and which playback channel
reaches its driver.

## Why this is easy now, and was not before

Ch7 and Ch8 of the 9-channel mic capture are **not microphones**. They are a
stereo loopback of the device's own playback — a hardware echo reference,
always present, needing no mixer change (measured 2026-08-29; full numbers in
SETUP.md's Mic Array section, the story in JOURNAL.md).

That is what makes this measurement cheap. The stimulus and the response come
back in **one capture off one ADC clock** — Ch7 is what went to the DAC, Ch0–Ch6
are what the mics heard — so the transfer function is a division with no
alignment step, no timing assumptions and nothing to correlate. The planned
alternative was to play a sweep from the controller and align it across a WiFi
link with unknown latency.

## Running it

```bash
device/tools/build_tools.sh          # builds capture_mics into device/build/
device/tools/speaker_profile/profile.sh [output_dir]
```

Signals are generated on first run (`generate_signals.py`) rather than
committed. Needs `adb`, and `numpy` on the host.

It stops EchoMuse for exclusive ALSA and restarts it on every exit path,
Ctrl-C included. Volume is restored by EchoMuse itself, which re-seeds from
`startupVolume` on its first config push.

**Part A** — 440Hz left with right silent, then the reverse. Establishes which
channel reaches the driver.

**Part B** — two log sweeps 60s apart, and **it prints whether they agree
before it prints any response table**. That ordering is deliberate: a response
curve you have already read is hard to un-believe if the repeatability probe
then fails.

## Reading the result

The result is the whole loop — DAC, amp, driver, room, **and the Echo's own
microphone**, which is not calibrated and whose response is in every number.
It is the right measurement for choosing EQ defaults for a system judged
through this speaker. It is not a driver response and does not replace a
measurement mic.

**Repeatability is not correctness.** Two sweeps in the same spot share the
same room, so a boundary reflection repeats perfectly and looks like a driver
feature. The only way to tell them apart is to **measure in several
placements**: what holds is the driver, what moves is the room.

## What it found on biscuit (2026-08-29, three placements)

Cluttered desk, raised in the open, and on a box against a wall:

| | |
|---|---|
| Placement-invariant (≤1.7dB spread) | 200, 250, 400, 800, 1000, 1600 Hz |
| Room-dominated (>3.7dB) | 1250, 2000, 2500, 4000, 5000, 6300 Hz |
| Unmeasurable | below 200Hz — the driver radiates almost nothing there, so it measures room noise. With nothing playing the mic's strongest components were 50Hz mains and its harmonics |

- The driver is **−18dB at 250Hz** relative to 1kHz, and that band is the most
  stable in the whole dataset (0.8dB across all three placements).
- It **peaks around 3.15kHz** (+8.1 / +8.9 / +5.3), real in every placement.
- **Above 4kHz the room dominates** — 5000Hz moved 13.8dB between placements,
  more than the entire ±12dB EQ range. Do not tune there.

**This independently confirms stock's EQ**, which was read off a device as FIR
coefficients (#247) with no reference to any of this: stock boosts +26dB at
150Hz where we measure the driver 24dB down, and notches −15dB at 2.8kHz where
we measure it peaking. Two methods sharing no assumptions, agreeing on both
features.

**Surface coupling is first-order.** The same device at the same driver-to-mic
distance measured 8.4dB louder on a desk than raised in the open, and louder
again against a wall — the downward-firing driver couples into whatever it
stands on. Response curves here are normalised to 1kHz so this does not distort
them, but it does mean no single EQ default suits every placement.
