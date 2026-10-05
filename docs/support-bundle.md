# Support bundles

When something misbehaves and it isn't obvious why, a support bundle collects
the diagnostics in one file you can attach to a
[GitHub issue](https://github.com/wilbowes/EchoMuse/issues).

**Settings → Support → Collect bundle**, then Download, or
`GET /api/support/bundle` if you prefer the API. Admin only.

It is a plain JSON file. **Open it before you send it** — it is readable, and
you should be able to satisfy yourself about the contents rather than take
this page's word for it.

## What it deliberately does not contain

A bundle is meant to be attached to a public issue, so anything private in it
is public the moment you send it, permanently. It is built as an **allowlist**:
every field is named individually and everything else is dropped. A new
column added to the database is excluded until someone deliberately includes
it — the failure mode is that support loses a field, never that your data
leaks.

Excluded, with no option to include them:

| Not included | Why |
|---|---|
| **Anything you said** — transcripts, `stt_text`, saved audio | Speech from inside your home. There is no opt-in flag, because a flag is a thing people tick and this one cannot be untickled once the file is public. |
| **Device labels** | You wrote them, and they routinely contain names — "Bedroom - Sam" is a real example. Replaced with `device-1`, `device-2`… |
| **Network identifiers** — WiFi SSID, BSSID, IP addresses | An SSID is geolocatable from public wardriving databases, so publishing one discloses roughly where you live. |
| **Credentials** — device tokens, ESPHome PSKs, password hashes, login sessions | Obvious, but stated so it is checkable. |
| **Dashboard account names** | They appear in log lines like "Shell session opened by …", which nothing else here would have caught. Replaced with the account's role — `<admin>` — which is the part worth knowing. |
| **File paths** | A data directory is `/home/<your name>/…` on a bare-metal install, so the controller reports sizes only. |
| **URLs and quoted strings in log lines** | Media URLs carry provider paths and session tokens; quoted strings in turn traces carry transcripts. |

Log lines from sources known to carry speech are dropped **whole** rather than
edited, because partially redacting a line that quotes a transcript is a bet
on a regular expression.

## What it does contain, and why each part earns its place

| Included | Why it is needed |
|---|---|
| Controller version, schema version | Almost every "is this fixed?" question starts here. |
| Device serials | Nothing correlates without them. They identify your hardware to you; they are not otherwise meaningful. |
| Firmware version, rollback slot, approval state | Tells us whether a fix is even present on that device. |
| Userspace and kernel (`emos`/`fireos`, `armv7l`/`aarch64`, kernel release) | FireOS 5 and FireOS 6 run different kernels and some faults exist on only one. |
| **Capabilities** (`mic`, `oww_shadow`, `ambient_light`…) | Decides which Home Assistant entities exist at all. "The light sensor didn't appear" is answered here in one line. |
| Configuration — thresholds, EQ, LED scenes, wake model | Behaviour, not identity. Keys whose *name* looks credential-shaped are redacted anyway. |
| Turn metadata — outcome, wake score, stage latencies, underruns | What happened and how long each stage took. No words, just timings and outcomes. |
| Hourly metrics per device — CPU, memory, storage, temperature, RSSI, RTT | Trends. Signal strength is included; the network's name is not. |
| The controller's own CPU (1m/5m/1h), memory, storage and uptime | A device starving for audio can be the host running out of CPU, memory or disk. The three windows separate "busy right now" from "busy earlier", which need different answers. Sizes and counts only, never paths. |
| Wake counters — near-misses, on-device drops, inference timings | Wake-word behaviour without any audio. |
| Recent controller log lines, sanitised | What the controller itself was doing — the part that explains most "it did the wrong thing" reports. Quoted text and URLs removed. |
| Recent per-device log lines, sanitised | What each device reported. Repetitive memory dumps are thinned so they cannot crowd out the rest. |

Roughly the last 24 hours, capped per device.

## Reviewing one before you send it

Open the file and look at the top: `redaction` states the contract, and
`devices[].name` should read `device-1`, not your room names. Searching it for
your WiFi name or something you said should come up empty.

If you find anything in there you would rather not publish, **that is a bug
and we want to hear about it** — please report it privately rather than in a
public issue.

## If a provisioning step failed instead

A support bundle describes a device the controller already knows about. A Dot
that is halfway through the provisioning wizard is not one of those, so the
wizard collects its own file.

When a step fails it reads the device's state there and then, and a **Download
diagnostics** button appears next to the error. Collection happens
automatically, because by the time you have been asked to run `getprop` by
hand the device has usually been retried or rebooted and the state that failed
is gone. Sharing it is still your decision.

It carries what a failed step needs explaining: which step, the error, the
build and model, whether root and the package manager answered, free space,
and what the radio can see. It follows the same rules as a bundle: no speech,
no network names, no addresses. WiFi scan results keep the security flags and
frequencies, which are the part that explains a failure, with the network
names replaced by `network-1`, `network-2` and so on, and the one you were
trying to join marked.

Same advice: open it before you send it.

## Retention

The bundle is a file on your machine. Nothing is uploaded anywhere by
generating one; it goes wherever you choose to put it, and deleting it is
enough. Regenerate rather than keeping old ones around — they are only useful
alongside a current problem.
