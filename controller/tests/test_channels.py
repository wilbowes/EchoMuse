"""
Release-channel guards.

A channel is a second add-on with its own slug, so it is a second
config.yaml describing the same program. Keeping two in step by hand is the
failure this project has already paid for once: a stale pin in one file
shipped a controller with no ingress support, presenting as two unrelated
faults (#160). Two files multiply that by every option, schema entry and
permission.

So the EA add-on is generated, and these tests fail if the committed copy
is not what the generator produces. The generator and the guard state the
same rule twice, which turns drift into a red test instead of a support
thread.
"""

import sys
from pathlib import Path

import pytest
import yaml

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER / "tools"))

import sync_channels  # noqa: E402

GA = yaml.safe_load((CONTROLLER / "config.yaml").read_text())
EA_PATH = sync_channels.EA.path

# Options whose VALUE belongs to the channel rather than to the program.
# Every other option must match: a setting reachable in one channel and not
# the other is the deployment-parity failure one level up. Adding to this set
# is a deliberate act — the default is that channels are identical.
CHANNEL_OWNED_OPTIONS = {"esphome_port_base"}


def _ea():
    assert EA_PATH.joinpath("config.yaml").is_file(), (
        "controller-ea/config.yaml is missing — run "
        "controller/tools/sync_channels.py")
    return yaml.safe_load((EA_PATH / "config.yaml").read_text())


# ── No drift ──────────────────────────────────────────────────────────────────

def test_the_committed_channel_matches_the_generator():
    problems = sync_channels.check(sync_channels.EA)
    assert not problems, (
        "Channel add-on is out of date:\n  " + "\n  ".join(problems) +
        "\n\nRun: controller/tools/sync_channels.py")


@pytest.mark.parametrize("key", [
    "arch", "host_network", "ingress", "ingress_port", "panel_admin",
    "panel_icon", "environment", "options", "schema",
    "image", "init", "url",
])
def test_channel_shares_every_non_identity_field_with_ga(key):
    """
    A setting reachable in one channel and not the other is the divergence
    the deployment-parity rule exists to prevent, one level up: it would
    make an EA bug report unanswerable without first asking which add-on
    the person installed.
    """
    ea = _ea()
    if key not in GA:
        pytest.skip(f"GA config has no {key}")
    if key == "options":
        # Every option's VALUE is shared except the ones that are part of a
        # channel's identity — see CHANNEL_OWNED_OPTIONS and the test below.
        # The key SET is still compared in full, two tests down.
        ea_opts = {k: v for k, v in ea[key].items()
                   if k not in CHANNEL_OWNED_OPTIONS}
        ga_opts = {k: v for k, v in GA[key].items()
                   if k not in CHANNEL_OWNED_OPTIONS}
        assert ea_opts == ga_opts, (
            "options differ between channels — regenerate rather than editing")
        return
    assert ea.get(key) == GA[key], (
        f"{key} differs between channels — regenerate rather than editing")


def test_options_and_schema_agree_key_for_key():
    # Belt and braces over the field comparison above: an option present in
    # one channel's schema but not the other is a validation difference that
    # only shows up when a user sets it.
    ea = _ea()
    assert set(ea["options"]) == set(GA["options"])
    assert set(ea["schema"]) == set(GA["schema"])


# ── Identity, which must differ ───────────────────────────────────────────────

def test_channel_has_its_own_slug():
    """
    The slug is the add-on's identity AND the name of its /data directory.
    Sharing one would make installing EA an in-place replacement of GA
    rather than a choice, and there would be no way back.
    """
    ea = _ea()
    assert ea["slug"] != GA["slug"]
    assert ea["slug"] == "controller-ea"


def test_channel_is_distinguishable_in_the_ui():
    # Two add-ons called "EchoMuse" with two identical panels is a support
    # thread waiting to happen.
    ea = _ea()
    assert ea["name"] != GA["name"]
    assert ea["panel_title"] != GA["panel_title"]


