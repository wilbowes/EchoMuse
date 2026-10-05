"""
Updating emOS in place: the decisions around the rebuild (em_emos_update).

The fixtures build a device's "running image" the way a provisioned device has
one — a synthetic reference run through the real packer — then update it the
way em_api does, so the checks are exercised against images of the shape they
will meet rather than against hand-made headers.
"""

import base64
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import em_emos_build as eb
import em_emos_update as up
import test_emos_build as fx

REPO = Path(__file__).resolve().parents[2]


def installed(version="emos-v0.9", arch="arm64", system_part=13, init_fill=1):
    """An emOS image as the wizard would have flashed it."""
    zimage = fx.arm64_zimage() if arch == "arm64" else fx.arm_zimage()
    machine, cls = (183, 2) if arch == "arm64" else (40, 1)
    init = fx.fake_init(machine=machine, elf_class=cls, size=4096 + init_fill)
    ref = fx.make_reference(zimage=zimage)
    return eb.build_emos_image(ref, init, version, system_part=system_part)["image"]


def new_init(arch="arm64"):
    machine, cls = (183, 2) if arch == "arm64" else (40, 1)
    return fx.fake_init(machine=machine, elf_class=cls, size=8192) \
        + up.TRIAL_MARK.encode() + b"\0"


def updated(reference, version="emos-v0.10", arch="arm64"):
    init = new_init(arch)
    return eb.build_emos_image(reference, init, version)["image"]


def osr_of(image):
    return up.ramdisk_os_release(image)


# ── Versions ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,key", [
    ("emos-v0.9", (0, 9, 0, 0)),
    ("0.9", (0, 9, 0, 0)),
    ("v0.10", (0, 10, 0, 0)),
    ("emos-v1.0.2", (1, 0, 2, 0)),
    ("emos-v0.9-3-gabc1234", (0, 9, 0, 3)),
    ("emos-v0.9-3-gabc1234-dirty", (0, 9, 0, 3)),
    ("", None), (None, None), ("dev", None), ("emos-v", None),
    ("1", None), ("0.9.1.2", None), ("0.x", None),
])
def test_version_key(text, key):
    assert up.version_key(text) == key


def test_ten_is_newer_than_nine():
    """The lexical trap: "0.10" < "0.9" as strings."""
    assert up.update_available("emos-v0.9", "emos-v0.10")
    assert not up.update_available("emos-v0.10", "emos-v0.9")


def test_a_build_past_the_tag_is_not_offered_that_tag():
    assert not up.update_available("emos-v0.10-2-gabc1234", "emos-v0.10")
    assert not up.update_available("emos-v0.10", "emos-v0.10")


def test_an_unknown_running_version_is_never_offered_an_update():
    for running in (None, "", "dev", "garbage"):
        assert not up.update_available(running, "emos-v0.10")


def test_nothing_older_than_the_trial_init_is_a_target():
    """0.9's init ignores the trial mark, so an update to it has no unattended
    rollback and must not be offered whatever the device runs."""
    assert not up.can_target("emos-v0.9")
    assert not up.update_available("emos-v0.5", "emos-v0.9")
    assert up.can_target("emos-v0.10")
    assert up.can_target("emos-v1.0")


@pytest.mark.parametrize("running,latest,want", [
    ("emos-v0.9", "emos-v0.10", "update"),
    ("emos-v0.10", "emos-v0.10", "current"),
    ("emos-v0.10-2-gabc1234", "emos-v0.10", "current"),
    # Newer, but not installable over the network: never "Up to date".
    ("emos-v0.5", "emos-v0.9", "wizard"),
    (None, "emos-v0.10", "unknown"),
    ("emos-v0.9", "", "unknown"),
    ("0.1", "emos-v0.10", "update"),
])
def test_what_the_panel_says(running, latest, want):
    assert up.offer(running, latest) == want
    assert up.update_available(running, latest) == (want == "update")


# ── os-release ───────────────────────────────────────────────────────────────

def test_os_release_parses_what_both_packers_stamp():
    text = ('NAME="emOS"\nID=emos\nPRETTY_NAME="emOS emos-v0.9"\n'
            'VERSION="emos-v0.9"\nVERSION_ID="emos-v0.9"\n'
            'BUILD_ID="0123456789abcdef"\n')
    assert up.running_emos(up.parse_os_release(text)) == (
        "emos-v0.9", "0123456789abcdef")


