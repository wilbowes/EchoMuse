# Intercom

Real-time voice conversation between two EchoMuse devices — the Drop-In
feature EchoMuse does not provide today. The full design is in the
project's design notes; this file is the protocol as built, phase by
phase, so a port or a dashboard can implement against it without reading
the code.

## Shape

Intercom is a mode on the device, not a relay through the controller and
not a Home Assistant feature. The controller is the switchboard: it knows
both devices, sets up the call, and fans hangup out to both ends. Call
audio flows device to device and never crosses the controller or Home
Assistant — Home Assistant's `assist_satellite` API has no intercom
concept.

Sendspin is the precedent for this shape: a device-resident mode the
controller switches, with its own port and its own session state.

## Phase 1 — the mode switch (this phase)

Phase 1 carries no audio. What it proves is that the switch itself is
solid: a call can be started and ended from the dashboard, both devices
enter and leave the mode together, the in-call ring shows, and nothing
gets stuck — no wedged speaker, no lost wake word, no call that survives
its switchboard.

- The device announces the `intercom` capability at registration.
- The controller offers a call to both devices with `intercom_offer`
  (`session`, `peer_id`, `peer_label`, `token`). The offer IS the call:
  each device enters in-call mode, lights a slow green pulse on the
  ring, and answers with `intercom_state` (`in_call`).
- The controller ends a call with `intercom_end` (`session`, `reason`),
  fanned out to both devices. Each device flushes the ring, answers
  `intercom_state` (`idle`), and returns to normal satellite mode.
- Teardown is idempotent on both sides: ending an ended session is a
  no-op, and a duplicated or late end can never wedge the device.
- A device that loses its controller link ends the call locally and
  clears the ring — a call cannot survive the loss of its switchboard.
- The wake-word scorer stays live for the whole call: intercom mode
  never sets the flag that pauses wake scoring for a voice turn. A wake
  during a call is detected and reported exactly as a wake during
  playback is; the controller declines to start a turn for it until
  Phase 3 nests turns inside the call.

### Device state

One device, one state at a time (`device/internal/intercom`):

| State | Meaning |
| --- | --- |
| `idle` | Normal satellite mode, no call |
| `in_call` | In a call, audio flowing (later phases) |
| `suspended` | In a call, this end running a nested Home Assistant turn (Phase 3) |

Transitions: `idle → in_call` on offer (re-offering the same session is
a no-op); `in_call → suspended → in_call` around a nested turn;
anything `→ idle` on end. Ending when idle is a no-op.

### Controller session

`controller/em_intercom.py` holds the session: an id, the two device
ids, a one-time token, and `active` / `ended`. A device is in at most
one call at a time. The token is a session secret — it is never logged —
and the callee checks a dialler against it once a peer link exists
(Phase 2). Phase 1 carries it without acting on it.

Starting a call requires both devices to be connected,
intercom-capable, unmuted, not in a voice turn and not already in a
call; anything else is a 409, never a half-started call. If offering to
one device fails after the other was offered, the session is ended
immediately so no device is left in a call the controller does not
know about.

### Voice and button behaviour during a call (Phase 1)

- **Wake word:** detected and logged, no turn starts. The private
  listener closes the session (`listen_close`, reason `intercom`); the
  controller-scored listener drops the wake before arming capture
  routing. Either way the call continues.
- **Action button:** a tap during a call hangs up. It is the one
  hangup that works while muted, because the button event flows over
  the control plane whatever the mic state is.
- **Mute:** a muted device never calls and never auto-answers (there is
  no auto-answer yet). Mute is device-sovereign: the controller cannot
  unmute a device, and a muted device cannot be called.

### API

- `POST /api/devices/{id}/intercom/start` — body `{peer_id}`. Returns
  the session. Admin only.
- `POST /api/devices/{id}/intercom/end` — body `{session_id?}`; `{}`
  ends whatever call the device is in. Admin only.
- `GET /api/devices` surfaces `intercomCapable`, `intercom` (the
  device's last reported state), `intercomSession` and `intercomPeer`.

## What is not built yet

- **Phase 2 — peer audio:** the `internal/intercom` peer listener and
  dialler, on-device 16 kHz → 48 kHz resampling, raw PCM streaming,
  ringing and consent. This is also when echo cancellation under
  continuous two-way speech gets tested on real hardware — the main
  unknown in the whole feature. If it cannot hold, the fallback is
  half-duplex push-to-talk.
- **Phase 3 — conversational latency and voice control:** the low-prime
  intercom speaker plane (the ~1 s prime is the dominant latency cost;
  a direct link alone only reaches ~1.1 s one way), the jitter buffer,
  nested Home Assistant turns (suspend / turn / resume), and the
  speech-to-text intercept for the hangup phrase.
- **Phase 4 — codec and transport, only if needed:** FLAC first for
  bandwidth, Opus on a datagram transport for loss resilience — only if
  Phase 2/3 measurements show raw PCM glitching on real devices.
