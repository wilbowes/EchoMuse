# Changelog

## 2.20.2-ea.2 (Early Access)

Two changes, both about being able to tell what your system is doing. No new
database migration, no firmware requirement.

### Speaker settings now change while the music is playing

The limiter and bass guard settings only took effect when the next track
started, so changing one while listening appeared to do nothing at all. That
made them very hard to judge: by the time a new track began, the sound you
were comparing against was gone.

They now apply immediately, mid-track. Turning the bass guard on or off, or
moving its depth, is audible straight away and does not click or interrupt
playback. The equaliser behaves the same way.

If you have been trying to tune the speaker and concluded the settings made no
difference, this is why — it is worth another listen.

### Early Access uses its own ports

The Early Access and stable add-ons keep completely separate data, including
their own list of devices — but both handed out the same port numbers to the
Home Assistant satellites they create. Switching between them could therefore
leave Home Assistant talking to the wrong device, which showed up as devices
sitting unavailable, wake words lighting the ring, and every request ending
immediately with no answer.

Early Access now uses a separate range, so a leftover Home Assistant entry
from the other channel simply shows as unavailable instead of reaching
something unexpected. Existing devices keep the ports they already have;
nothing is renumbered.

**Switching channels is still a migration.** Each add-on has its own database
and its own certificate authority, so your devices will not connect to the
other channel until you copy `/data` across — see the Documentation tab.

## 2.20.2-ea.1 (Early Access)

Two fixes that stop the controller doing something wrong, and the first two
steps of making the speaker sound better. No new database migration, no
firmware requirement.

### Home Assistant sign-in through the sidebar works again

Opening EchoMuse from the Home Assistant sidebar failed and fell back to the
setup-token page. The lookup that matches your Home Assistant account to an
EchoMuse one crashed on every request, so automatic sign-in — the main reason
to run the add-on rather than the standalone container — has been unusable for
several releases. Reported and diagnosed by @lennart24, fixed by @finik.

### Wake words no longer fire at nobody

The wake-word engine fills its buffer with random noise whenever it is reset,
and we scored that noise as if it were sound for the next 1.28 seconds. On one
device in one day that produced **19 wake events with nobody speaking**, all of
them just under a second after a turn ended, some scoring higher than real
speech does. Worse, it could fire the barge-in detector and cancel an answer
you were waiting for.

Raising the wake-word threshold would not have helped — these score higher than
real speech, and two of the recorded events cleared 0.80. Found, measured and
fixed by @dmndru.

One consequence worth knowing: interrupting a response is now ignored for the
first 1.28 seconds of it. That is the right trade against turns being cancelled
by nobody.

### The equalizer no longer distorts what it boosts

Turning any EQ band up could push the audio past full scale, and it was being
hard-clipped — measured at **4.7% of samples** on a normal signal with a modest
bass boost, and 18% at the top of the sliders. Flat EQ was unaffected, so this
only ever hit people who reached for the controls to improve their sound.

There is now a limiter on the output. Same test signal, same settings, no
clipping at all, for 1–6 dB of gain reduction rather than the 12 dB a simple
volume trim would have cost. **Limiter** and its ceiling and release are in
Playback; leave it on.

### New: Bass guard

A dynamic control that drops bass the speaker physically cannot produce.

It sounds backwards, and it is the single biggest thing available for a driver
this size. Frequencies below about 115 Hz still move the cone even though you
cannot hear them, and that movement muddies everything above — which is what
"tin-can" actually is. Removing them makes the midrange clearer, and measurably
*louder*, because the limiter no longer has to hold the whole signal down to
contain bass peaks nobody was going to hear. Quiet passages keep their low end;
only loud content is affected.

The parameters come from measurements of the stock Amazon firmware on the same
speaker. **Bass guard depth** is in Playback, defaulting to −20 dB where stock
uses −40 dB — deliberately gentler, because stock pairs its setting with an
equalizer curve we have not measured.

**This one wants your ears.** Play something with real low end and try the depth
from 0 to −40. Expect it to sound thinner at first and then clearer; the best
setting is probably deeper than feels right.

### Also

- Every device now carries all four stock wake word models, so changing wake
  word no longer requires a device to have been provisioned with it.
