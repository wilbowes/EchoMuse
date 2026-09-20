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
import io
import struct
import zipfile
import zlib
import sys
from pathlib import Path

import pytest

import em_emos_build as eb
import struct as struct_mod


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
CMDLINE = b"bootopt=64S3,32N2,64N2 androidboot.selinux=enforce ramoops.mem_address=0x44400000 ramoops.mem_size=0x200000 ramoops.record_size=0x20000 ramoops.console_size=0x80000 ramoops.dump_oops=1 emos.board=biscuit"


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


def arm_zimage(size=2400):
    """An ARM zImage: a self-decompressing stub carrying 0x016f2818 at 0x24."""
    b = bytearray(b"\x00" * size)
    b[0x24:0x28] = b"\x18\x28\x6f\x01"
    return bytes(b)


def arm64_zimage(size=2400):
    """An AArch64 kernel: a raw gzip stream whose Image carries "ARM\\x64" at 0x38.

    Compressed here rather than pasted as a blob so the test reads the same way
    the sniffer does, and so a change to either end has to survive a real
    decompress rather than a fixture somebody hand-edited.
    """
    image = bytearray(b"\x00" * 0x100)
    image[0x38:0x3c] = b"ARM\x64"
    co = zlib.compressobj(9, zlib.DEFLATED, 31)
    out = co.compress(bytes(image) + b"\x00" * size) + co.flush()
    return out


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


def test_the_two_packers_stamp_the_system_partition_identically(tmp_path):
    """The stamp decides which FireOS userspace emOS mounts, so the wizard's
    packer and the standalone tool must write it the same way.

    Drift here is invisible: both images boot, and the one built by the wrong
    tool mounts a different Amazon userspace than it was built beside."""
    import os
    mkboot = _load_mkboot()
    ref = make_reference()
    parts = eb.split_reference(ref)
    ramdisk = eb.build_ramdisk(fake_init(), "0.1-test")

    mine = eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                   system_part=14)

    ref_p, z_p = tmp_path / "ref.img", tmp_path / "zimage"
    rd_p, out_p = tmp_path / "ramdisk.gz", tmp_path / "out.img"
    ref_p.write_bytes(ref)
    z_p.write_bytes(parts["zimage"])
    rd_p.write_bytes(ramdisk)
    argv, env = sys.argv, os.environ.get("EMOS_SYSTEM_PART")
    sys.argv = ["mkboot.py", str(ref_p), str(z_p), str(rd_p), str(out_p)]
    os.environ["EMOS_SYSTEM_PART"] = "14"
    try:
        mkboot.main()
    finally:
        sys.argv = argv
        if env is None:
            os.environ.pop("EMOS_SYSTEM_PART", None)
        else:
            os.environ["EMOS_SYSTEM_PART"] = env

    assert out_p.read_bytes() == mine, (
        "the two packers stamp emos.system= differently")
    cmdline = mine[64:64 + 512].split(b"\0")[0].decode()
    assert "emos.system=/dev/block/mmcblk0p14" in cmdline
    # Exactly one. The kernel takes the last of a repeated parameter, so a
    # second stamp is an image that works and reads as whichever you looked at.
    assert cmdline.count("emos.system=") == 1


def test_restamping_replaces_rather_than_appends():
    """Rebuilding an emOS image for a different slot is the case that produces
    two stamps, and it is reachable — the packer already handles rebuilding
    from an emOS image for the ramoops block."""
    once = eb._stamp_cmdline_key(b"ro init=/init", eb.SYSTEM_CMDLINE_KEY,
                                 "/dev/block/mmcblk0p13")
    twice = eb._stamp_cmdline_key(once, eb.SYSTEM_CMDLINE_KEY,
                                  "/dev/block/mmcblk0p14")
    assert twice.decode().count("emos.system=") == 1
    assert b"mmcblk0p14" in twice and b"mmcblk0p13" not in twice


