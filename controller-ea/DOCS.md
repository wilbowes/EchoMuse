# EchoMuse — Early Access

This is the **Early Access** channel: the next controller release,
before it is general. It is the same program as the stable add-on
and the same settings; only the version differs.

**Install this instead of the stable add-on, not alongside it.**
Both use host networking and the same ports (8767, 8768, 8770), so
whichever starts second will fail to bind.

**Switching channels is a migration, not a toggle.** Home Assistant
add-ons do not share storage, so this starts with an empty database
and a newly generated certificate authority. Existing devices hold
the stable add-on's CA, and they take `wss` from the mDNS record
rather than from `require_device_tls` — so they will fail
verification and will not connect at all until you copy the stable
add-on's `/data` across, including all four files in `tls/`.

**Satellite ports differ by channel**, and that is deliberate. The
stable add-on hands out 16001 upward for voice satellites and 17001
upward for Bluetooth proxies; this one uses 16101 and 17101. Home
Assistant keys an ESPHome device on its host and port, so shared
ranges would let an entry left over from the other channel connect
to a different device entirely — the wake word fires, the ring
lights, and the turn dies with no pipeline behind it. With the
ranges apart, a stale entry simply shows as unavailable.

Report anything you find against the EchoMuse repository, saying
which channel you are on.

---

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