- Security updates to the web server, TLS and mDNS libraries.
- The rooting guide now says the unlock needs Linux, not macOS. Thanks
  @StefanOltmann.

## 2.20.1

Everything from the 2.20.1 Early Access builds. All fixes — no new settings,
no database migration, no firmware requirement, and nothing to do on your
devices.

### Interrupting a response now works

Saying the wake word while EchoMuse was answering stopped it — and then
nothing happened. Whatever you said next was never heard, so you had to wait
and ask again. The interrupting turn was ending a few milliseconds after it
began, before any audio reached it.

Home Assistant runs one voice pipeline at a time for each device, and EchoMuse
was starting a second one without telling it to stop the first; the abandoned
pipeline's ending then arrived and closed the new turn. Fixed for both cases —
interrupting while it is thinking, and while it is speaking.

Interrupting is still a one-way door: say the wake word and then stay quiet,
and the original answer is gone rather than resumed.

### Announcements

- **They now finish before Home Assistant is told they have.**
  `assist_satellite.announce` returned the moment the request arrived rather
  than when the audio stopped, so an automation playing two announcements in a
  row could have them talk over each other, and the satellite dropped out of
  its "responding" state early. An announcement now also reports whether the
  audio actually reached the speaker.
- **A cancelled voice turn no longer silences every announcement after it.**
  Press the action button to stop a turn, or mute the device, and
  announcements stopped playing there — no error, nothing in the dashboard,
  and it stayed that way until a voice turn happened to run. With more than
  one device this looked like announcements moving to the wrong device.
  Long-standing, and only found by trying enough announcements in a row.

### Wake words

- **Changing a wake word installs it before switching the device onto it.**
  If you picked a wake word a device had never been given, and that device was
  doing its own wake word detection, it stopped answering entirely — it had no
  model to listen with, and nothing said so while the dashboard showed the
  device as healthy. The device now keeps listening for its current wake word
  until the new model has arrived, then switches. If the model cannot be
  installed it stays where it is and the device log says why.

  Devices using controller-side detection are unaffected — the model file
  means nothing to them, so the change stays immediate.

  Note that changing a wake word briefly reconnects the device in Home
  Assistant, which makes every entity for it flicker unavailable and back. If
  you have an automation with a **state trigger** on one of them it will fire.
  See the configuration guide under *Wake word model*.

### Elsewhere

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. It now follows the device's own report that the
  audio has stopped, rather than the moment the controller finished sending it
  — which happens almost instantly and left several seconds of audio still to
  play. The start of a response is still estimated and can lead the speaker by
  about a second.
- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the provisioning wizard,
  re-downloaded the same ~10MB every time. It is now kept beside your database
  and checked against its fingerprint before use.
- **A guide for moving from the Docker container to the Home Assistant
  add-on**, in `docs/migrate-to-addon.md`. Read the part about `tls/` before
  you start.

## 2.20.1-ea.5 — Early Access

- **Changing a wake word now installs it before switching the device onto
  it.** In ea.3 and ea.4, picking a wake word a device did not have made the
  controller take over the listening for that device while it installed the
  model — even if you had deliberately chosen on-device detection. It worked,
  but it quietly changed a setting you had chosen and the dashboard carried on
  showing the setting you asked for.

  The device now keeps listening for its current wake word, on the device,
  until the new model has actually arrived — then it switches. If the model
  cannot be installed, the device stays on the wake word it already has and
  the device log says so. Nothing you chose gets overridden.

  Devices that use controller-side wake word detection are unaffected: the
  model file means nothing to them, so the change applies immediately as
  before.

## 2.20.1-ea.4 — Early Access

- **The dashboard's Speaking state now follows the device.** In ea.3 the tile
  showed Speaking slightly early, dropped back to Thinking while the device
  was still talking, then went idle. It was following when the controller
  finished *sending* audio, which happens almost instantly — the device still
  had several seconds of it left to play. It now waits for the device to
  report that the audio has actually stopped.

  The start of a response is still estimated and can lead the speaker by about
  a second: the device deliberately holds audio until it has enough buffered,
  and it only tells the controller when playback *ends*. Reporting the start
  too needs a firmware change.

## 2.20.1-ea.3 — Early Access