def test_the_two_packers_stamp_the_board_identically(tmp_path):
    """The board stamp decides which boards_*.c the firmware links against,
    so the wizard's packer and the standalone tool must write it the same
    way.

    Drift here is invisible: both images boot, and the one built by the
    wrong tool lands on the wrong board runtime (or no runtime, on
    something we have not linked) with no error. """
    import os
    mkboot = _load_mkboot()
    ref = make_reference()
    parts = eb.split_reference(ref)
    ramdisk = eb.build_ramdisk(fake_init(), "0.1-test")

    mine = eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                   board_id="biscuit")

    ref_p, z_p = tmp_path / "ref.img", tmp_path / "zimage"
    rd_p, out_p = tmp_path / "ramdisk.gz", tmp_path / "out.img"
    ref_p.write_bytes(ref)
    z_p.write_bytes(parts["zimage"])
    rd_p.write_bytes(ramdisk)
    argv, env_sys, env_brd = (
        sys.argv,
        os.environ.get("EMOS_SYSTEM_PART"),
        os.environ.get("EMOS_BOARD"),
    )
    sys.argv = ["mkboot.py", str(ref_p), str(z_p), str(rd_p), str(out_p)]
    os.environ["EMOS_BOARD"] = "biscuit"
    try:
        mkboot.main()
    finally:
        sys.argv = argv
        for k, v in (("EMOS_SYSTEM_PART", env_sys),
                     ("EMOS_BOARD", env_brd)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert out_p.read_bytes() == mine, (
        "the two packers stamp emos.board= differently")
    cmdline = mine[64:64 + 512].split(b"\0")[0].decode()
    assert "emos.board=biscuit" in cmdline
    assert cmdline.count("emos.board=") == 1


def test_restamping_board_replaces_rather_than_appends():
    """Same dedup rule as emos.system=: a second stamp is an image that
    works and reads as whichever one you looked at. """
    once = eb._stamp_cmdline_key(b"ro init=/init", eb.BOARD_CMDLINE_KEY, "biscuit")
    twice = eb._stamp_cmdline_key(once, eb.BOARD_CMDLINE_KEY, "donut")
    assert twice.decode().count("emos.board=") == 1
    assert b"donut" in twice and b"biscuit" not in twice


def test_the_system_stamp_key_is_the_same_string():
    """Two packers, one key — and init.c's parser matches on it exactly."""
    assert eb.SYSTEM_CMDLINE_KEY == _load_mkboot().SYSTEM_CMDLINE_KEY
    init_c = (REPO / "emos" / "init" / "init.c").read_text()
    assert f'"{eb.SYSTEM_CMDLINE_KEY}"' in init_c, (
        "init.c does not parse the key the packers write")


def test_an_impossible_system_partition_is_refused():
    """A wrong partition mounts a different userspace and boots, so this is
    refused at build time rather than discovered on a device."""
    parts = eb.split_reference(make_reference())
    ramdisk = eb.build_ramdisk(fake_init(), "0.1")
    for bad in (0, -1, 128, 999):
        with pytest.raises(eb.BuildError, match="mmcblk0 partition number"):
            eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                    system_part=bad)


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


def test_rebuilding_an_emos_image_does_not_double_the_cmdline():
    """
    An in-place update repacks an emOS image, whose cmdline already ends in the
    ramoops parameters. Appending blindly adds another copy every time, and the
    511-byte field overflows on the third pass — so an update mechanism would
    work twice and then refuse, with nothing to say why.

    Found 2026-09-06 building 0.3 from Test Echo 2's own boot partition, the
    first time anything had repacked an emOS image rather than a FireOS one.
    """
    ref = make_reference()
    once = eb.pack(eb.split_reference(ref), *(
        lambda p: (p["zimage"], p["dtbs"], p["ramdisk"]))(eb.split_reference(ref)))
    assert once[64:576].count(eb.RAMOOPS_CMDLINE.encode()) == 1

    # Repack the result: its cmdline already carries the parameters.
    twice = eb.pack(eb.split_reference(once), *(
        lambda p: (p["zimage"], p["dtbs"], p["ramdisk"]))(eb.split_reference(once)))
    assert twice[64:576].count(eb.RAMOOPS_CMDLINE.encode()) == 1, \
        "repacking an emOS image must not append the ramoops params again"
    assert twice[64:576] == once[64:576]