def test_os_release_quoting_and_comments():
    osr = up.parse_os_release("# c\n\nA=1\nB='two words'\nC=\"x=y\"\nnoequals\n")
    assert osr == {"A": "1", "B": "two words", "C": "x=y"}


def test_another_systems_os_release_is_not_emos():
    assert up.running_emos(up.parse_os_release(
        'ID=android\nVERSION_ID="5.1.1"\n')) == (None, None)
    assert up.running_emos({}) == (None, None)


# ── The image read off the device ────────────────────────────────────────────

def test_image_length_agrees_with_the_packer():
    img = installed()
    assert up.image_length(img[:48]) == len(img)
    assert up.image_length(b"") == 0
    assert up.image_length(b"NOTANDRD" + img[8:48]) == 0


def test_the_stored_id_is_the_computed_one_on_our_images():
    img = installed()
    assert up.stored_id(img) == up.computed_id(img)
    assert re.fullmatch(r"[0-9a-f]{40}", up.stored_id(img))


def test_the_ramdisk_gives_up_its_os_release():
    osr = osr_of(installed("emos-v0.8"))
    assert osr["ID"] == "emos" and osr["VERSION_ID"] == "emos-v0.8"
    assert re.fullmatch(r"[0-9a-f]{16}", osr["BUILD_ID"])


def test_a_stock_ramdisk_has_no_os_release():
    assert up.ramdisk_os_release(fx.make_reference()) == {}


def test_a_good_reference_has_no_problems():
    for arch in ("arm64", "arm"):
        img = installed(arch=arch)
        assert up.reference_problems(img, osr_of(img)) == []


def test_an_unstamped_v1_image_is_still_ours():
    """amonet 1 images carry no emos.system= and are recognised by ramoops."""
    img = installed(system_part=None)
    assert b"emos.system=" not in img[64:576]
    assert up.reference_problems(img, osr_of(img)) == []


def test_a_stock_image_is_refused():
    ref = fx.make_reference()
    assert up.reference_problems(ref, {}) == [
        "the boot partition does not hold an emOS image"]


def test_a_truncated_read_is_refused():
    img = installed()
    assert up.reference_problems(img[:-up.BLOCK], osr_of(img))
    assert up.reference_problems(b"", {})


def test_a_flipped_byte_anywhere_in_kernel_or_ramdisk_is_refused():
    img = installed()
    osr = osr_of(img)
    ksz = int.from_bytes(img[8:12], "little")
    ramdisk_at = up.BLOCK + len(eb.pad(b"\0" * ksz))
    for off in (up.BLOCK + 0x300, ramdisk_at + 5):
        bad = bytearray(img)
        bad[off] ^= 0x01
        problems = up.reference_problems(bytes(bad), osr)
        assert any("id does not match" in p for p in problems), off


def test_an_image_that_is_not_the_one_running_is_refused():
    """The partition holds 0.8 and the device says it is running 0.9: this is
    not the partition the device boots from, so nothing is rebuilt from it."""
    img = installed("emos-v0.8")
    running = osr_of(installed("emos-v0.9"))
    problems = up.reference_problems(img, running)
    assert any("not the one running" in p for p in problems)


def test_same_version_different_build_is_not_the_one_running():
    img = installed("emos-v0.9", init_fill=1)
    other = osr_of(installed("emos-v0.9", init_fill=2))
    assert osr_of(img)["BUILD_ID"] != other["BUILD_ID"]
    assert up.reference_problems(img, other)


def test_a_device_that_reports_no_build_is_refused():
    img = installed()
    assert up.reference_problems(img, {})


# ── The image built from it ──────────────────────────────────────────────────

def test_a_rebuild_passes_every_check_on_both_kernels():
    for arch in ("arm64", "arm"):
        ref = installed(arch=arch)
        new = updated(ref, arch=arch)
        assert up.built_problems(ref, new, "emos-v0.10", 16 << 20) == []


def test_a_rebuild_keeps_the_kernel_cmdline_and_stamp():
    ref = installed(system_part=14)
    new = updated(ref)
    assert new[64:576] == ref[64:576]
    assert b"emos.system=/dev/block/mmcblk0p14" in new[64:576]
    assert new[64:576].count(b"ramoops.mem_address") == 1


