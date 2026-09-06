# User Acceptance Testing

**A checklist for confirming EchoMuse actually does what it claims on your
hardware, in your house, with your Home Assistant.** Work through as much of it
as applies to you and tell us what failed. Partial results are useful — one
section done properly beats a whole pass skimmed.

Nothing here needs a developer. Every test is a thing you can do from the
dashboard, the device, or Home Assistant.

---

## Before you start

**Record these four numbers.** Every report needs them, and half of the
questions we ask back are one of these.

| | Where |
|---|---|
| Controller version | Dashboard header |
| Firmware version | Device → **Status** tab |
| Home Assistant version | HA → Settings → About |
| Hardware | Echo Dot Gen 2, or which board |

**Use a copy of your setup if you can.** Some of these tests delete a device
or push credentials. Where a test is destructive it says so in bold.

### How to report a failure

1. Open an issue: <https://github.com/wilbowes/EchoMuse/issues>
2. Give the **test ID** (e.g. `D3`), what you expected, what happened.
3. Attach a support bundle — **Settings → Support → Collect bundle**, then
   Download. It contains no transcripts, no SSIDs, no device labels, no
   credentials; see [support-bundle.md](support-bundle.md) for exactly what is
   and isn't in it. Open it before you send it.
4. Note the **wall-clock time** the failure happened. The bundle's logs are
   timestamped and that is how we find it.

### Two things that are not bugs

- **A control shown greyed out with a reason under it.** The controller asks
  each device what it can do and disables what your firmware doesn't
  implement. That is working as designed. A control that is *enabled* and
  silently does nothing is a bug — report that.
- **A setting that reads differently on one device than the fleet.** Config is
  scoped per section. Device → Status → `Config` row says `Fleet` or
  `Local override (n of 6)`.

### Already known — please don't re-file

Check these before opening anything. If your symptom matches, add your
version numbers to the existing issue instead.