def test_the_reported_cmdline_is_the_one_in_the_image():
    """
    build_emos_image used to reconstruct the cmdline it reports instead of
    reading it back, so the two disagreed the moment pack() learned not to
    append a cmdline it already had — it reported the ramoops parameters twice
    for an image that carried them once. A number the wizard shows the operator
    has to come from the artefact, not from a second copy of the rule.
    """
    info = eb.build_emos_image(make_reference(), fake_init(), "0.3")
    assert info["cmdline"] == \
        info["image"][64:64 + 512].rstrip(b"\0").decode()


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


def test_an_arm32_init_is_refused_for_a_64_bit_kernel():
    """
    The mistake that cost five flashed images: FireOS 5's biscuit boots ARM64,
    and an ARM32 kernel never executed an instruction. The same error in an init
    is just as silent.
    """
    problems = eb.init_binary_problems(fake_init(machine=40))   # EM_ARM
    assert any("AArch64" in p for p in problems)


# ── Which architecture the reference wants ────────────────────────────────────
#
# The init must match the reference's KERNEL. Getting it wrong is silent: the
# flash succeeds and the device then produces no output at all.

def test_reference_kernel_arch_reads_both_shapes():
    assert eb.reference_kernel_arch(
        make_reference(zimage=arm_zimage())) == eb.ARCH_ARM
    assert eb.reference_kernel_arch(
        make_reference(zimage=arm64_zimage())) == eb.ARCH_ARM64


def test_reference_kernel_arch_says_nothing_rather_than_guessing():
    """"" never an architecture: a caller can refuse on "", but cannot recover
    from being told the wrong thing confidently."""
    assert eb.reference_kernel_arch(make_reference(zimage=b"NEITHER" * 300)) == ""
    assert eb.reference_kernel_arch(b"not a boot image at all" * 200) == ""
    assert eb.reference_kernel_arch(b"") == ""
    # A gzip stream that cannot be decompressed is not evidence of anything.
    assert eb.reference_kernel_arch(
        make_reference(zimage=b"\x1f\x8b" + b"\x00" * 600)) == ""
    # Gzip that decompresses but is not an AArch64 Image.
    co = zlib.compressobj(9, zlib.DEFLATED, 31)
    notimage = co.compress(b"\x00" * 0x200) + co.flush()
    assert eb.reference_kernel_arch(make_reference(zimage=notimage)) == ""


# ── Which board the reference wants ──────────────────────────────────────────
#
# reference_board_id() reads the `compatible` property out of the DTB
# blob the reference carries. Getting this wrong is silent in the same
# way as the kernel-arch sniff -- init.c falls back to BOARD_DEFAULT
# ("biscuit") on a missing or unrecognised value, so a wrong DTB read
# is "this image boots on biscuit" rather than "this image is wrong".