def test_a_rebuild_gets_a_new_id():
    """#573's first question: init promotes boot-good.img on an id change, so
    an update that kept the old id would never be promoted."""
    ref = installed()
    new = updated(ref)
    assert up.stored_id(new) != up.stored_id(ref)


def test_a_changed_kernel_is_refused():
    ref = installed()
    new = bytearray(updated(ref))
    new[up.BLOCK + 0x400] ^= 0xFF
    problems = up.built_problems(ref, bytes(new), "emos-v0.10", 16 << 20)
    assert any("kernel differs" in p for p in problems)


def test_a_changed_cmdline_or_address_is_refused():
    ref = installed()
    for off in (12, 20, 32, 70):
        new = bytearray(updated(ref))
        new[off] ^= 0x01
        problems = up.built_problems(ref, bytes(new), "emos-v0.10", 16 << 20)
        assert any("addresses or the command line" in p for p in problems), off


def test_an_image_larger_than_the_partition_is_refused():
    ref = installed()
    new = updated(ref)
    problems = up.built_problems(ref, new, "emos-v0.10", len(new) - 1)
    assert any("boot partition holds" in p for p in problems)


def test_rebuilding_to_the_identical_image_is_refused():
    ref = installed()
    problems = up.built_problems(ref, ref, "emos-v0.9", 16 << 20)
    assert any("already installed" in p for p in problems)


def test_an_image_stamped_with_another_version_is_refused():
    ref = installed()
    new = updated(ref, "emos-v0.11")
    assert up.built_problems(ref, new, "emos-v0.10", 16 << 20)


def test_an_init_without_the_trial_is_recognised_from_the_binary():
    assert up.init_supports_trial(new_init())
    assert not up.init_supports_trial(fx.fake_init())


def test_the_real_init_source_carries_the_string_the_check_looks_for():
    src = (REPO / "emos" / "init" / "init.c").read_text()
    assert f'"{up.TRIAL_MARK}"' in src


# ── The trial mark, and init's half of it ────────────────────────────────────

# The vector emos/init/trialcheck.c uses: id bytes 00..13.
SHARED_ID = bytes(range(20)).hex()


def test_the_mark_is_the_one_trialcheck_reads():
    assert SHARED_ID == "000102030405060708090a0b0c0d0e0f10111213"
    mark = up.trial_mark(SHARED_ID, "0123456789abcdef", "emos-v0.10")
    assert mark.splitlines()[0] == SHARED_ID
    assert mark.endswith("\n")
    src = (REPO / "emos" / "init" / "trialcheck.c").read_text()
    assert f'#define ID_HEX "{SHARED_ID}"' in src
    # The form with our notes after the id is one trialcheck accepts.
    assert 'ID_HEX "\\nbuild=0123456789abcdef\\nversion=0.10\\n"' in src


def test_the_mark_names_the_image_by_the_id_init_reads():
    """init reads 20 bytes at offset 576 of the boot partition."""
    img = updated(installed())
    assert up.stored_id(img) == img[576:596].hex()


def test_init_and_the_controller_agree_on_the_paths():
    src = (REPO / "emos" / "init" / "init.c").read_text()
    assert f'#define BOOTDEV   "{up.BOOT_DEV}"' in src
    assert f'#define GOODIMG   "{up.GOOD_IMG}"' in src
    assert f'#define BOOTSTATE "{up.BOOT_STATE}"' in src
    assert f'#define TRIALMARK "{up.TRIAL_MARK}"' in src
    assert f"#define TRIAL_SECS {up.TRIAL_SECS}" in src
    assert f"#define MAX_TRIES {up.MAX_TRIES}" in src


def test_the_watch_outlasts_a_full_rollback():
    assert up.WATCH_S > up.TRIAL_SECS * up.MAX_TRIES + 120


