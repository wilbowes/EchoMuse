# EchoMuse Documentation

User-facing documentation, written to be readable without an engineering
background. Intended as the seed of a future wiki — screenshots and
walkthroughs welcome.

| Document | What it covers |
|---|---|
| [Quickstart](quickstart.md) | Zero to talking to your Dot: controller install, first-run setup, device approval, Home Assistant hookup, everyday use. |
| [Configuration Guide](configuration.md) | Every dashboard setting explained in plain language — what it does, when to touch it, and how to tune it. Ends with [what leaves your network](configuration.md#what-leaves-your-network) — there is no telemetry, and the one outbound connection is named. |
| [The Voice Pipeline, Explained](voice-pipeline.md) | How your voice travels from the microphones to Home Assistant and back, stage by stage, with the benefits and caveats of each design choice. |
| [FAQ](faq.md) | Quick answers and workarounds for the things that come up most — rooting refusals, wizard failures, update problems, wake word tuning, privacy. Check here before filing. |
| [User Acceptance Testing](uat.md) | A checklist for confirming EchoMuse does what it claims on your hardware, and how to report what doesn't. Includes the known faults not worth re-filing. |
| [Moving to the Home Assistant add-on](migrate-to-addon.md) | Migrating an existing Docker install to the add-on without losing your devices, settings or Home Assistant entities. Read the part about `tls/` before you start. |

Deeper technical references live elsewhere:

- [support-bundle.md](support-bundle.md) — what a support bundle contains,
  what it deliberately excludes, and how to check before you share one.
- [rooting.md](rooting.md) — what a device needs before EchoMuse can use it.
  The exploit itself is R0rt1z2's work on XDA Forums and that thread is canon;
  this covers where EchoMuse picks up, and what the wizard does for you.
- [device-controller-interface.md](device-controller-interface.md) — the wire
  contract a device binary implements: the three WebSocket planes, capability
  negotiation, `/control` messages, `/data` frames, config push, link auth, and
  the `crown` board profile. Read this before building bindings for a new board.
- [audio-states.md](audio-states.md) — who owns the speaker and what is on the
  wire: the two audio planes, ducking, flush semantics, and the open questions
  about how voice, music, announcements and alarms interact.
- [led-ring-states.md](led-ring-states.md) — the ring's state model: owner
  priority, link availability, and the button/audio event tables.
- [SETUP.md](../SETUP.md) — architecture reference: how the mic array, the
  audio pipeline and the device/controller protocol actually work, plus
  troubleshooting. Not an onboarding guide.
- [JOURNAL.md](../JOURNAL.md) — the engineering journal: a long-form,
  chronological record of what was built, what broke, and what we got wrong.
- [CLAUDE.md](../CLAUDE.md) — codebase orientation for developers (and AI
  assistants).