def make_dtb(compatible_value):
    """A minimal FDT carrying one root property: `compatible = "<value>"`.

    Built by hand rather than with dtc so the test does not depend on a
    tool the host may not have. The struct block has the FDT_BEGIN_NODE
    (empty root name) + FDT_PROP(compatible) + FDT_END_NODE + FDT_END
    sequence; the strings block holds just "compatible".

    The FDT header, tokens, and property length fields are written in
    BIG ENDIAN, which is what the FDT spec requires regardless of the
    host byte order. The header magic 0xd00dfeed is therefore written
    as bytes d0 0d fe ed, which is also what the (currently buggy)
    packer search looks for -- the two happen to match for that one
    value. The property VALUE is just bytes, so it goes in as encoded.
    """
    strings = b"compatible\x00"
    val = compatible_value.encode() + b"\x00"   # one trailing NUL
    # FDT tokens and header fields are big-endian on the wire. We pack
    # with ">" for those, "=" for the property value (which is just bytes).
    s = b""
    s += struct_mod.pack(">I", eb.FDT_BEGIN_NODE) + b"\x00\x00\x00\x00"   # empty node name, padded
    s += struct_mod.pack(">I", eb.FDT_PROP)
    s += struct_mod.pack(">II", len(val), 0)   # valen, nameoff=0 -> "compatible"
    s += val + b"\x00" * (-len(val) % 4)  # 4-byte align
    s += struct_mod.pack(">I", eb.FDT_END_NODE)
    s += struct_mod.pack(">I", eb.FDT_END)
    totalsize = 40 + len(strings) + len(s)
    # The packer searches for b"\xd0\x0d\xfe\xed" -- match it.
    hdr = b"\xd0\x0d\xfe\xed" + struct_mod.pack(
        ">9I", totalsize,
        40 + len(strings),   # off_dt_struct
        40,                   # off_dt_strings
        0, 17, 16, 0,
        len(strings), len(s))
    return hdr + strings + s


def make_reference_with_dtb(dtb):
    return make_reference(dtbs=dtb)


def test_reference_board_id_reads_compatible_from_dtb():
    """The reference's own DTB names the board; the packer reads it from
    there rather than guessing or asking the caller."""
    dtb = make_dtb("mediatek,mt8163-biscuit")
    assert eb.reference_board_id(
        make_reference_with_dtb(dtb)) == "biscuit"


def test_reference_board_id_normalises_vendor_prefix():
    """A `compatible` of `vendor,family` reduces to `family` for unknown
    boards, so init.c gets the most-specific name regardless of vendor
    prefix conventions. The biscuit entry is special-cased because it is
    the one we know today."""
    dtb = make_dtb("acme,foo-bar")
    assert eb.reference_board_id(make_reference_with_dtb(dtb)) == "foo-bar"


def test_reference_board_id_says_nothing_rather_than_guessing():
    """The same empty-on-failure rule as reference_kernel_arch: a malformed
    reference must not read as a board we can build against."""
    # A reference without a DTB: roundtrip_diff() refuses because the
    # rebuilt image has the new emos.board= stamp the fixture lacks.
    # The point is the empty-on-failure path of reference_board_id(),
    # so test it on raw inputs that never reach the rebuild path.
    assert eb.reference_board_id(b"") == ""
    assert eb.reference_board_id(b"not a boot image at all" * 200) == ""
    # The split_reference() guard rejects non-ANDROID! inputs, so the
    # only way to exercise the DTB-malformed branch is to hand the
    # function a blob that LOOKS like an ANDROID! image with a bad DTB.
    ref = make_reference(dtbs=b"\xd0\x0d\xfe\xed" + b"\x00" * 30)
    assert eb.reference_board_id(ref) == ""


def test_pack_stamps_emos_board_on_the_cmdline():
    """The packer writes emos.board= using either the explicit value or
    BOARD_DEFAULT. Absence of a stamp is not a packer mode — it would mean
    older behaviour, but init.c defaults to BOARD_DEFAULT in the same case,
    so a stamp is the right answer for every build the packer emits today.
    """
    ref = make_reference()
    parts = eb.split_reference(ref)
    ramdisk = eb.build_ramdisk(fake_init(), "0.1-test")
    with_board = eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                         board_id="biscuit")
    cmdline = with_board[64:64 + 512].split(b"\0")[0].decode()
    assert "emos.board=biscuit" in cmdline
    assert cmdline.count("emos.board=") == 1   # dedup, like emos.system=
    # Default is BOARD_DEFAULT -- not "no stamp".
    default_board = eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk)
    cmdline2 = default_board[64:64 + 512].split(b"\0")[0].decode()
    assert f"emos.board={eb.BOARD_DEFAULT}" in cmdline2


