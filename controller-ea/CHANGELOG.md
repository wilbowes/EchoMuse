# Changelog

## 2.20.0-ea.6 — Early Access

An Early Access build on top of 2.20.0. The 2.20.0 notes below are the
release this builds on — you already have them if you were running an
earlier Early Access build.

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
