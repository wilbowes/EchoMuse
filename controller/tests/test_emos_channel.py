"""Early Access emOS: emos-vX.Y-ea.N, published as a prerelease.

The firmware's rules (test_firmware_channel), applied to emOS: a GA
controller never offers EA emOS, by flag or by name; an EA or dev controller
offers the newest of both; ordering is by version, never list order; and an
EA device is not offered the GA of an older number as an "update".
"""

import re
from pathlib import Path

import em_emos_update as up


def _rel(tag, pre=False, draft=False, init=True):
    return {"tag_name": tag, "prerelease": pre, "draft": draft,
            "assets": [{"name": "init"}, {"name": "emos-payload.zip"}] if init
                      else [{"name": "emos-payload.zip"}]}


RELEASES = [  # newest first by date, as GitHub lists them
    _rel("emos-v0.11-ea.2", pre=True),
    _rel("v2.20.0-ea.1", pre=True),
    _rel("emos-v0.11-ea.1", pre=True),
    _rel("emos-v0.10"),
    _rel("emos-v0.9"),
]


def test_ea_orders_before_its_ga_and_after_the_one_before():
    assert (up.version_key("emos-v0.10") < up.version_key("emos-v0.11-ea.1")
            < up.version_key("emos-v0.11-ea.2") < up.version_key("emos-v0.11")
            < up.version_key("emos-v0.11-2-gabc1234"))


def test_a_ga_controller_never_picks_ea():
    assert up.choose_release(RELEASES, include_ea=False)["tag_name"] == "emos-v0.10"


def test_a_ga_controller_skips_an_ea_tag_even_without_the_flag():
    rs = [_rel("emos-v0.11-ea.1"), _rel("emos-v0.10")]
    assert up.choose_release(rs, include_ea=False)["tag_name"] == "emos-v0.10"


def test_an_ea_controller_picks_the_newest_of_both():
    assert up.choose_release(RELEASES, include_ea=True)["tag_name"] == "emos-v0.11-ea.2"
    rs = [_rel("emos-v0.11-ea.2", pre=True), _rel("emos-v0.11")]
    assert up.choose_release(rs, include_ea=True)["tag_name"] == "emos-v0.11"


def test_by_version_not_list_order():
    rs = [_rel("emos-v0.9"), _rel("emos-v0.10")]
    assert up.choose_release(rs, include_ea=False)["tag_name"] == "emos-v0.10"


def test_drafts_firmware_and_releases_without_an_init_are_skipped():
    rs = [_rel("emos-v0.12", draft=True), _rel("emos-v0.11", init=False),
          _rel("v2.30.0"), _rel("emos-v0.10")]
    assert up.choose_release(rs, include_ea=True)["tag_name"] == "emos-v0.10"
    assert up.choose_release([], include_ea=True) is None


def test_an_ea_device_is_offered_the_next_ea_and_its_ga_but_nothing_older():
    assert up.update_available("emos-v0.11-ea.1", "emos-v0.11-ea.2")
    assert up.update_available("emos-v0.11-ea.2", "emos-v0.11")
    assert not up.update_available("emos-v0.11-ea.1", "emos-v0.10")
    assert up.offer("emos-v0.11-ea.1", "emos-v0.10") == "current"
    assert up.can_target("emos-v0.11-ea.1")


def test_the_release_workflow_refuses_what_the_controller_cannot_read():
    wf = (Path(__file__).resolve().parents[2]
          / ".github/workflows/emos-release.yml").read_text()
    pat = re.search(r"grep -Eq '(\^emos-v[^']+)'", wf).group(1)
    for good in ("emos-v0.1", "emos-v0.10", "emos-v1.0.2", "emos-v0.11-ea.1"):
        assert re.fullmatch(pat, good), good
        assert up.version_key(good) is not None, good
    for bad in ("emos-v0.11;id", "emos-v0.11-rc1", "emos-v0", "emos-v0.11-ea."):
        assert not re.fullmatch(pat, bad), bad
    # An -ea tag is published as a prerelease, which is half of what keeps it
    # off the GA fleet; the name is the other half.
    assert "*-ea.*) echo \"prerelease=true\"" in wf
    assert "prerelease: ${{ steps.channel.outputs.prerelease }}" in wf