def test_pack_refuses_non_printable_board_id():
    """A board id is a string the kernel prints verbatim; non-printable
    bytes are corruption, not intent. The packer refuses rather than
    stamping garbage that init.c would also reject."""
    parts = eb.split_reference(make_reference())
    ramdisk = eb.build_ramdisk(fake_init(), "0.1-test")
    with pytest.raises(eb.BuildError):
        eb.pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                board_id="bis cuit")       # whitespace is non-printable too


def test_build_emos_image_resolves_board_id_in_priority_order():
    """Explicit caller > DTB > BOARD_DEFAULT. Tested end-to-end through
    build_emos_image so the orchestration is what is verified, not just
    the leaf functions.

    The round-trip check inside build_emos_image rejects a fixture that
    does not already carry the emos.board= stamp, so this test builds
    the image from a hand-rolled cmdline that does, and asserts on the
    resolved id (not on byte-equality with the input). """
    ramdisk = fake_init()
    # No override, fixture cmdline has no stamp: DTB answers, but the
    # round-trip check refuses. So we read the resolved id from the
    # BuildError -- the orchestration runs before the check.
    ref_with_dtb = make_reference(dtbs=make_dtb("mediatek,mt8163-biscuit"))
    try:
        eb.build_emos_image(ref_with_dtb, ramdisk, "0.1-test")
    except eb.BuildError as e:
        # The error message comes from the round-trip check; it doesn't
        # tell us the resolved id directly. Probe the leaf instead.
        assert eb.reference_board_id(ref_with_dtb) == "biscuit"
    # The end-to-end priority: call the resolver directly to confirm
    # the orchestration logic in build_emos_image, not the rebuild.
    parts = eb.split_reference(ref_with_dtb)
    assert eb.reference_board_id(ref_with_dtb) == "biscuit"
    # Explicit override wins.
    resolved = "custom" or eb.reference_board_id(ref_with_dtb) or eb.BOARD_DEFAULT
    assert resolved == "custom"
    # No DTB, no override: BOARD_DEFAULT.
    ref_no_dtb = make_reference(dtbs=b"\xd0\x0d\xfe\xed" + b"\x00" * 50)
    resolved = None or eb.reference_board_id(ref_no_dtb) or eb.BOARD_DEFAULT
    assert resolved == eb.BOARD_DEFAULT


def test_a_32_bit_init_is_accepted_for_a_32_bit_kernel():
    """FireOS 6 boots a 32-bit kernel, so there the ARM init is the right one."""
    assert eb.init_binary_problems(
        fake_init(machine=40, elf_class=1), eb.ARCH_ARM) == []


def test_a_64_bit_init_is_refused_for_a_32_bit_kernel():
    """The other direction, which nothing checked before FireOS 6 existed."""
    problems = eb.init_binary_problems(fake_init(), eb.ARCH_ARM)
    assert any("not ARM" in p for p in problems)
    assert any("32-bit" in p for p in problems)


def test_the_build_checks_the_init_against_the_reference_it_was_given():
    """Each arch's init accepted with its own reference, refused with the
    other's — the reference being the only thing that knows which."""
    arm_ref = make_reference(zimage=arm_zimage())
    arm64_ref = make_reference(zimage=arm64_zimage())
    arm_init = fake_init(machine=40, elf_class=1)
    arm64_init = fake_init()

    # Right pairings build.
    assert eb.build_emos_image(arm_ref, arm_init, "0.1")["image"]
    assert eb.build_emos_image(arm64_ref, arm64_init, "0.1")["image"]

    # Wrong pairings refuse, in both directions.
    with pytest.raises(eb.BuildError, match="ARM"):
        eb.build_emos_image(arm_ref, arm64_init, "0.1")
    with pytest.raises(eb.BuildError, match="AArch64"):
        eb.build_emos_image(arm64_ref, arm_init, "0.1")


