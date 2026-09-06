"""
The emOS image packer, and the thing it must agree with.

`controller/em_emos_build.py` and `emos/mkboot.py` implement the same boot
image format twice, because the controller image is built with
`context: controller` and cannot reach `emos/`. Two implementations of one
format can only be known to agree by running both, which is what
`test_agrees_with_mkboot` does — the same instrument `emos/init/pwcheck.c`
provides for the password hash.

The fixture is synthetic. A real reference is a device's own boot partition,
which is exactly the file we never ship, so the shape is reproduced here
instead: an ANDROID! header, an MTK-wrapped kernel of zImage + appended DTBs,
and a raw ramdisk.
"""

import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

import em_emos_build as eb


REPO = Path(__file__).resolve().parents[2]


def _load_mkboot():
    """Import emos/mkboot.py by path — it is outside the controller package."""
    path = REPO / "emos" / "mkboot.py"
    if not path.exists():
        pytest.skip(f"emos/mkboot.py not present at {path}")
    spec = importlib.util.spec_from_file_location("_mkboot", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mkboot"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── The fixture ──────────────────────────────────────────────────────────────

DTB_MAGIC = b"\xd0\x0d\xfe\xed"
KADDR, RADDR, SADDR, TAGS, HDRV, OSV = 0x40080000, 0x44000000, 0, 0x40000100, 0, 0
CMDLINE = b"bootopt=64S3,32N2,64N2 androidboot.selinux=enforce"


def make_reference(zimage=b"ZIMAGE" * 400, dtbs=None, ramdisk=b"RAMDISK" * 300,
                   mtk_pad=b"\x00"):
    """A boot image shaped like biscuit's, assembled the way the device's is.

    `mtk_pad` is the byte after the kernel header's name field. Amazon's own
    images use 0xff and magiskboot-repacked ones use 0x00, and both are real —
    see test_either_mtk_header_padding_round_trips.
    """
    if dtbs is None:
        dtbs = DTB_MAGIC + b"\x11" * 512
    payload = zimage + dtbs
    khdr = struct.pack("<II", eb.MTK_MAGIC, len(payload))
    khdr += b"KERNEL".ljust(32, b"\0")
    kernel = khdr.ljust(0x200, mtk_pad) + payload
    hdr = b"ANDROID!"
    hdr += struct.pack("<10I", len(kernel), KADDR, len(ramdisk), RADDR,
                       0, SADDR, TAGS, eb.PAGE, HDRV, OSV)
    hdr += b"\0" * 16
    hdr += CMDLINE.ljust(512, b"\0")
    digest = hashlib.sha1()
    for region in (kernel, ramdisk, b""):
        digest.update(region)
        digest.update(struct.pack("<I", len(region)))
    hdr += digest.digest().ljust(32, b"\0")
    hdr += b"\0" * 1024
    return eb.pad(hdr) + eb.pad(kernel) + eb.pad(ramdisk)


def fake_init(machine=183, elf_class=2, e_type=2, size=4096):
    """An ELF header good enough for the checks, over filler."""
    b = bytearray(b"\x7fELF")
    b += bytes([elf_class, 1, 1]) + b"\0" * 9
    b += struct.pack("<HH", e_type, machine)
    b += b"\0" * (size - len(b))
    return bytes(b)


# ── The cross-implementation contract ────────────────────────────────────────

def test_agrees_with_mkboot(tmp_path):
    """
    The controller's packer and emos/mkboot.py must produce the same bytes.

    If they drift, the wizard builds an image that the standalone tool would
    not have built, and nothing else in either tree notices — the device is
    what finds out, after a partition write.
    """
    mkboot = _load_mkboot()
    ref = make_reference()
    parts = eb.split_reference(ref)
    ramdisk = eb.build_ramdisk(fake_init(), "0.1-test")

    mine = eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk)

    # mkboot's main() is a CLI over files; drive it the way build.sh does.
    ref_p = tmp_path / "ref.img"
    z_p = tmp_path / "zimage"
    rd_p = tmp_path / "ramdisk.gz"
    out_p = tmp_path / "out.img"
    ref_p.write_bytes(ref)
    z_p.write_bytes(parts["zimage"])
    rd_p.write_bytes(ramdisk)
    argv = sys.argv
    sys.argv = ["mkboot.py", str(ref_p), str(z_p), str(rd_p), str(out_p)]
    try:
        mkboot.main()
    finally:
        sys.argv = argv

    assert out_p.read_bytes() == mine, (
        "controller/em_emos_build.py and emos/mkboot.py disagree — see the "
        "module docstring in em_emos_build.py")


def test_the_ramoops_cmdline_is_the_same_string():
    """It names a physical address the vendor device tree reserves; a copy that
    drifts points the crash log at memory something else owns."""
    assert eb.RAMOOPS_CMDLINE == _load_mkboot().RAMOOPS_CMDLINE


# ── The gate that runs before any flash ──────────────────────────────────────

def test_a_reference_round_trips_byte_for_byte():
    assert eb.roundtrip_identical(make_reference())