def test_channels_hand_out_satellite_ports_from_disjoint_ranges():
    """
    Home Assistant keys an ESPHome device on its host and port, and the two
    channels have separate databases — so both counters would otherwise start
    at 16001 and hand the same numbers to different devices.

    Measured 2026-08-19, switching channels with shared ranges: every
    satellite entity unavailable for a day, wake words still firing and turns
    dying in milliseconds because HA had no pipeline behind them. With the
    ranges apart a stale entry visibly fails to connect instead.

    The gap must also exceed any realistic device count, since a channel that
    allocated its way into the next one's range would reintroduce exactly
    this. BLE proxies ride at +BLE_PORT_OFFSET, so separating the voice bases
    separates those too.
    """
    ea = _ea()
    ga_base = GA["options"]["esphome_port_base"]
    ea_base = ea["options"]["esphome_port_base"]

    assert ea_base != ga_base, (
        "Both channels allocate satellite ports from the same base — a Home "
        "Assistant config entry from one will reach the other's devices")
    assert abs(ea_base - ga_base) >= 100, (
        f"Only {abs(ea_base - ga_base)} ports between the channel bases — a "
        f"fleet that size would allocate into the other channel's range")

    # The type is shared even though the value is not: a channel may not
    # loosen validation the other enforces.
    assert ea["schema"]["esphome_port_base"] == GA["schema"]["esphome_port_base"]


def test_channel_pulls_the_same_image_repository():
    # Channels differ by TAG, not by artefact source. A second repository
    # would need a second publish path and could drift in ways no test here
    # could see.
    ea = _ea()
    assert ea["image"] == GA["image"]


def test_channel_version_is_independent_of_ga():
    """
    version: is the one field a release moves, and it must survive a sync —
    regenerating EA must never quietly drag its pin back to GA's, which
    would ship GA code to everyone who opted into EA.
    """
    ea_before = (EA_PATH / "config.yaml").read_text()
    generated = sync_channels.generate(sync_channels.EA)["config.yaml"]
    import re
    got = re.search(r'^version:\s*"(.*)"', generated, re.M).group(1)
    want = re.search(r'^version:\s*"(.*)"', ea_before, re.M).group(1)
    assert got == want, "sync_channels must preserve the channel's own version"


def test_the_channel_has_a_documentation_tab():
    """
    Home Assistant renders DOCS.md on the Documentation tab. An empty tab is
    worst on the channel someone went out of their way to install — and it
    is where the migration warning has to live, since switching channels
    leaves existing devices unable to connect at all.
    """
    docs = EA_PATH / "DOCS.md"
    assert docs.is_file(), "controller-ea/DOCS.md is missing"
    text = docs.read_text()
    assert "Early Access" in text
    # The two things that will actually bite someone who installs this.
    assert "instead of the stable add-on" in text
    assert "tls/" in text


def test_generated_file_says_it_is_generated():
    # Someone WILL open this file to change a setting.
    text = (EA_PATH / "config.yaml").read_text()
    assert "DO NOT EDIT" in text
    assert "sync_channels.py" in text


def test_every_channel_documents_the_version_it_pins():
    """
    Supervisor shows an add-on's CHANGELOG.md when offering an update, and
    says "No changelog found" when there is none — precisely when someone is
    deciding whether to take it.

    Two separate misses, both found on a real update (2026-08-16):

      - controller-ea/ had NO changelog at all, because sync_channels.py's
        PRESENTATION tuple listed the translations, icon and logo and not
        this file. The generated channel therefore shipped without the one
        thing the update dialog reads.
      - GA 2.19.0 shipped with no entry of its own; the file jumped from
        1.0.1 to an Early Access heading.

    A release's notes also live in its tag annotation (the dashboard's own
    update notice reads that), so it is easy to write them there, see them
    rendered, and never notice Supervisor showing nothing.
    """
    for path in (CONTROLLER, CONTROLLER.parent / "controller-ea"):
        config = yaml.safe_load((path / "config.yaml").read_text())
        version = str(config["version"])

        changelog = path / "CHANGELOG.md"
        assert changelog.is_file(), (
            f"{path.name}/CHANGELOG.md is missing — Supervisor's update "
            f"dialog will read 'No changelog found'")

        assert version in changelog.read_text(), (
            f"{path.name}/CHANGELOG.md never mentions {version}, the version "
            f"its config.yaml pins — an update to it shows notes for some "
            f"other release")