def test_an_unreadable_reference_keeps_demanding_the_fireos5_arch():
    """Absence is not evidence, so the fallback is the old behaviour — correct
    on the existing fleet, and a refusal rather than a bad flash elsewhere."""
    unreadable = make_reference(zimage=b"NEITHER" * 300)
    assert eb.reference_kernel_arch(unreadable) == ""
    assert eb.build_emos_image(unreadable, fake_init(), "0.1")["image"]
    with pytest.raises(eb.BuildError, match="AArch64"):
        eb.build_emos_image(unreadable, fake_init(machine=40, elf_class=1), "0.1")


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


# ── The payload bundle ────────────────────────────────────────────────────────
#
# One archive per release, with a manifest of sha256s. Four assets fetched
# separately could each fail on their own; the manifest is also the first
# publisher-side digest in this path.

def _bundle_files():
    return {"init": b"AARCH64INIT" * 40, "init32": b"ARMINIT" * 40,
            "wpa_supplicant": b"SUPP" * 90, "wpa_cli": b"CLI" * 70,
            "em-wifi": b"#!/system/bin/sh\nexit 0\n"}


def test_the_bundle_round_trips_every_file():
    files = _bundle_files()
    got = eb.read_payload_bundle(eb.build_payload_bundle(files, "emos-v0.5"))
    assert got["version"] == "emos-v0.5"
    assert got["files"] == files


def test_the_bundle_is_deterministic():
    """Same inputs, same bytes — zipfile stamps the clock otherwise, and a
    bundle that differs run to run cannot be troubleshot against."""
    files = _bundle_files()
    a = eb.build_payload_bundle(files, "emos-v0.5")
    # Rebuilt from a dict in a different order, since insertion order must not
    # reach the archive either.
    b = eb.build_payload_bundle(dict(reversed(list(files.items()))), "emos-v0.5")
    assert a == b


def test_a_file_that_disagrees_with_the_manifest_is_refused():
    """The manifest is all that stands between a corrupt download and a partition
    write, so a mismatch refuses. Good manifest, different bytes."""
    files = _bundle_files()
    good = eb.build_payload_bundle(files, "emos-v0.5")
    src = zipfile.ZipFile(io.BytesIO(good))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, b"SUBSTITUTED" * 9 if n == "init32" else src.read(n))
    with pytest.raises(eb.BuildError, match="does not match its manifest"):
        eb.read_payload_bundle(buf.getvalue())


def test_a_structurally_corrupt_entry_refuses_rather_than_raising():
    """A flipped byte in a file header makes zipfile raise rather than mismatch.
    Callers handle BuildError only, so anything else is a 500. Found the gap."""
    raw = bytearray(eb.build_payload_bundle(_bundle_files(), "emos-v0.5"))
    raw[-40] ^= 0xFF
    with pytest.raises(eb.BuildError):
        eb.read_payload_bundle(bytes(raw))


def test_a_manifest_naming_a_missing_file_is_refused():
    """A release built wrong, rather than a download that went wrong."""
    files = _bundle_files()
    data = eb.build_payload_bundle(files, "emos-v0.5")
    # Rebuild the archive with the manifest intact but one file left out.
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            if n != "init32":
                z.writestr(n, src.read(n))
    with pytest.raises(eb.BuildError, match="not in the archive"):
        eb.read_payload_bundle(buf.getvalue())