def test_round_trip_fails_when_the_image_is_not_what_it_says():
    """
    The gate has to be capable of saying no, or it is decoration.

    A header field that does not describe the body is the case that matters:
    the packer would reassemble it wrongly, and the refusal is what stops that
    reaching a partition.
    """
    bad = bytearray(make_reference())
    struct.pack_into("<I", bad, 8, struct.unpack_from("<I", bad, 8)[0] + 16)
    assert not eb.roundtrip_identical(bytes(bad))


def test_either_mtk_header_padding_round_trips():
    """
    The byte after the MTK kernel header's name field is NOT constant across
    images. Amazon's own images pad it with 0xff; anything repacked by
    magiskboot pads it with 0x00. A packer that picks one is right on half the
    fleet and puts 472 differing bytes inside the header LK validates on the
    other half — and this code has now been wrong in BOTH directions.

    The fix is to reuse the reference's own header rather than synthesise one,
    which is safe because an emOS build carries the kernel over untouched.
    """
    for padbyte in (b"\x00", b"\xff"):
        ref = make_reference(mtk_pad=padbyte)
        assert eb.roundtrip_diff(ref, ignore_id=True) is None, padbyte.hex()
        parts = eb.split_reference(ref)
        built = eb.pack(parts, parts["zimage"], parts["dtbs"], parts["ramdisk"],
                        extra_cmdline="")
        assert built[eb.PAGE + 40:eb.PAGE + 41] == padbyte, \
            "the reference's own padding must survive the repack"


def test_a_kernel_header_that_lies_about_its_payload_is_refused():
    """Reusing the header is only safe while it still describes the payload."""
    ref = make_reference()
    parts = eb.split_reference(ref)
    bad = bytearray(parts["mtkhdr"])
    struct.pack_into("<I", bad, 4, 12345)
    parts["mtkhdr"] = bytes(bad)
    with pytest.raises(eb.BuildError, match="does not describe its own payload"):
        eb.pack(parts, parts["zimage"], parts["dtbs"], parts["ramdisk"])


def test_a_stale_image_id_does_not_block_a_build():
    """
    Some images legitimately carry an id that does not describe their contents
    — a tool that repacks a ramdisk while preserving the header verbatim
    leaves one, and f1r30s does exactly that. Reproducing it is impossible by
    definition, and requiring it refused stock FireOS 5 + f1r30s, which is the
    state docs/rooting.md tells users to be in (measured 2026-09-06).

    The id is a checksum over regions this build replaces outright, and pack()
    computes a fresh one for what it emits, so the reference's is not an input
    to anything.
    """
    bad = bytearray(make_reference())
    for i in range(eb._ID_START, eb._ID_START + 20):
        bad[i] ^= 0xFF
    assert not eb.roundtrip_identical(bytes(bad)), "the strict check still sees it"
    assert eb.roundtrip_diff(bytes(bad), ignore_id=True) is None


def test_relaxing_the_id_does_not_relax_anything_else():
    """
    Everything the packer SYNTHESISES must still reproduce. The kernel and the
    ramdisk are copied verbatim so they cannot differ, but the MTK kernel
    header, the product name, the extra command line and every pad byte are
    rebuilt, and a disagreement in any of them means the image was misread.
    """
    for offset, what in ((50, "product name"), (700, "extra cmdline")):
        bad = bytearray(make_reference())
        bad[offset] ^= 0xFF
        assert eb.roundtrip_diff(bytes(bad), ignore_id=True) is not None, what


def test_the_round_trip_diff_names_the_field():
    """A bare False is not actionable — finding out why a real image was
    refused cost a hex dump over adb and a field-by-field decode."""
    bad = bytearray(make_reference())
    bad[eb._ID_START] ^= 0xFF
    off, where, got, made = eb.roundtrip_diff(bytes(bad))
    assert off == eb._ID_START
    assert "image id" in where
    assert got != made


def test_build_refuses_an_image_it_cannot_reproduce():
    bad = bytearray(make_reference())
    struct.pack_into("<I", bad, 8, struct.unpack_from("<I", bad, 8)[0] + 16)
    with pytest.raises(eb.BuildError, match="byte for byte"):
        eb.build_emos_image(bytes(bad), fake_init(), "0.1")


# ── Refusing the wrong reference, with something a person can act on ─────────

def test_a_file_that_is_not_a_boot_image_is_refused():
    with pytest.raises(eb.BuildError, match="mmcblk0p10"):
        eb.split_reference(b"not a boot image" * 200)


def test_a_truncated_file_is_refused():
    with pytest.raises(eb.BuildError):
        eb.split_reference(make_reference()[:100])


def test_a_kernel_that_is_not_mtk_wrapped_is_refused():
    ref = bytearray(make_reference())
    struct.pack_into("<I", ref, eb.PAGE, 0xDEADBEEF)
    with pytest.raises(eb.BuildError, match="MTK-wrapped"):
        eb.split_reference(bytes(ref))


def test_a_kernel_with_no_dtb_is_refused():
    ref = make_reference(dtbs=b"\x22" * 512)
    with pytest.raises(eb.BuildError, match="no DTB"):
        eb.split_reference(ref)


