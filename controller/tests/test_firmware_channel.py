"""
EA firmware (vX.Y.Z-ea.N, a GitHub prerelease) reaches EA and dev controllers
only. A GA controller offers GA firmware alone, and never offers a device a
version older than the one it runs.
"""

from version import (choose_firmware_release, firmware_key, firmware_update,
                     offers_ea_firmware)


def test_ordering():
    order = ["v2.16.0", "v2.16.0-78-g08163f4", "v2.17.0-ea.1", "v2.17.0-ea.2",
             "v2.17.0-ea.10", "v2.17.0", "v2.17.0-3-gabc1234", "v2.18.0-ea.1"]
    keys = [firmware_key(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_what_is_not_a_version():
    for v in ("20260926-1044-dev", "dev", "", None, "v2.17", "v2.17.0-ea.x"):
        assert firmware_key(v) is None, v


def test_ea_firmware_is_not_downgraded_by_a_ga_controller():
    assert not firmware_update("v2.17.0-ea.1", "v2.16.0")


def test_ea_moves_to_the_ga_of_the_same_number():
    assert firmware_update("v2.17.0-ea.3", "v2.17.0")
    assert firmware_update("v2.17.0-ea.3", "v2.17.0-ea.4")
    assert not firmware_update("v2.17.0", "v2.17.0-ea.4")


def test_a_dev_build_is_still_offered_a_release():
    assert firmware_update("20260926-1044-dev", "v2.17.0")
    assert not firmware_update(None, "v2.17.0")
    assert not firmware_update("v2.17.0", None)


def test_only_a_plain_release_build_is_a_ga_controller():
    assert not offers_ea_firmware("v2.25.0")
    assert not offers_ea_firmware("2.25.0")
    for v in ("v2.25.0-ea.1", "v2.24.1-72-ge68d134", "dev",
              "dev-controller-v2.24.1-68-g823e740", ""):
        assert offers_ea_firmware(v), v


def _rel(tag, pre=False, draft=False, binary=True):
    return {"tag_name": tag, "prerelease": pre, "draft": draft,
            "assets": [{"name": "server"}] if binary else []}


RELEASES = [  # newest first by date, as GitHub lists them
    _rel("v2.17.0-ea.2", pre=True),
    _rel("controller-v2.25.0", binary=False),
    _rel("v2.17.0-ea.1", pre=True),
    _rel("v2.16.0"),
    _rel("v2.15.0"),
]


def test_a_ga_controller_never_picks_ea():
    assert choose_firmware_release(RELEASES, include_ea=False)["tag_name"] == "v2.16.0"


def test_a_ga_controller_skips_an_ea_tag_even_without_the_flag():
    rs = [_rel("v2.17.0-ea.1", pre=False), _rel("v2.16.0")]
    assert choose_firmware_release(rs, include_ea=False)["tag_name"] == "v2.16.0"


def test_an_ea_controller_picks_the_newest_of_both():
    assert choose_firmware_release(RELEASES, include_ea=True)["tag_name"] == "v2.17.0-ea.2"
    rs = [_rel("v2.17.0-ea.2", pre=True), _rel("v2.17.0")]
    assert choose_firmware_release(rs, include_ea=True)["tag_name"] == "v2.17.0"


def test_by_version_not_list_order():
    rs = [_rel("v2.9.0"), _rel("v2.10.0")]
    assert choose_firmware_release(rs, include_ea=False)["tag_name"] == "v2.10.0"


def test_drafts_and_releases_without_a_binary_are_skipped():
    rs = [_rel("v2.18.0", draft=True), _rel("v2.17.0", binary=False), _rel("v2.16.0")]
    assert choose_firmware_release(rs, include_ea=True)["tag_name"] == "v2.16.0"
