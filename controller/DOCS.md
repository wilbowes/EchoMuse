# EchoMuse

Runs the EchoMuse controller — wake word detection, fleet dashboard, and
Home Assistant integration for rooted Echo Dot 2nd Gen devices — as a Home
Assistant add-on instead of a separate docker-compose deployment.

## Installation

1. Install the add-on and start it. **Controller LAN IP address** can be
   left empty — the address devices are told to dial is detected from this
   host, and the one in use is logged at startup. Set it explicitly only if
   this machine has more than one network interface.
2. Open the dashboard from **Open Web UI**. You are signed in as your Home
   Assistant user; there is no separate account or setup token. The first
   person to open it becomes the EchoMuse admin, and anyone after that gets
   read-only access until an admin promotes them.
3. Use the dashboard's **provisioning wizard** (USB, Chrome) to set up a
   rooted Echo Dot. It finds this controller automatically — no manual IP
   entry on the device side.
4. Approve the device in the dashboard once it appears as pending. Home
   Assistant then discovers it automatically via the built-in ESPHome
   integration.

### The provisioning wizard needs a secure browser context

WebUSB — which the wizard uses to talk to the Dot over the cable — is only
available on a secure origin. If you reach Home Assistant over plain
`http://`, the wizard's first step will say so and name the exact origin to
allow. Either serve Home Assistant over HTTPS, or add that origin to
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` and relaunch the
browser. The allowlist matches scheme, host **and port** exactly, so an
entry for some other address does not cover it.

### Privacy

Saved utterance recordings and voice transcripts are **admin-only**. Every
Home Assistant user in the household can reach this panel, so read-only
accounts see turn timings, scores and outcomes but not the audio or the
text of what was said. Recording is off by default and stays that way until
you turn it on per device.

## Configuration

Every option is explained inline in the add-on's Configuration tab. For the
full picture — rooting a device, the voice pipeline, every configuration
knob — see the project's own docs:

- [Quickstart](https://github.com/wilbowes/EchoMuse/blob/main/docs/quickstart.md)
- [Configuration reference](https://github.com/wilbowes/EchoMuse/blob/main/docs/configuration.md)
- [Rooting a device](https://github.com/wilbowes/EchoMuse/blob/main/docs/rooting.md)

**Note**: restart the add-on after changing configuration.