- **Changing a device's wake word can no longer leave it deaf.** If you picked
  a wake word a device had never been given, and that device was doing its own
  wake word detection, it stopped answering entirely — it had no model to
  listen with, and the controller had already stepped back from listening on
  its behalf. Nothing said so, and the dashboard showed the device as healthy.
  The controller now keeps listening for that device while it installs the
  model, and stays listening if the install fails.

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. The tile was only ever told about listening,
  thinking and the end of the turn, so it never heard about the bit in
  between.

- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the wizard, re-downloaded the
  same ~10MB every time. It is now kept on disk beside your database and
  checked against its fingerprint before use.

## 2.20.1-ea.2 — Early Access

Two announcement faults found while testing ea.1.

- **A cancelled voice turn used to silence every announcement after it.**
  Press the action button to stop a turn — or mute the device — and
  announcements stopped playing on that device, with no error and nothing in
  the dashboard to show for it. It stayed that way until a voice turn happened
  to run. Long-standing; ea.1's announcement changes are simply what made
  anyone try enough announcements in a row to find it. If you have more than
  one device, this looked like announcements moving to the wrong device: the
  others were fine, because only the cancelled one was affected.

- **`media_player.play_media` with announce turned on failed outright in
  ea.1.** A rename in that release missed this second announcement path, so
  every call raised an internal error and nothing played.
  `assist_satellite.announce` was unaffected.

An announcement now also tells Home Assistant whether the audio actually
reached the speaker, rather than always reporting success.

## 2.20.1-ea.1 — Early Access

- **Talking over a response now works.** Saying the wake word while EchoMuse
  was answering did stop it — and then nothing happened. Whatever you said
  next was never heard: the interrupting turn ended a few milliseconds after
  it began, before any audio was captured, so you had to wait and start again.
  Home Assistant runs one voice pipeline at a time per device, and EchoMuse
  was starting a second without telling it to stop the first; the old
  pipeline's ending then arrived and closed the new turn. Fixed for both
  cases — interrupting while it is thinking, and while it is speaking.

  Note that interrupting is still a one-way door. If you say the wake word and
  then stay quiet, the original answer is gone rather than resumed.

- **Announcements now finish before Home Assistant is told they have.** The
  `assist_satellite.announce` service returned the moment the request
  arrived rather than when the audio stopped. An automation that plays two
  announcements in a row could have them talk over each other on the device,
  and the satellite dropped out of its "responding" state early. It now waits
  for playback, and says whether the audio actually reached the speaker.

## 2.20.0-ea.6 — Early Access

- **Installing wake word models on a device works again.** Sending a wake
  word model to an Echo failed with an internal error, every time. If you
  changed a device's wake word to one it had not been given during setup, it
  simply stopped responding — it had no model to listen with, and nothing
  said so. Fixed, with a test so it cannot come back quietly.

## 2.20.0

Everything since 2.19.0. Two changes need a moment of your attention — the
wake word name and the volume range — and both are called out below.

### Adding a device in Home Assistant now works

Adding an EchoMuse device opened a dialog asking you to say the wake word,
and it never advanced: the ring lit, the device answered, and **Skip** was
the only way on. Home Assistant listens for the *name* of the wake word that
fired and the controller never sent one. Every ordinary voice turn already
worked, which is why this went unnoticed for so long — the setup dialog is
the only thing that reads it.

Two follow-on fixes came out of the same flow. The device now stops
listening the moment Home Assistant is finished, instead of holding the
microphone until a timeout — which is what made the dialog's second prompt
land on a device still busy with the first, and what caused the occasional
"device not found, press Retry". And the ring holds briefly so you can see
it heard you, rather than flashing for a tenth of a second.

### ⚠️ Your wake word may need selecting again

The wake word reported to Home Assistant loses its version suffix: **"hey
jarvis v0.1" becomes "hey jarvis"**. The `_v0.1` is an openWakeWord filename
convention that was never meant to be read by a person, and it had been
reaching Home Assistant's own device registry.

Home Assistant restores a wake word choice **by name**, so after updating, a
device may show none selected. Set it again under Settings → Devices &
Services. One dropdown, once per device.

### ⚠️ Volume distorted above about three-quarters, and no longer does