# ── The init binary ──────────────────────────────────────────────────────────

def test_a_good_init_has_no_problems():
    assert eb.init_binary_problems(fake_init()) == []


def test_an_arm32_init_is_refused():
    """
    The mistake that cost five flashed images: biscuit boots ARM64, and an
    ARM32 kernel never executed an instruction. The same error in an init is
    just as silent.
    """
    problems = eb.init_binary_problems(fake_init(machine=40))   # EM_ARM
    assert any("AArch64" in p for p in problems)


def test_a_dynamically_linked_init_is_refused():
    problems = eb.init_binary_problems(fake_init(e_type=3))     # ET_DYN
    assert any("statically linked" in p for p in problems)


def test_something_that_is_not_an_elf_is_refused():
    assert eb.init_binary_problems(b"#!/bin/sh\necho hi\n") == [
        "the init binary is not an ELF executable"]


def test_build_refuses_a_bad_init_before_touching_the_reference():
    with pytest.raises(eb.BuildError, match="AArch64"):
        eb.build_emos_image(make_reference(), fake_init(machine=40), "0.1")


# ── The ramdisk ──────────────────────────────────────────────────────────────

def test_the_ramdisk_carries_init_and_the_mountpoints():
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1"))
    for name in (b"init", b"etc/os-release", b"dev", b"proc", b"sys",
                 b"system", b"data", b"etc"):
        assert name in raw, f"{name!r} missing from the ramdisk"
    assert raw.startswith(b"070701"), "not a newc cpio archive"
    assert b"TRAILER!!!" in raw


def test_the_ramdisk_contains_the_init_we_gave_it():
    import gzip
    init = fake_init(size=8192)
    raw = gzip.decompress(eb.build_ramdisk(init, "0.1"))
    assert init in raw


def test_os_release_carries_the_version():
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1-abcdef"))
    assert b'PRETTY_NAME="emOS 0.1-abcdef"' in raw
    assert b'VERSION_ID="0.1-abcdef"' in raw


def test_the_build_is_reproducible():
    """
    Same inputs, same bytes — no clock anywhere in the archive or the gzip
    header. An image that differs run to run cannot be compared against the one
    on the device, and comparing is how the flash step knows the write landed.
    """
    a = eb.build_ramdisk(fake_init(), "0.1")
    b = eb.build_ramdisk(fake_init(), "0.1")
    assert a == b

    ref = make_reference()
    x = eb.build_emos_image(ref, fake_init(), "0.1")
    y = eb.build_emos_image(ref, fake_init(), "0.1")
    assert x["image"] == y["image"]
    assert x["md5"] == y["md5"]


def test_an_empty_init_is_refused():
    with pytest.raises(eb.BuildError, match="no init binary"):
        eb.build_ramdisk(b"", "0.1")


# ── The built image ──────────────────────────────────────────────────────────

def test_the_built_image_is_a_boot_image_the_packer_understands():
    built = eb.build_emos_image(make_reference(), fake_init(), "0.1")["image"]
    parts = eb.split_reference(built)
    assert parts["kaddr"] == KADDR
    assert built[:8] == b"ANDROID!"
    assert len(built) % eb.PAGE == 0


def test_the_kernel_and_dtbs_are_carried_over_verbatim():
    """The user's own kernel goes back in unchanged — that is what makes the
    artifact reconstructable from software already on their device."""
    ref = make_reference()
    before = eb.split_reference(ref)
    built = eb.build_emos_image(ref, fake_init(), "0.1")["image"]
    after = eb.split_reference(built)
    assert after["zimage"] == before["zimage"]
    assert after["dtbs"] == before["dtbs"]


def test_the_ramoops_parameters_are_appended_to_the_devices_own_cmdline():
    info = eb.build_emos_image(make_reference(), fake_init(), "0.1")
    assert info["cmdline"].startswith(CMDLINE.decode())
    assert "ramoops.mem_address=0x44400000" in info["cmdline"]


def test_a_cmdline_that_will_not_fit_is_refused():
    """The ramoops parameters are APPENDED, so a device already close to the
    512-byte field overflows it — refuse rather than silently truncate the
    boot arguments."""
    # Same length in, or the slice assignment shortens the image and the
    # failure arrives from somewhere else entirely.
    ref = bytearray(make_reference())
    ref[64:64 + 512] = b"x" * 500 + b"\0" * 12
    assert len(ref) == len(make_reference())
    with pytest.raises(eb.BuildError, match="too long"):
        eb.build_emos_image(bytes(ref), fake_init(), "0.1")


def test_the_report_names_what_went_in():
    ref = make_reference()
    info = eb.build_emos_image(ref, fake_init(), "0.1")
    assert info["reference_md5"] == hashlib.md5(ref).hexdigest()
    assert info["md5"] == hashlib.md5(info["image"]).hexdigest()
    assert info["size"] == len(info["image"]) == info["size"]
    assert info["zimage_size"] > 0 and info["dtb_size"] > 0