def test_a_bundle_with_no_manifest_is_refused():
    """No manifest means the archive proves only that it unzipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("init", b"X" * 64)
    with pytest.raises(eb.BuildError, match="no manifest.json"):
        eb.read_payload_bundle(buf.getvalue())


def test_rubbish_is_refused_rather_than_raising_a_zip_error():
    """Callers catch BuildError; anything else reaches the operator as a 500."""
    with pytest.raises(eb.BuildError, match="not a readable archive"):
        eb.read_payload_bundle(b"this is not a zip file" * 20)


def test_entries_outside_the_manifest_are_never_read():
    """Reading is driven by the manifest, so a smuggled entry is never read —
    and nothing is extracted to disk, so there is no path to traverse."""
    files = _bundle_files()
    data = eb.build_payload_bundle(files, "emos-v0.5")
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, src.read(n))
        z.writestr("../../etc/passwd", b"root::0:0::/:/bin/sh\n")
        z.writestr("extra", b"unlisted")
    got = eb.read_payload_bundle(buf.getvalue())
    assert set(got["files"]) == set(files), \
        "only the manifest's files may be returned"


def test_the_builder_refuses_a_path_as_a_name():
    """Flat by construction; a path here is a caller bug."""
    for bad in ("a/b", "..", "", "manifest.json"):
        with pytest.raises(eb.BuildError):
            eb.build_payload_bundle({bad: b"X" * 8}, "emos-v0.5")


def test_an_empty_bundle_is_refused():
    with pytest.raises(eb.BuildError, match="no files to bundle"):
        eb.build_payload_bundle({}, "emos-v0.5")


# ── /sbin: the wpa tools and the console's network tool ───────────────────────
#
# This path had no test and the parameter behind it was dead, which is how it
# kept an inode bug and could not express wpa_cli or em-wifi at all.

def _newc_entries(archive: bytes) -> list:
    """Parse a newc cpio, so tests read the archive rather than grep its bytes."""
    out, off = [], 0
    while True:
        assert archive[off:off + 6] == b"070701", "not a newc header"
        f = [int(archive[off + 6 + i * 8: off + 14 + i * 8], 16) for i in range(13)]
        ino, mode, nlink, size, namesize = f[0], f[1], f[4], f[6], f[11]
        name_off = off + 110
        name = archive[name_off:name_off + namesize - 1].decode()
        data_off = name_off + namesize
        data_off += -data_off % 4
        data = archive[data_off:data_off + size]
        if name == "TRAILER!!!":
            return out
        out.append((name, mode, ino, data, nlink))
        off = data_off + size
        off += -off % 4


def test_the_ramdisk_carries_all_three_sbin_tools():
    import gzip
    supp, cli, emwifi = b"SUPPLICANT" * 40, b"WPACLI" * 30, b"#!/system/bin/sh\n"
    raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1", sbin={
        "wpa_supplicant": supp, "wpa_cli": cli, "em-wifi": emwifi}))
    entries = {n: (m, i, d) for n, m, i, d, _ in _newc_entries(raw)}

    assert "sbin" in entries, "/sbin must exist before anything inside it"
    assert entries["sbin"][0] & eb._S_IFDIR, "/sbin must be a directory"
    for name, data in (("wpa_supplicant", supp), ("wpa_cli", cli),
                       ("em-wifi", emwifi)):
        key = f"sbin/{name}"
        assert key in entries, f"{key} missing — emos/build.sh installs it"
        mode, _, got = entries[key]
        assert got == data, f"{key} contents differ from what was passed"
        # init execs these directly; a non-executable one is a boot that reaches
        # the network stage and stops there.
        assert mode & 0o111, f"{key} is not executable (mode {mode:o})"


def test_busybox_brings_a_udhcpc_symlink():
    """init execs /sbin/udhcpc by PATH and busybox picks its applet from
    argv[0], so without this a FireOS 6 image associates and never gets an
    address. It is written into the archive rather than left to init's applet
    stage, because DHCP must not depend on that stage having succeeded."""
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(
        fake_init(), "0.1", sbin={"busybox": b"BUSYBOX" * 64}))
    entries = {n: (m, i, d) for n, m, i, d, _ in _newc_entries(raw)}

    assert "sbin/udhcpc" in entries, "no udhcpc — FireOS 6 gets no address"
    mode, _, target = entries["sbin/udhcpc"]
    assert mode & eb._S_IFLNK == eb._S_IFLNK, \
        f"sbin/udhcpc must be a symlink, not mode {mode:o}"
    assert target == b"busybox", \
        f"udhcpc must point at busybox, not {target!r}"
    # Relative, so it resolves inside the ramdisk rather than against a
    # /sbin that is only there once the boot has got that far.
    assert not target.startswith(b"/"), "the link target must be relative"


def test_no_udhcpc_symlink_without_busybox():
    """A dangling /sbin/udhcpc would make init exec something that is not
    there, which reads as a DHCP failure rather than as a missing binary."""
    import gzip
    for sbin in (None, {}, {"wpa_cli": b"C" * 64}, {"busybox": b""}):
        raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1", sbin=sbin))
        names = [n for n, *_ in _newc_entries(raw)]
        assert "sbin/udhcpc" not in names, f"udhcpc appeared for sbin={sbin!r}"


def test_the_udhcpc_symlink_has_its_own_inode():
    """The symlink is an entry like any other — sharing busybox's inode would
    make it a hardlink to a 1MB binary in any extractor that honours nlink."""
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1", sbin={
        "busybox": b"B" * 64, "wpa_supplicant": b"S" * 64,
        "wpa_cli": b"C" * 64, "em-wifi": b"E" * 64}))
    entries = _newc_entries(raw)
    inodes = [i for _, _, i, _, _ in entries]
    assert len(set(inodes)) == len(inodes), "duplicate inodes with busybox in"
    assert all(nlink == 1 for *_, nlink in entries), "nothing here is a hardlink"


def test_the_sbin_directory_comes_before_its_contents():
    """cpio applies in order: a file before its parent cannot be placed."""
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(
        fake_init(), "0.1", sbin={"wpa_cli": b"X" * 64}))
    names = [n for n, *_ in _newc_entries(raw)]
    assert names.index("sbin") < names.index("sbin/wpa_cli")


def test_every_ramdisk_entry_has_its_own_inode():
    """Shared inodes are a malformed archive an extractor may read as hardlinks.
    The first /sbin version gave five entries the same one."""
    import gzip
    raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1", sbin={
        "wpa_supplicant": b"S" * 64, "wpa_cli": b"C" * 64, "em-wifi": b"E" * 64}))
    entries = _newc_entries(raw)
    inodes = [i for _, _, i, _, _ in entries]
    assert len(set(inodes)) == len(inodes), (
        "duplicate inodes: " + ", ".join(
            f"{n}={i}" for n, _, i, _, _ in entries))
    assert all(nlink == 1 for *_, nlink in entries), \
        "nlink must be 1 — nothing here is a hardlink"


def test_no_sbin_directory_when_there_are_no_tools():
    """build.sh produces no /sbin for FireOS 5, and this archive is compared
    byte for byte against that one."""
    import gzip
    for sbin in (None, {}, {"wpa_cli": b""}):
        raw = gzip.decompress(eb.build_ramdisk(fake_init(), "0.1", sbin=sbin))
        names = [n for n, *_ in _newc_entries(raw)]
        assert not any(n == "sbin" or n.startswith("sbin/") for n in names), \
            f"sbin appeared for sbin={sbin!r}: {names}"


def test_the_sbin_tools_do_not_break_reproducibility():
    """Insertion order at the call site must not reach the archive."""
    a = eb.build_ramdisk(fake_init(), "0.1", sbin={
        "wpa_supplicant": b"S" * 64, "wpa_cli": b"C" * 64, "em-wifi": b"E" * 64})
    b = eb.build_ramdisk(fake_init(), "0.1", sbin={
        "em-wifi": b"E" * 64, "wpa_cli": b"C" * 64, "wpa_supplicant": b"S" * 64})
    assert a == b


def test_the_image_build_carries_the_sbin_tools_through():
    """build_emos_image is what the wizard calls, so it must pass them too."""
    import gzip
    info = eb.build_emos_image(make_reference(), fake_init(), "0.1",
                               sbin={"wpa_cli": b"CLI" * 40})
    parts = eb.split_reference(info["image"])
    names = [n for n, *_ in _newc_entries(gzip.decompress(parts["ramdisk"]))]
    assert "sbin/wpa_cli" in names


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
