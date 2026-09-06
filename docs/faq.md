# FAQ

Quick answers and workarounds for the things that come up most. Where an
answer has a longer version, it links to it.

If your problem isn't here, the [UAT guide](uat.md) has a list of known open
faults worth checking before you file, and
[support-bundle.md](support-bundle.md) explains what to attach.

---

## Rooting and unlocking

### The unlock won't run on my Mac.
**It needs Linux.** The unlock is
[R0rt1z2's work on XDA](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/),
not ours, and it doesn't work on macOS. A live USB is enough — the unlock is
the only step that needs Linux. Everything after it, including the
provisioning wizard, runs from a Chromium-based browser on any OS.

### `brick.sh` refuses with "restricted on locked hw".
**Your Dot isn't on the latest FireOS.** The exploit only works on current
firmware.

1. Pair the Dot to an Amazon account (any account — make a throwaway) so it
   gets on WiFi.
2. Mute it and leave it plugged in for 20–30 minutes. Muting stops wake words
   interrupting the update. You may need two of these unattended rounds.
3. Then say "Alexa, check for software updates" to jump it to current.
4. Retry `brick.sh`.

Two things contributors have hit along the way: if the Alexa app won't pair a
very old Dot, select **Echo Tap** in the app to get the old hotspot pairing
flow; and turn **off** any file manager that auto-mounts USB devices, because
it grabs the handshake and makes later adb steps fail with unhelpful errors.

### Sideloading FireOS 5.5.5.4 fails with a red flash of the ring.
Sideload it again. It generally works on the second attempt.

### Deeper questions about the unlock itself.
Those belong on the [XDA thread](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/).
We link to it rather than copying it, deliberately — a copy goes stale without
anyone noticing.

### Did I brick it?
**Probably not permanently.** A Dot stuck in preloader is usually
recoverable — see the recovery notes on the XDA thread. Don't flash anything
else at it until you've asked.

---

## The provisioning wizard

### The wizard can't see my device, or says it's in use by another program.
**Run `adb kill-server` first.** A running adb server on your machine holds
the device and the browser can't claim it. This is the single most common
provisioning failure.

### The device picker shows nothing.
Use **Chrome or Edge**. The wizard talks to the device over WebUSB and other
browsers vary. Brave in particular is unconfirmed.

### The wizard says it needs a secure context.
If you reach Home Assistant over plain `http://`, the browser blocks WebUSB.
Add your HA URL at `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
— e.g. `http://homeassistant.local:8123` — and restart the browser. Tracked as
[#170](https://github.com/wilbowes/EchoMuse/issues/170).

### The USB connection drops and re-enumerates every few seconds.
`persist.sys.usb.config` is set to `mtp,adb`, and the composite gadget is what
drops the bus — roughly every six seconds, with `device firmware changed` in
`dmesg` each time. Forcing it to `adb` alone fixes it. Nothing EchoMuse does
needs MTP.

**`setprop` from a booted device will not work.** Android's property service
refuses that specific property whatever `ro.secure` and `ro.debuggable` say
(`init: sys_prop: permission denied uid:2000 name:sys.usb.config`), so it has
to be changed before that layer applies. From TWRP, unpack the boot image, edit
`default.prop` to read `persist.sys.usb.config=adb`, add
`persist.sys.usb.state=adb`, repack and write it back — the full commands are
in [#79](https://github.com/wilbowes/EchoMuse/issues/79).

Root-caused by @kylegordon and confirmed independently by @midiland, who found
`setprop` did work on his device — worth trying first, since it costs nothing.

*(This entry previously advised setting `mtp,adb`, which is the cause rather
than the fix. Corrected 2026-09-05.)*

### A wizard step failed and I don't know why.
Every failed step offers diagnostics. Grab those before retrying — the state
the device is in is the diagnostic, and retrying destroys it.

---

## Controller and dashboard

### Add-on or Docker container — which should I run?
Whichever suits you. **Both are first-class** and neither is improved at the
other's expense. The add-on is easier if you already run Home Assistant OS;
the container is the answer if the controller lives elsewhere.

### I'm read-only in the dashboard under the add-on and can't get admin back.
Fixed — **update the controller**. The rule now counts Home Assistant admins
who can sign in through ingress, so the first HA user gets admin. On the first
page load after updating, the dashboard re-reads your role from the server and
corrects itself; you shouldn't need to do anything else.

If you're stranded on an older build, in the browser console:

```js
['em_token','em_role','em_auth_via'].forEach(k => localStorage.removeItem(k));
location.reload();
```

Background: [#235](https://github.com/wilbowes/EchoMuse/issues/235).

### How do I update the controller?
```
docker compose pull && docker compose up -d
```
or update the add-on from Home Assistant. **The dashboard's update notice is
advisory and always will be** — the controller is your container, and a
process cannot restart itself mid-request and then tell you how it went.

### The update notice shows no release notes.
Notes come from the tag annotation. If they're empty, that's ours to fix —
please file it with the version you're on.

### The dashboard shows my device as Online but nothing works.
Check **Device → Status → Voice assistant**. `Waiting for HA` means Home
Assistant has never connected to that satellite — usually a stale HA config
entry after a device was re-added. Delete the ESPHome entry in HA and
re-discover it.

### Where is my data?
SQLite beside the controller — in `/data` for the add-on, in the mounted
volume for the container. Back that directory up; it holds devices, users,
config, activity history and the TLS CA.

### Can I move the controller to a different IP?
Yes. Devices find it over mDNS and the TLS certificate deliberately identifies
it by name, not address, so it can move freely. **Static endpoints for routed
or tunnelled networks are in progress** —
[#106](https://github.com/wilbowes/EchoMuse/issues/106) /
[#166](https://github.com/wilbowes/EchoMuse/issues/166).

---

## Home Assistant

### The "Set up voice satellite" dialog times out and needs Retry.
**Known, and it's cosmetic** — press Retry and it completes. The jingle plays,
the satellite is set up, and HA's dialog gives up before it hears back.
[#219](https://github.com/wilbowes/EchoMuse/issues/219).

### Home Assistant rejects my device: "Unexpected device found at …".
An HA ESPHome config entry is keyed on host **and port**, and it's pointing at
a port that now belongs to a different device — usually after deleting and
re-adding devices, or switching release channels. **Delete the stale ESPHome
entry in HA and re-discover.** Note that this gives the device new
`entity_id`s, so automations referring to the old ones need updating.

### Every announcement throws an error in HA's log.
Fixed — update the controller. We were sending a media player state HA has no
mapping for.

### Can I use two different wake words for two different assistants?
**Not yet, but Home Assistant's half already works.** HA picks a pipeline from
the wake word phrase reported by the satellite, and we now report it. What's
missing is running more than one wake word model at a time. Tracked as
[#112](https://github.com/wilbowes/EchoMuse/issues/112).

### Music Assistant behaves oddly.
See [#210](https://github.com/wilbowes/EchoMuse/issues/210) — and please add
your setup, that one needs more reports than it has.

### The device isn't offered as a target for "start a conversation".
Not implemented yet. [#335](https://github.com/wilbowes/EchoMuse/issues/335).

---

## Voice, wake words and audio

### It doesn't wake reliably.
In order:

1. **Config → Wake word → Sensitivity**, move toward Eager.
2. **Config → Microphones → mic gain**, if the room is large or you're far
   from the device.
3. Try a different model. The stock openWakeWord models vary a lot in how well
   they suit a given voice.

If it still misses at normal speaking distance in a quiet room, that's worth a
report with the model name and the distance.

### It wakes when nobody said anything.
Move Sensitivity toward Precise. If it fires with the TV on specifically,
[#294](https://github.com/wilbowes/EchoMuse/issues/294) is the open work on
that — say what was playing.

### Can I use my own wake word?
Yes — [oww_forge](../oww_forge/README.md) trains one, and you install it in
the dashboard under **Config → Wake word → + Custom model**. Prefer the
published Docker image over building it yourself; the upstream pins that make
it work are only preserved in a published artifact.

### How do I hear what the device actually sent to speech-to-text?
**Config → Microphones → Advanced → save utterances.** The audio then appears
per-turn on the device's **Activity** tab. That is exactly the audio Whisper
received, which makes it the right thing to listen to when transcription is
wrong.

### The audio distorts when it's loud.
Check **Config → Playback**: the limiter and bass guard should be on. If it
still distorts, report the volume percentage — the number is the diagnostic.

### It sounds worse than stock Alexa did.
Partly true and partly fixed. A DAC clipping fault above unity gain was found
and corrected, which was most of it. What remains is that stock applies a
substantial driver correction we don't yet —
[#247](https://github.com/wilbowes/EchoMuse/issues/247).

### Music pauses instead of ducking under a voice response.
Ducking needs firmware that advertises audio mixing. Update the device
firmware; if it still pauses, check Device → Status and report it.

### Long responses cut off part-way.
Known: [#324](https://github.com/wilbowes/EchoMuse/issues/324). A support
bundle with the time it happened genuinely helps here.

### Can I interrupt it while it's talking?
Yes — say the wake word again. Enable it under **Config → Wake word →
Barge-in** if it isn't already.

### Does it do timers?
Yes, as of the current release — ask for one the way you'd expect. **Stopping
a ringing timer by voice is known to be unreliable**, and that half is still
being worked on. Report what you said and what happened; the wording people
actually use is the useful part.

---

## Devices and the fleet

### A firmware update failed.
Fixed in `controller-v2.18.0` — **update the controller first**, then retry.
Two faults were behind it: the transfer deleted the fallback slot before
sending anything, and one error message covered five different outcomes.

If you're on an older controller, the workaround that worked: update one
device at a time, with the device close to an access point.

**Never power-cycle a device mid-update.**

### A device stopped responding after an update.
Device → **Updates** offers a rollback to the previous slot. If that doesn't
help, a support bundle plus the time it happened is the right report.

### I deleted a device and it kept working.
Fixed — update the controller. A deleted device now drops its connection and
comes back as pending.

### I re-added a device and its voice port is missing.
Same fix, same answer: update the controller.

### My device changed its Home Assistant entity IDs.
That happens whenever a device is deleted and re-added — HA keys entities on
identity, and a re-added device is a new one. There's no way to carry the old
IDs across. Rename the entities in HA if you need the old names back.

### One device settings, or all of them?
Config is scoped per section. A device follows the fleet for every section it
doesn't override, so changing one device's Ring settings leaves its mic and
wake word tracking fleet changes. **Device → Status → Config** tells you which
it is.

### Two devices both answer when I say the wake word.
They shouldn't — arbitration picks one. **Config → Wake word → Arbitration
window** widens the window devices are compared in. If both still answer,
report it.

### Can I swap a dead Echo for a new one and keep its history?
Not yet — [#133](https://github.com/wilbowes/EchoMuse/issues/133).

### The device won't reconnect to WiFi after a reboot.
If your network has no internet access, this is a known Android behaviour
blocking auto-join — [#317](https://github.com/wilbowes/EchoMuse/issues/317).

### There's no ambient light sensor on my device.
Some Dots ship a second-source sensor that binds a different driver. Known,
and the fix is unverifiable on our hardware —
[#90](https://github.com/wilbowes/EchoMuse/issues/90) — so a report from
affected hardware is genuinely useful.

---

## Privacy

### Does EchoMuse phone home?
**No.** No telemetry, no analytics, no crash reporting, no install counter.
The consequence is stated plainly in
[configuration.md](configuration.md#what-leaves-your-network): nobody,
including the maintainers, knows how many people use it.

### What does leave my network, then?
One thing: the controller asks `api.github.com` once an hour what the newest
release is, so the dashboard can tell you an update exists. Firmware is
downloaded from GitHub when you choose to update. Set
`update_check_interval` to `0` to stop even that.

### Is my voice audio sent anywhere?
It goes from the device to your controller to your Home Assistant, over your
LAN. Where it goes after that is whatever speech-to-text you configured in
HA — that choice is yours, not ours.

### Is a support bundle safe to attach to a public issue?
Yes, by design. It's an allowlist: no transcripts, no saved audio, no WiFi
SSIDs, no IP addresses, no device labels you wrote, no credentials, no file
paths. It's plain JSON — [open it first](support-bundle.md) rather than take
our word for it.

### Is the link between device and controller encrypted?
It can be, and should be. Device → **Status** → press **Secure link** if the
Link row reads `plain ws`. **The ESPHome connection to Home Assistant is
still plaintext**, including mic audio —
[#341](https://github.com/wilbowes/EchoMuse/issues/341).

---

## Hardware and scope

### Will this work on an Echo Dot Gen 3 / Show / Studio?
Only Echo Dot Gen 2 ("biscuit") is supported today. **Echo Show 8 support is
in review** ([#358](https://github.com/wilbowes/EchoMuse/pull/358)) and Echo
Show 5 is being worked on ([#36](https://github.com/wilbowes/EchoMuse/issues/36)).
Other boards are welcome — the Android-specific surface is about twenty call
sites, so a new board is mostly a mic/speaker/LED/button binding.

### Does it depend on Amazon's software?
Barely, and keeping it that way is deliberate. It's a Linux daemon using ALSA,
i2c, evdev and sysfs that happens to run on Android because that's what
shipped on the box. A change that puts an Amazon blob back in the audio path is going
the wrong way, even when it sounds better.

### Is there a video of it working?
Yes — see [#193](https://github.com/wilbowes/EchoMuse/issues/193), which
collects community recordings.

### I want to help. Where do I start?
Issues labelled **`good first issue`** and **`ready`** are the ones with a
settled design. Anything labelled `needs-design` or `needs-decision` is not
ready to be built yet, however small it looks — check with us first so you
don't write something we then have to decline.