| Symptom | Issue |
|---|---|
| HA's "Set up voice satellite" dialog times out and needs Retry, after the jingle plays | [#219](https://github.com/wilbowes/EchoMuse/issues/219) |
| Playback cuts off part-way through a long spoken response | [#324](https://github.com/wilbowes/EchoMuse/issues/324) |
| Radio / stream playback is interrupted | [#325](https://github.com/wilbowes/EchoMuse/issues/325) |
| Unplugging the headphone jack stalls the mic and drops the device | [#117](https://github.com/wilbowes/EchoMuse/issues/117) |
| Odd behaviour with something plugged into the aux jack | [#141](https://github.com/wilbowes/EchoMuse/issues/141) |
| Ambient light sensor missing on a device with a `G090LF` serial | [#90](https://github.com/wilbowes/EchoMuse/issues/90) |
| WiFi never reconnects after a reboot on a network with no internet | [#317](https://github.com/wilbowes/EchoMuse/issues/317) |
| Music started elsewhere stays silent until a voice turn finishes | [#262](https://github.com/wilbowes/EchoMuse/issues/262) |
| Double/triple tap detected unreliably | [#115](https://github.com/wilbowes/EchoMuse/issues/115) |
| High CPU on the device | [#176](https://github.com/wilbowes/EchoMuse/issues/176) |

---

## A — Getting a device on

### A1 · Root and provision a device
**Do:** Run the provisioning wizard end to end on a device that has never been
rooted. Follow [rooting.md](rooting.md).
**Expect:** Every step reports success, and the device reboots into a state
where the dashboard sees it.
**Flag:** Any step that says it succeeded but left the device wrong. Include
the wizard's diagnostics — it offers them on a failed step.

### A2 · The device appears and can be approved
**Do:** Open the dashboard. Find the new device.
**Expect:** It appears as pending with an **Approve** tab and nothing else.
After approving, the full tab set appears: Status, Activity, Config, Console,
Updates, Logs.
**Flag:** A device that never appears, or appears already approved.

### A3 · Status reads true
**Do:** Device → **Status**.
**Expect:** Serial, firmware version, WiFi network, volume, Link, Config
scope, Voice assistant row — all populated, none showing `—` for something
that plainly exists.
**Flag:** Any row reading `—` while the thing it describes is working.

### A4 · Reboot survival
**Do:** Pull power from the device. Wait for it to boot.
**Expect:** It rejoins on its own within a couple of minutes, no dashboard
action needed, and keeps its volume and mute state from before.
**Flag:** A device that needs re-approving, or comes back at a different
volume.

### A5 · Controller restart survival
**Do:** Restart the controller (add-on restart, or `docker compose restart`).
**Expect:** Devices reconnect on their own. Settings, users, and history are
all still there.
**Flag:** Anything that had to be set up again.

---

## B — Home Assistant

### B1 · The satellite is discovered
**Do:** HA → Settings → Devices & Services. Look for an ESPHome discovery for
your device.
**Expect:** It offers to add. After adding, the device page shows an
**Assist satellite** entity.
**Flag:** No discovery at all; or discovery pointing at the wrong device.

### B2 · Voice assistant status is honest
**Do:** Device → **Status** → **Voice assistant** row.
**Expect:** `HA connected · port NNNNN` while HA is connected. Stop HA — it
should change to `Waiting for HA`.
**Flag:** A row that says HA is connected when it isn't, or vice versa.

### B3 · A full voice turn
**Do:** Say the wake word, then ask something with a spoken answer.
**Expect:** Ring lights on wake → response spoken from the device speaker →
ring returns to idle. Device → **Activity** shows the turn with an `ok`
outcome.
**Flag:** Any turn that ends in something other than `ok` when you spoke
normally. Give the outcome string from Activity.

### B4 · Announcements
**Do:** Call `assist_satellite.announce` from HA Developer Tools against your
device.
**Expect:** It plays, and the service call returns without error.
**Flag:** Errors in HA's log, or an announcement that plays but never
completes.

### B5 · Action button entity
**Do:** Hold the action (dot) button ~1s. Watch HA → the device's **Action
Button** event entity.
**Expect:** A `long` event fires. A short tap starts a voice turn instead
(unless you enabled "tap is an event").
**Flag:** No event entity present on a device whose firmware supports holds;
or a hold that starts a turn.

### B6 · Ambient light sensor
**Do:** Cover the device, then shine a light at it. Watch the **Ambient
Light** sensor in HA.
**Expect:** Lux moves. On hardware with no readable sensor there should be
**no entity at all** — not an entity stuck at 0.
**Flag:** An entity permanently reading 0 or unavailable.

---

## C — Wake word

### C1 · The default wake word
**Do:** Say it from a normal seated distance, ten times, in a quiet room.
**Expect:** At least 8 of 10 wake the device.
**Flag:** Fewer than 8. Say the distance and the model.

### C2 · False wakes
**Do:** Leave the device in a room with TV or conversation for an hour.
**Expect:** Zero or one spurious wakes.
**Flag:** More than that. Note what was playing.

### C3 · Sensitivity moves the needle
**Do:** Config → Wake word → move Sensitivity toward Eager. Repeat C1.
**Expect:** More wakes, and more false wakes. The change takes effect without
restarting anything.
**Flag:** A setting that saves but changes nothing.

### C4 · A custom model installs and works
**Do:** Train one with [oww_forge](../oww_forge/README.md), then Config → Wake
word → **+ Custom model** → upload.
**Expect:** It appears in the list, can be selected, and the device wakes on
it.
**Flag:** A model that uploads and is selectable but never triggers — that is
a specific known class of bug and worth a report.

### C5 · On-device wake word
**Do:** Config → Wake word → turn on on-device detection.
**Expect:** Wakes still work. Device → Activity still records turns.
**Flag:** Wakes that stop entirely, or wake latency that gets noticeably
worse.

### C6 · Multiple devices don't both answer
**Do:** With two devices in earshot, say the wake word once.
**Expect:** One device answers. The other doesn't.
**Flag:** Both answering, or neither.

---

## D — Audio out

### D1 · Volume
**Do:** Change volume from the dashboard, from HA, and with the device's own
volume buttons.
**Expect:** All three agree, and the level survives a reboot.
**Flag:** Any of the three disagreeing with the others.

### D2 · Speech is intelligible at low volume
**Do:** Set volume to ~20%, ask something with a long answer.
**Expect:** Clear speech, no distortion.
**Flag:** Distortion, clipping, or crackle. Say the volume level.

### D3 · Speech is clean at high volume
**Do:** Set volume to 100%, ask the same question.
**Expect:** Loud, but not buzzing or breaking up.
**Flag:** Distortion at a specific percentage — the number matters.

### D4 · EQ does something
**Do:** Config → Playback → move the EQ faders, or pick a preset.
**Expect:** An audible change on the next thing spoken, no restart.
**Flag:** No audible difference, or a change that needs a reboot.

### D5 · Speaker protection
**Do:** Config → Playback → confirm limiter and bass guard are on. Play
something bass-heavy loud.
**Expect:** It stays controlled. Turn the limiter off and it should get
noticeably worse.
**Flag:** No difference with them on or off.

### D6 · The headphone jack
**Do:** Plug into the 3.5mm jack.
**Expect:** Audio moves to the jack.
**Flag:** Anything beyond the known jack faults in the table above.

---

## E — Music and ducking

### E1 · Music plays
**Do:** Send music to the device from HA (Music Assistant or a media_player
call).
**Expect:** It plays.
**Flag:** Silence, stutter, or a stream that stops after a fixed interval.

### E2 · Music ducks under a voice turn
**Do:** With music playing, say the wake word and ask something.
**Expect:** Music drops in volume, the answer is spoken over it, music returns
to level. It should **duck**, not pause, on firmware that supports mixing.
**Flag:** A full pause on a device whose Status shows it can mix; music that
never comes back up; or a duck that leaves music permanently quiet.

### E3 · Duck depth is adjustable
**Do:** Config → Playback → change duck depth. Repeat E2.
**Expect:** Audibly different depth.
**Flag:** No change.

### E4 · Barge-in
**Do:** During a long spoken answer, say the wake word again.
**Expect:** The answer stops and the device listens to you.
**Flag:** The answer continuing to the end; or the new turn being refused.
Give the Activity outcome for the second turn.

---

## F — Timers and alarms

*Recently changed — this is the area most worth testing carefully.*

### F1 · A timer rings
**Do:** "Set a timer for one minute."
**Expect:** The device rings at one minute.
**Flag:** No ring, or a ring on the wrong device.

### F2 · Stopping the ring
**Do:** While it rings, tell it to stop.
**Expect:** It stops.
**Flag:** A ring that won't stop by voice. Note whether the button stops it.

### F3 · A timer firing during a voice turn
**Do:** Set a short timer, then start another voice turn so the timer fires
mid-answer.
**Expect:** Both are handled sensibly — you hear both, neither is lost, and
the device returns to idle afterwards.
**Flag:** Audio that stops dead, a device stuck ringing, or a turn that never
completes. **This combination is new and is exactly what we want tested.**

### F4 · A timer firing under ducked music
**Do:** Music playing, timer fires.
**Expect:** The alarm is heard over the music, then music returns to full
level.
**Flag:** Music that stays ducked, or an alarm you can't hear.

---

## G — Buttons and LEDs

### G1 · Action button starts a turn
**Do:** Tap the dot button.
**Expect:** Same behaviour as a wake word.
**Flag:** No response, or a delayed one.

### G2 · Mute is real
**Do:** Press mute. Try the wake word. Try the action button.
**Expect:** Red ring, and no voice turn happens by either route. The mic is
off in hardware, not just ignored.
**Flag:** A turn starting while muted.

### G3 · Unmute restores
**Do:** Press mute again.
**Expect:** Ring returns to idle, wake works immediately.
**Flag:** Needing a reboot to get the mic back.

### G4 · Ring states match what's happening
**Do:** Watch the ring through a full turn.
**Expect:** Distinct listening / thinking / speaking states, back to idle at
the end. See [led-ring-states.md](led-ring-states.md).
**Flag:** A ring left lit after a turn ends, or stuck on one colour.

### G5 · Ring colours are configurable
**Do:** Config → Ring → change listen and think colours.
**Expect:** Next turn uses them.
**Flag:** No change, or a change needing a restart.

---

## H — Bluetooth proxy

### H1 · The proxy is offered
**Do:** Config → Bluetooth → enable. Check HA → Settings → Devices & Services.
**Expect:** The device appears as a Bluetooth proxy.
**Flag:** Enabled in the dashboard but absent in HA.

### H2 · It finds something
**Do:** Bring a BLE device (a thermometer, a tracker) near it.
**Expect:** HA sees it via this proxy.
**Flag:** No discoveries at all after 10 minutes.

### H3 · It survives a reboot
**Do:** Reboot the device.
**Expect:** Proxy comes back on its own.
**Flag:** Needing to toggle it off and on.

---

## I — Security and the device link

### I1 · Secure link
**Do:** Device → Status. If Link reads `plain ws`, press **Secure link**.
**Expect:** The device reconnects within a few seconds and Link reads
`wss (TLS)`.
**Flag:** A device that goes offline and stays there. (It should redial.)

### I2 · Credentials survive a reboot
**Do:** Reboot a TLS device.
**Expect:** Comes back on `wss (TLS)`.
**Flag:** Falling back to plain.

### I3 · Login is enforced
**Do:** Log out. Try to open the dashboard, and try an API URL directly.
**Expect:** Both refuse.
**Flag:** Anything reachable logged out.

### I4 · Non-admin accounts are limited
**Do:** Create a non-admin user. Log in as them.
**Expect:** No Console tab, no Updates tab, no Support tab, no user
management.
**Flag:** Any admin action a non-admin can reach.

---

## J — Updates

### J1 · Firmware update is offered
**Do:** Device → **Updates**.
**Expect:** It shows the installed version and the latest release, and offers
the update only when there is one.
**Flag:** An update offered when you're already current, or none offered when
you're behind.

### J2 · **Destructive** — apply a firmware update
**Do:** Run the update. Watch it through.
**Expect:** It transfers, the device reboots, and comes back on the new
version, keeping its settings.
**Flag:** A device that doesn't come back, or comes back on the old version
while claiming success. **Do not power-cycle it mid-update.** If it does not
return after five minutes, say so in the report before doing anything else —
the state it is in is the diagnostic.

### J3 · Controller update notice
**Do:** When a controller release exists that is newer than yours.
**Expect:** A notice in the dashboard, with readable release notes. It should
tell you to update — it must never update itself.
**Flag:** A notice with empty notes; or any button that claims to perform the
update.

---

## K — The dashboard

### K1 · Activity is accurate
**Do:** Do five turns. Open Device → **Activity**.
**Expect:** Five turns, with sensible outcomes and timings.
**Flag:** Missing turns, or timings that are obviously wrong.

### K2 · Logs load
**Do:** Device → **Logs**.
**Expect:** Recent lines, from both device and controller.
**Flag:** An empty view on a device that has been running.

### K3 · Config scoping
**Do:** Change one section (say Ring) on one device only.
**Expect:** Status → Config reads `Local override (1 of 6)`. Change a fleet
setting in a *different* section — this device should follow it.
**Flag:** An override that leaks into other sections, or a device that stops
tracking the fleet entirely.

### K4 · It works on a phone
**Do:** Open the dashboard on a phone.
**Expect:** Usable. Nothing cut off, no horizontal scrolling.
**Flag:** Anything unreachable at that width. Screenshot helps.

### K5 · Contrast and readability
**Do:** Look at it in both light and dark.
**Expect:** Everything readable.
**Flag:** Low-contrast text — with a screenshot.

### K6 · Support bundle
**Do:** Settings → Support → Collect bundle → Download. Open the file.
**Expect:** Valid JSON. **No transcripts, no WiFi SSID, no IP addresses, no
device labels you wrote, no tokens.**
**Flag:** Anything private in it. Report that privately rather than in a
public issue, and don't attach the bundle.

---

## L — Failure handling

### L1 · Losing the controller
**Do:** Stop the controller while a device is idle.
**Expect:** The device notices, indicates it, and reconnects on its own when
the controller returns — no reboot, no re-approval.
**Flag:** A device that never comes back, or comes back only after a power
cycle.

### L2 · Losing WiFi
**Do:** Take the AP down for a minute, then bring it back.
**Expect:** The device rejoins on its own.
**Flag:** A device that stays off the network. (Note the known issue #317 for
networks with no internet.)

### L3 · Losing Home Assistant
**Do:** Stop HA. Say the wake word.
**Expect:** The failure is visible — the Voice assistant row should not read
healthy. When HA returns, turns work again without touching anything.
**Flag:** A dashboard that reads healthy with HA down.

### L4 · **Destructive** — deleting a device
**Do:** Delete a device from the dashboard.
**Expect:** It disconnects, disappears, and comes back as *pending* rather
than silently continuing to serve. Re-approve it.
**Flag:** A deleted device that keeps working; or a re-added device whose
Status shows no voice port.

---

## Reporting a whole pass

If you run a full pass, one issue with a table is more useful than one issue
per test:

```
Controller: 2.21.0    Firmware: v2.13.0    HA: 2026.8.3    Hardware: Echo Dot Gen 2

A1 pass   A2 pass   A3 pass   A4 pass   A5 pass
B1 pass   B2 pass   B3 pass   B4 FAIL   B5 pass   B6 n/a
C1 pass (9/10)  C2 FAIL (4 false wakes in an hour, TV on)  ...
```

`pass` / `fail` / `n/a` / `skipped`. For each fail, a paragraph and a support
bundle. Anything you couldn't test is as useful to know as a failure — it
tells us which parts of this guide nobody can actually follow.