EchoMuse was driving the codec's digital volume past the point where it can
only clip — measured at **65% distortion three button presses above the
midpoint, and 89% at the maximum**, with the output no longer getting any
louder. Stock Alexa never touches that control. This is the substance of
every report that EchoMuse sounded worse than stock when turned up. Found
and measured by @kdkavanagh.

Two things you will notice:

- **The volume percentage in Home Assistant will read higher** for a device
  sitting at the same physical level, because the scale no longer includes a
  stretch that only distorted. Nothing got louder or quieter. **If you have
  an automation with a volume threshold in it, check that threshold.**
- **The top of the range is quieter than it was.** What used to be above it
  was a square wave, so this is the fix rather than a regression — but if
  you regularly ran a device near maximum you will hear it. Cleaner, and not
  as loud.

The physical buttons now step about 4dB per press across the audible range,
instead of spending presses near the bottom of a scale where nothing is
audible — silencing a device is the mute button's job. The volume ring spans
that same range, so a press always moves it.

### Home Assistant add-on

- **Leaving "Server IP" empty now detects this host's LAN address.** The old
  fallback was a hardcoded address, so a fresh install with the field blank
  advertised it to every device over mDNS: the controller ran perfectly and
  no device could reach it, with nothing reporting an error anywhere. If you
  set the field by hand to work around that, you can clear it.
- **No separate dashboard login under Home Assistant.** HA has already
  authenticated you, so the panel signs you in as your HA user and the
  first-run setup token is gone. The first person through becomes admin;
  everyone after is read-only until promoted. There is no Sign out on an HA
  session — sign out of Home Assistant instead.
- **Recordings and transcripts are admin-only.** Saved utterances are
  recognisable speech from inside your home, and every household HA user can
  reach the panel, so read-only accounts no longer see the audio player or
  the transcript text. Enforced on the server, not hidden in the page.
- **A Debug logging option.** The controller could always do this, but only
  if you ran it as a container and knew the environment variable. Leave it
  off unless you are chasing something; it is verbose.
- **An Early Access channel**, installed as a separate add-on for anyone who
  wants the next release before it is general. It has its own storage, so
  switching channels is a migration rather than a toggle — see its
  documentation before installing.
- The provisioning wizard's WebUSB error now names the exact origin the
  browser needs allowed, instead of suggesting an address the add-on refuses.

### Elsewhere

- **Entity names no longer repeat the device name.** Home Assistant already
  puts the device name in front of every entity and we were adding it again,
  so a sensor read "Kitchen Voice Assistant Kitchen Ambient Light". Only the
  displayed name changes: entity IDs stay as they are, so automations keep
  working, and an entity you renamed yourself keeps your name.
- **Greyed-out switches no longer do the opposite of what they show.** A
  disabled toggle could still be clicked, and stored the inverted value.
  Thanks to @kdkavanagh.
- Roles can be changed via `PATCH /api/users/{id}`; the last admin cannot be
  demoted.
- A voice turn that Home Assistant ends before it starts listening is no
  longer recorded as "no speech" — it was blaming the speaker for something
  at the other end, in a figure the activity statistics report.
### Early Access prereleases

This release was published to the Early Access channel first, as
`2.20.0-ea.1` through `2.20.0-ea.5`. Each carried a subset of the above;
`2.20.0-ea.5` is the same set of changes as this release. Nothing further is
needed if you were running one of them.


## 2.19.0

- **EchoMuse can be installed as a Home Assistant add-on** rather than run
  with docker compose by hand. The dashboard appears as a sidebar panel
  through ingress, so it is reachable wherever Home Assistant is without
  exposing another port. Thanks to @natecj for building it, and @Pinball3D
  whose earlier attempt worked out the approach.
- Existing docker compose installs are unaffected.
- **Six shipped defaults corrected — new installs only.** Wake threshold
  0.3 → 0.5, beamforming, echo cancellation and barge-in on by default,
  barge-in threshold 0.10 → 0.05. Stored configuration always wins over a
  shipped default, so an existing controller keeps every value it has.

## 1.0.1

- Initial Home Assistant Supervisor add-on packaging: install and run the
  controller from Settings → Add-ons instead of hand-run docker-compose.
- Ingress support for the dashboard (no separate port to expose).
- Add-on config UI labels, icon, and logo.
