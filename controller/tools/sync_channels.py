#!/usr/bin/env python3
"""
sync_channels.py — generate the Early Access add-on from the GA one.

Home Assistant has no notion of a release channel: one add-on is one
version. A channel is therefore a SECOND ADD-ON with its own slug, which
users opt into by installing it — the same shape ESPHome uses (esphome /
esphome-beta / esphome-dev).

That means two config.yaml files describing the same program, and keeping
them in step by hand is the failure this project has already paid for:
config.yaml pinning a version whose image predated the add-on shipped a
controller with no ingress support, presenting as two unrelated faults
(#160). Two files multiply that by every option, every schema entry and
every permission.

So EA is GENERATED, never edited. Everything except the add-on's identity
is copied verbatim, and tests/test_channels.py fails if the committed copy
does not match what this produces — the generator and the guard are the
same rule stated twice, so drift is a red test rather than a support
thread.

Deliberately NOT generated: `version:`. Channels advance independently —
EA pinning the GA version would defeat the entire point — so it is
preserved from the existing file when there is one, and seeded from GA on
first creation.

Usage, from controller/:
    tools/sync_channels.py                     # write controller-ea/
    tools/sync_channels.py --check             # exit 1 if out of date
    tools/sync_channels.py --set-version 2.20.0-ea.1

`version:` is the one field a release moves, and the generated file says DO
NOT EDIT — so moving it is a command rather than a hand-edit nobody is sure
is allowed. It must match the tag being released: controller-release.yml
refuses to build when the tag and the channel's pin disagree, which is the
guard that exists because a stale pin once shipped an add-on whose image
predated the add-on itself (#160).
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
REPO = CONTROLLER.parent

# Files a pull-only add-on needs. EA carries no build context at all — it
# pulls the same published image as GA, differing only in which tag — so
# there is no Dockerfile and no source here to fall out of step.
PRESENTATION = ("translations/en.yaml", "icon.png", "logo.png",
                "CHANGELOG.md")


class Channel:
    def __init__(self, dirname: str, slug: str, name: str, panel_title: str,
                 blurb: str, docs_banner: str, esphome_port_base: int):
        self.dirname = dirname
        self.slug = slug
        self.name = name
        self.panel_title = panel_title
        self.blurb = blurb
        self.docs_banner = docs_banner
        # Part of the channel's identity, not a setting that drifted: the
        # two channels must hand out satellite ports from disjoint ranges,
        # or a Home Assistant config entry left over from one reaches a
        # device belonging to the other. See em_db.ESPHOME_PORT_BASE.
        self.esphome_port_base = esphome_port_base

    @property
    def path(self) -> Path:
        return REPO / self.dirname


EA = Channel(
    dirname="controller-ea",
    slug="controller-ea",
    name="EchoMuse (Early Access)",
    panel_title="EchoMuse EA",
    blurb=(
        "Early Access build of the EchoMuse controller — the next release, "
        "before it is general. Install this INSTEAD of the stable add-on, "
        "not alongside it: both use host networking and the same ports. "
        "Add-ons do not share storage, so switching channels starts with an "
        "empty database and a new certificate authority, and existing "
        "devices will not connect until the stable add-on's /data is copied "
        "across."
    ),
    esphome_port_base=16101,
    docs_banner=(
        "# EchoMuse — Early Access\n"
        "\n"
        "This is the **Early Access** channel: the next controller release,\n"
        "before it is general. It is the same program as the stable add-on\n"
        "and the same settings; only the version differs.\n"
        "\n"
        "**Install this instead of the stable add-on, not alongside it.**\n"
        "Both use host networking and the same ports (8767, 8768, 8770), so\n"
        "whichever starts second will fail to bind.\n"
        "\n"
        "**Switching channels is a migration, not a toggle.** Home Assistant\n"
        "add-ons do not share storage, so this starts with an empty database\n"
        "and a newly generated certificate authority. Existing devices hold\n"
        "the stable add-on's CA, and they take `wss` from the mDNS record\n"
        "rather than from `require_device_tls` — so they will fail\n"
        "verification and will not connect at all until you copy the stable\n"
        "add-on's `/data` across, including all four files in `tls/`.\n"
        "\n"
        "**Satellite ports differ by channel**, and that is deliberate. The\n"
        "stable add-on hands out 16001 upward for voice satellites and 17001\n"
        "upward for Bluetooth proxies; this one uses 16101 and 17101. Home\n"
        "Assistant keys an ESPHome device on its host and port, so shared\n"
        "ranges would let an entry left over from the other channel connect\n"
        "to a different device entirely — the wake word fires, the ring\n"
        "lights, and the turn dies with no pipeline behind it. With the\n"
        "ranges apart, a stale entry simply shows as unavailable.\n"
        "\n"
        "Report anything you find against the EchoMuse repository, saying\n"
        "which channel you are on.\n"
        "\n"
        "---\n"
        "\n"
    ),
)

# Identity lines rewritten per channel. Everything else is copied byte for
# byte, which is the point: an option, a schema entry or a permission added
# to GA reaches EA without anyone remembering to do it.
def _render(ga_config: str, ch: Channel, version: str) -> str:
    out = ga_config

    header = (
        "# GENERATED FILE — DO NOT EDIT.\n"
        "#\n"
        "# Produced from controller/config.yaml by controller/tools/\n"
        "# sync_channels.py, and checked by tests/test_channels.py. Edit the\n"
        "# GA config and re-run the generator; an edit here is reverted by\n"
        "# the next sync and fails CI in the meantime.\n"
        "#\n"
        "# `version:` is the exception — channels advance independently, so\n"
        "# it is preserved across syncs and is the one field a release moves.\n"
        "#\n"
        "# Comments below are inherited verbatim from the GA config and\n"
        "# describe it: this directory has no Dockerfile and never builds,\n"
        "# it only pulls the tag named by `version:`.\n"
        "#\n"
    )

    # Matches the integer default in options:, never the quoted type in
    # schema: — the two lines share a key name and only one of them is a
    # channel's own value.
    out = re.sub(r'^(\s*)esphome_port_base:\s*\d+\s*$',
                 rf'\1esphome_port_base: {ch.esphome_port_base}',
                 out, count=1, flags=re.M)

    out = re.sub(r'^name:.*$',        f'name: "{ch.name}"',        out, count=1, flags=re.M)
    out = re.sub(r'^slug:.*$',        f'slug: "{ch.slug}"',        out, count=1, flags=re.M)
    out = re.sub(r'^panel_title:.*$', f'panel_title: {ch.panel_title}', out, count=1, flags=re.M)
    out = re.sub(r'^version:.*$',     f'version: "{version}"',     out, count=1, flags=re.M)

    # description: is a single long quoted scalar in the GA file.
    out = re.sub(r'^description:.*$', f'description: "{ch.blurb}"', out, count=1, flags=re.M)

    return header + out


def _current_version(path: Path, fallback: str) -> str:
    if not path.is_file():
        return fallback
    m = re.search(r'^version:\s*"(.*)"\s*$', path.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else fallback


def generate(ch: Channel) -> dict[str, str]:
    """Return {relative path: content} for the channel, without writing."""
    ga = (CONTROLLER / "config.yaml").read_text(encoding="utf-8")
    ga_version = _current_version(CONTROLLER / "config.yaml", "0.0.0")
    version = _current_version(ch.path / "config.yaml", ga_version)
    out = {"config.yaml": _render(ga, ch, version)}

    # DOCS.md is what Home Assistant renders on the Documentation tab.
    # Without it that tab is empty — on the channel a user is most likely to
    # want instructions for, since it is the one they went out of their way
    # to install. Banner-prefixed rather than copied verbatim: the GA
    # document describes the stable add-on, and the differences that matter
    # here are the ones a channel introduces.
    ga_docs = CONTROLLER / "DOCS.md"
    if ga_docs.is_file():
        out["DOCS.md"] = ch.docs_banner + ga_docs.read_text(encoding="utf-8")
    return out


def write(ch: Channel) -> None:
    ch.path.mkdir(parents=True, exist_ok=True)
    for rel, content in generate(ch).items():
        (ch.path / rel).write_text(content, encoding="utf-8")
    for rel in PRESENTATION:
        src = CONTROLLER / rel
        if not src.is_file():
            continue
        dst = ch.path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def check(ch: Channel) -> list[str]:
    """Return a list of human-readable drift descriptions ([] if in step)."""
    problems: list[str] = []
    for rel, content in generate(ch).items():
        path = ch.path / rel
        if not path.is_file():
            problems.append(f"{ch.dirname}/{rel} is missing")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"{ch.dirname}/{rel} differs from the generated form")
    for rel in PRESENTATION:
        src = CONTROLLER / rel
        if not src.is_file():
            continue
        dst = ch.path / rel
        if not dst.is_file():
            problems.append(f"{ch.dirname}/{rel} is missing")
        elif src.read_bytes() != dst.read_bytes():
            problems.append(f"{ch.dirname}/{rel} differs from controller/{rel}")
    return problems


def main(argv: list[str]) -> int:
    channels = [EA]

    if "--set-version" in argv:
        i = argv.index("--set-version")
        if i + 1 >= len(argv):
            print("--set-version needs a value, e.g. 2.20.0-ea.1",
                  file=sys.stderr)
            return 2
        wanted = argv[i + 1]
        for ch in channels:
            write(ch)  # ensure the file exists before rewriting its pin
            path = ch.path / "config.yaml"
            text = path.read_text(encoding="utf-8")
            text = re.sub(r'^version:.*$', f'version: "{wanted}"', text,
                          count=1, flags=re.M)
            path.write_text(text, encoding="utf-8")
            print(f"{ch.dirname} pinned to {wanted}")
        return 0

    if "--check" in argv:
        problems = [p for ch in channels for p in check(ch)]
        if problems:
            print("Channel add-ons are out of date:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print("\nRun: controller/tools/sync_channels.py", file=sys.stderr)
            return 1
        print("Channel add-ons are in step with controller/")
        return 0

    for ch in channels:
        write(ch)
        print(f"wrote {ch.dirname}/ "
              f"(version {_current_version(ch.path / 'config.yaml', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