def test_trialcheck_passes():
    """Runs the C check where a compiler exists (CI runs it regardless)."""
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler")
    out = Path(subprocess.check_output(["mktemp", "-d"]).decode().strip())
    exe = out / "trialcheck"
    subprocess.run([cc, "-O2", "-o", str(exe),
                    str(REPO / "emos" / "init" / "trialcheck.c")],
                   check=True, capture_output=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("bad", [
    "", "00" * 19, "00" * 21, "0" * 39 + "G", ("ab" * 20).upper(),
    "00" * 20 + "\n", " " + "00" * 20, "'; reboot; '".ljust(40, "0"),
])
def test_a_malformed_id_never_reaches_a_mark(bad):
    with pytest.raises(ValueError):
        up.trial_mark(bad, "0123456789abcdef", "emos-v0.10")


@pytest.mark.parametrize("build,version", [
    ("$(reboot)", "emos-v0.10"), ("0123456789abcdef", "0.10; rm -rf /"),
    ("a b", "emos-v0.10"), ("0123", "it's"), ("", "emos-v0.10"),
    ("0123", ""), ("0123", "`id`"), ("0123", "a\nb"),
])
def test_nothing_unsafe_is_written_into_a_command(build, version):
    with pytest.raises(ValueError):
        up.mark_cmd(SHARED_ID, build, version)


def test_the_mark_round_trips():
    mark = up.parse_mark(up.trial_mark(SHARED_ID, "0123456789abcdef", "emos-v0.10"))
    assert mark == {"id": SHARED_ID, "build": "0123456789abcdef",
                    "version": "emos-v0.10"}
    assert up.parse_mark("") == {}
    assert up.parse_mark("not a mark\n") == {}
    assert up.parse_mark(None) == {}


def test_the_rollback_record_is_the_one_trialcheck_writes():
    text = f"from={SHARED_ID}\nto=unknown\ntries=3\n"
    src = (REPO / "emos" / "init" / "trialcheck.c").read_text()
    assert '"from=" ID_HEX "\\n"' in src and '"to=unknown\\n"' in src \
        and '"tries=3\\n"' in src
    assert up.parse_rollback(text) == {
        "from": SHARED_ID, "to": "unknown", "tries": 3}
    init = (REPO / "emos" / "init" / "init.c").read_text()
    assert f'#define ROLLBACKREC "{up.ROLLBACK_REC}"' in init
    assert '"from=%s\\nto=%s\\ntries=%d\\n"' in init


@pytest.mark.parametrize("text", [
    "", None, "from=abc\nto=unknown\ntries=3\n",
    f"from={SHARED_ID}\nto={SHARED_ID}\n",
    f"from={SHARED_ID}\nto={SHARED_ID}\ntries=three\n",
    f"from={SHARED_ID}; reboot\nto={SHARED_ID}\ntries=3\n",
])
def test_a_damaged_rollback_record_is_no_record(text):
    assert up.parse_rollback(text) == {}


def test_what_a_rollback_record_says():
    other = "ab" * 20
    assert "restored its previous image" in up.rollback_text(
        {"from": SHARED_ID, "to": other, "tries": 3}, "emos-v0.10")
    # The same image written back: no update was involved.
    assert "rewrote its known-good image" in up.rollback_text(
        {"from": other, "to": other, "tries": 3}, "emos-v0.10")


def test_mark_verdicts():
    mark = up.parse_mark(up.trial_mark(SHARED_ID, "newbuild", "emos-v0.10"))
    assert up.mark_verdict({}, "anything") == "none"
    assert up.mark_verdict(mark, "newbuild") == "confirm"
    assert up.mark_verdict(mark, "oldbuild") == "rolled_back"
    assert up.mark_verdict(mark, None) == "rolled_back"
    # A mark with no build line cannot confirm anything.
    assert up.mark_verdict({"id": SHARED_ID}, None) == "rolled_back"


# ── The device's answers ─────────────────────────────────────────────────────

def _b64(data):
    return base64.encodebytes(data).decode()


def preflight_reply(img, good=None, state="0", sectors="32768", mark="",
                    md5line=None, b64line="eAo=", dd=True, ok=True):
    good = img if good is None else good
    md5line = md5line if md5line is not None else (
        hashlib.md5(b"x\n").hexdigest() + "  -")
    osr = "\n".join(f'{k}="{v}"' for k, v in osr_of(img).items())
    return (
        f"OSR_BEGIN\n{osr}\nOSR_END\nSTATE:{state}\n"
        f"HDR_BEGIN\n{_b64(img[:up.BLOCK])}HDR_END\n"
        f"GOOD_BEGIN\n{_b64(good[:up.BLOCK])}GOOD_END\n"
        f"SECTORS:{sectors}\nMD5TOOL:{md5line}\nB64TOOL:{b64line}\n"
        + ("DDTOOL:ok\n" if dd else "") +
        f"FREE /dev/block/mmcblk0p16  11000  900  10100   8% /data\n"
        f"MARK_BEGIN\n{mark}MARK_END\n" + (up.OK + "\n" if ok else ""))


def test_a_healthy_device_passes_preflight():
    img = installed()
    pre = up.parse_preflight(preflight_reply(img))
    assert up.preflight_problems(pre) == []
    assert pre["partition_bytes"] == 16 << 20
    assert up.image_length(pre["header"]) == len(img)
    assert up.good_matches(pre)


def test_crlf_from_the_device_changes_nothing():
    img = installed()
    pre = up.parse_preflight(preflight_reply(img).replace("\n", "\r\n"))
    assert up.preflight_problems(pre) == []


def test_a_reply_that_did_not_finish_is_not_a_pass():
    img = installed()
    assert up.parse_preflight(preflight_reply(img, ok=False)) == {}
    assert up.parse_preflight("") == {}
    assert up.parse_preflight(None) == {}
    assert up.preflight_problems({}) == [
        "the device did not answer the preflight check"]


def test_an_unconfirmed_boot_is_not_updated():
    img = installed()
    for state in ("1", "3", ""):
        pre = up.parse_preflight(preflight_reply(img, state=state))
        assert any("not confirmed" in p for p in up.preflight_problems(pre)), state


def test_a_missing_md5_tool_refuses():
    img = installed()
    for line in ("", "d41d8cd98f00b204e9800998ecf8427e  -"):
        pre = up.parse_preflight(preflight_reply(img, md5line=line))
        assert any("md5sum" in p for p in up.preflight_problems(pre))


def test_a_dd_that_cannot_write_safely_refuses():
    pre = up.parse_preflight(preflight_reply(installed(), dd=False))
    assert any("busybox dd" in p for p in up.preflight_problems(pre))


def test_a_missing_base64_names_itself():
    """Not "could not be read as a boot image", which is what an empty header
    looks like and points at the partition instead of the tool."""
    img = installed()
    reply = preflight_reply(img, b64line="").replace(_b64(img[:up.BLOCK]), "")
    problems = up.preflight_problems(up.parse_preflight(reply))
    assert any("base64 does not run" in p for p in problems)
    assert not any("boot image" in p for p in problems)


def test_a_fireos_device_is_refused_at_preflight():
    out = ("OSR_BEGIN\nOSR_END\nSTATE:\nHDR_BEGIN\nHDR_END\nGOOD_BEGIN\n"
           "GOOD_END\nSECTORS:\nMD5TOOL:\nFREE \nMARK_BEGIN\nMARK_END\n" + up.OK)
    assert up.preflight_problems(up.parse_preflight(out)) == [
        "this device is not running emOS"]


def test_an_unreadable_partition_refuses():
    img = installed()
    reply = preflight_reply(img).replace(_b64(img[:up.BLOCK]), "", 1)
    assert any("could not be read" in p
               for p in up.preflight_problems(up.parse_preflight(reply)))
    pre = up.parse_preflight(preflight_reply(img, sectors=""))
    assert any("size could not be read" in p for p in up.preflight_problems(pre))


def test_a_full_data_partition_refuses_and_an_unreadable_df_does_not():
    six_mb = 6 << 20
    assert "not enough space" in up.space_problem(
        "/dev/block/mmcblk0p16  11000  10990  10  99% /data", six_mb)
    assert up.space_problem(
        "/dev/block/mmcblk0p16  11000  900  10100   8% /data", six_mb) == ""
    assert up.space_problem("", six_mb) == ""


def test_a_stale_or_missing_known_good_is_noticed():
    img = installed()
    assert not up.good_matches(up.parse_preflight(
        preflight_reply(img, good=installed("emos-v0.8"))))
    assert not up.good_matches(up.parse_preflight(preflight_reply(img, good=b"")))


def test_chunks_cover_the_image_exactly_once():
    for blocks in (1, up.CHUNK_BLOCKS - 1, up.CHUNK_BLOCKS, up.CHUNK_BLOCKS + 1,
                   3224, 8192):
        cs = up.chunks(blocks * up.BLOCK)
        assert sum(c for _, c in cs) == blocks
        assert [s for s, _ in cs] == [i * up.CHUNK_BLOCKS for i in range(len(cs))]
        assert all(0 < c <= up.CHUNK_BLOCKS for _, c in cs)


def chunk_reply(data, md5=None, ok=True):
    md5 = md5 or hashlib.md5(data).hexdigest()
    return (f"CHUNK_BEGIN\n{_b64(data)}CHUNK_END\nCHUNKMD5:{md5}  -\n"
            + (up.OK if ok else ""))


def test_a_chunk_arrives_only_whole_and_verified():
    data = bytes(range(256)) * 16          # two blocks
    assert up.parse_chunk(chunk_reply(data), 2) == data
    assert up.parse_chunk(chunk_reply(data).replace("\n", "\r\n"), 2) == data
    assert up.parse_chunk(chunk_reply(data, ok=False), 2) is None
    assert up.parse_chunk(chunk_reply(data, md5="0" * 32), 2) is None
    assert up.parse_chunk(chunk_reply(data), 3) is None            # short
    assert up.parse_chunk(chunk_reply(data[:-1] + b"\0"), 2) == data[:-1] + b"\0"
    corrupt = chunk_reply(data).replace("AAEC", "AAED", 1)
    assert up.parse_chunk(corrupt, 2) is None
    assert up.parse_chunk("", 2) is None


def test_write_verdicts():
    want = "a" * 32
    done = f"DD:0\nREADBACK:{want}  -\n{up.OK}\n"
    assert up.write_verdict(done, want) == "ok"
    assert up.write_verdict(done, "b" * 32) == "mismatch"
    assert up.write_verdict(f"DD:1\nREADBACK:\n{up.OK}", want) == "mismatch"
    # Cut off before the end: nothing is known, which is not the same as bad.
    assert up.write_verdict(f"DD:0\nREADBACK:{want}  -\n", want) == "unknown"
    assert up.write_verdict("", want) == "unknown"


def test_only_our_two_images_can_be_written_to_the_boot_partition():
    assert up.BOOT_DEV in up.write_cmd(up.NEW_IMG, 4096)
    assert up.BOOT_DEV in up.write_cmd(up.GOOD_IMG, 4096)
    for source in ("/data/local/bin/server", "/dev/zero", "x; reboot"):
        with pytest.raises(ValueError):
            up.write_cmd(source, 4096)


def test_the_write_reads_back_after_dropping_the_cache():
    cmd = up.write_cmd(up.NEW_IMG, 3224 * up.BLOCK)
    assert cmd.index("conv=notrunc,fsync") < cmd.index("drop_caches") < cmd.index("READBACK")
    assert "count=3224" in cmd


def test_status_reply():
    img = installed("emos-v0.10")
    osr = osr_of(img)
    text = "\n".join(f'{k}="{v}"' for k, v in osr.items())
    mark = up.trial_mark(up.stored_id(img), osr["BUILD_ID"], "emos-v0.10")
    st = up.parse_status(
        f"OSR_BEGIN\n{text}\nOSR_END\nMARK_BEGIN\n{mark}MARK_END\nSTATE:1\n{up.OK}")
    assert st["version"] == "emos-v0.10" and st["state"] == "1"
    assert up.mark_verdict(st["mark"], st["build"]) == "confirm"
    assert st["uptime"] is None
    assert up.parse_status("OSR_BEGIN\nOSR_END\n") == {}


def test_a_redial_is_told_from_a_restart_by_uptime():
    st = up.parse_status(f"OSR_BEGIN\nOSR_END\nUPTIME:4321.50 4000.1\n{up.OK}")
    assert st["uptime"] == 4321.5
    # Asked 40s ago and the kernel has been up over an hour: it never restarted.
    assert up.booted_before(st["uptime"], 40)
    # Up 35s, asked 60s ago: this is a fresh boot.
    assert not up.booted_before(35.0, 60)
    # Unreadable uptime never claims the device stayed up.
    assert not up.booted_before(None, 60)


def test_every_command_ends_with_the_sentinel():
    cmds = [up.preflight_cmd(), up.read_chunk_cmd(0, 4), up.refresh_good_cmd(4096),
            up.digests_cmd(4096), up.verify_cmd(4096), up.mark_cmd(SHARED_ID, "b", "0.10"),
            up.write_cmd(up.NEW_IMG, 4096), up.CLEANUP_CMD, up.CONFIRM_CMD,
            up.status_cmd(), up.REBOOT_CMD]
    for c in cmds:
        assert c.rstrip().endswith(f"echo {up.OK}"), c
