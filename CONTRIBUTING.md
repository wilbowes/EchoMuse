# Contributing

Contributions are welcome, and the most useful ones are usually small. This
page exists so you can tell, before spending an evening, whether a change is
wanted and what shape it should take.

## The most valuable thing you can send

**A bug report with a support bundle.** Dashboard → Support → Download bundle.
It is an allowlist — no transcripts, no recordings, no network names, no
account names — and it carries the controller log, per-device logs, versions,
config and link metrics. Remote diagnosis used to cost days per round trip;
with a bundle it is usually one.

Hardware findings are the next most valuable, because most of us have one kind
of Echo in one kind of room. If something behaves differently on your unit,
that is worth an issue on its own.

## Where to help

Every open issue carries three labels, and between them they should tell you
whether to start writing code.

**State — read this one first.**

| | |
|---|---|
| `ready` | Specified well enough that a PR can be reviewed against the issue text. Start here. |
| `needs-design` | The problem is agreed, the solution is not. Comment with an approach before writing code. |
| `needs-decision` | Waiting on a call about direction. A PR cannot settle it. |
| `blocked` | Waiting on another issue, named in the body. |
| `needs-reporter` | Waiting on someone outside the project. |
| `needs-triage` | Just arrived; a maintainer has not read it properly yet. |

**Area** — `area:device` (Go, on the Echo), `area:controller` (Python server),
`area:dashboard`, `area:provisioning` (rooting, wizard, OTA), `area:ha`
(add-on, ESPHome, entities), `area:forge` (wake word trainer), `area:docs`.

**Signal** — `good first issue` is small and self-contained. `help wanted` is
something we would rather not do ourselves. `hardware:welcome` needs a rooted
Echo to do at all, and those are the ones we most want help with, because most
of us have one kind of Echo in one kind of room. `needs-hardware` is the
opposite: it needs measurement on our own bench before anyone can act.

**Before you start, check nobody else has.** `claimed` means there is an open
PR — GitHub only lets us assign collaborators, so that label is how an outside
contributor's work in progress is marked. Say so on the issue when you pick
one up and we will add it.

`ready` is an invitation, and we mean it. If an issue is labelled `ready` and
a PR arrives that implements it, that PR was not premature — if we got the
label wrong, that is ours to fix, not yours.

### Quick links

Saved searches for the common questions. Counts move; the filters do not.

- [Ready to pick up](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aready%20-label%3Aclaimed%20-label%3Aneeds-hardware) — Specified, unclaimed, nothing external blocking it.
- [Good first issue](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3A%22good%20first%20issue%22%20-label%3Aclaimed) — Small and self-contained.
- [Needs a device](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Ahardware%3Awelcome) — Can only be done with a rooted Echo — the ones we most want help with.
- [Open bugs](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Abug%20label%3Aready) — Confirmed and specified.
- [Design not settled](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aneeds-design) — Comment before writing code.
- [Someone is on it](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aclaimed) — Check before starting.

By area: [device](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Adevice) · [controller](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Acontroller) · [dashboard](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Adashboard) · [provisioning](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Aprovisioning) · [ha](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Aha) · [forge](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Aforge) · [docs](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20label%3Aarea%3Adocs)

By release: [2.21.0](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20milestone%3A2.21.0) · [2.22.0](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20milestone%3A2.22.0) · [3.0.0](https://github.com/wilbowes/EchoMuse/issues?q=is%3Aopen%20is%3Aissue%20milestone%3A3.0.0)

## Milestones and releases

Issues are grouped by the release they are meant to land in, and a milestone
is a theme rather than a bucket:

- **2.21.0** — controller GA, in early access now. Closed to new work.
- **2.22.0** — link resilience. The audio path survives a bad link (#140).
- **3.0.0** — Sendspin multi-room, and the output chain moves to the device
  (#272).

An issue with no milestone is not scheduled, which is not the same as
unwanted. A `ready` issue outside a milestone is still a fine thing to pick
up.

## Before you open a PR

```bash
cd controller && python -m pytest tests/    # needs pytest numpy scipy pyyaml
cd device && go test ./... && go vet ./...
```

Both run in CI on every push. **Please add a test if your change is pure
logic** — the controller suite deliberately cannot import `em_controller` or
`em_esphome` (they pull in aiohttp, zeroconf and openwakeword), so decisions
that need coverage get extracted into their own module: see `em_button`,
`em_linkauth`, `em_turnclock`, `em_barge`. Following that pattern is the
single easiest way to get a change reviewed quickly.

Hardware-dependent code is not testable on the host and nobody expects you to
fake it. Say what you tested it on.

## What we maintain, so you do not have to

Please **leave these out of your PR** — we will write them once the change
lands. This is not about ownership of the project, it is that these files are
either generated or written from context that only shows up in maintenance.

- **`CLAUDE.md`, `device/CLAUDE.md`, `controller/CLAUDE.md`** — a maintainer
  log rather than documentation. Most of it is "here is what we got wrong,
  why, and what not to try again", written after being burnt. It is also the
  most conflict-prone file in the repo.
- **`CHANGELOG.md`** — written per release, for the person deciding whether to
  update.
- **`controller-ea/`** — generated by `controller/tools/sync_channels.py`.
  Never edit it; a test fails on drift.
- **Version pins** in `controller/config.yaml` and release tags.

**`docs/` is fair game** and improvements there are welcome — it is the
user-facing documentation, and it is usually the thing that is out of date.

## A few rules that will bite you

Each of these has already cost somebody a bad day, so they are worth knowing
before you write code rather than after review.

- **`em_db.MIGRATIONS` is append-only.** The stored `schema_version` is an
  index into that list, so editing a deployed entry corrupts every database
  that already ran it. Add a new entry.
- **Negotiate by capability, not by version.** Devices announce what they
  implement; the controller reads `Device.capabilities`. Comparing version
  strings puts release history in the controller and misjudges dev builds.
- **Degrade to the old behaviour, never to a wrong answer.** A measurement
  that is absent stores as NULL, not 0 — "we did not measure this" and "this
  was zero" are different facts and the stats are read as if they are true.
- **Home Assistant entity keys are append-only.** HA keys its registry on
  them, so renumbering renames everyone's entities and breaks their
  automations.
- **Anything in `controller/device_payloads/` needs an update path**, and a
  test enforces it. A payload with no way to reach fielded devices means every
  user updates it by hand.

If a change bumps into one of these and there is genuinely no way around it,
say so in the PR — that is a discussion, not a rejection.

## Writing for people

Anything a person reads — PR comments, issue replies, release notes — leads
with the answer and stays short. Evidence is the number, not the derivation.
The exception is anything irreversible or anything asking someone to act on
their own hardware, where the warning stays whatever it costs in length.

Commit messages are the opposite: they are the record, and their density is
the point. Say why, not just what.

## Licence

By contributing you agree your work is licensed under the MIT licence in
`LICENSE`. If you add a third-party component, add it to `NOTICE.md` and keep
its licence text beside the code.
