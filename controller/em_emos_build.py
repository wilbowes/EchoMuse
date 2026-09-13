"""
Build an emOS boot image from a device's OWN escrowed boot partition.

This is the controller-side half of `emos/build.sh`. It exists separately, and
that is a deliberate cost rather than an oversight: the controller image is
built with `context: controller`, so nothing outside this directory reaches the
Dockerfile, and `emos/mkboot.py` is therefore not importable from here.

**`tests/test_emos_build.py` pins this byte-identical to `emos/mkboot.py`** on a
synthetic reference, so the duplication fails a check instead of drifting
quietly. That is the same instrument the console password uses against the C in
`emos/init/pwcheck.c`, for the same reason: two implementations of one format
can only be known to agree by running both.

WHAT IS AND IS NOT REDISTRIBUTED. The kernel and the DTBs come out of the
reference image the user pulled off their own device, and go straight back in.
Nothing of Amazon's is stored here or shipped in this image; what we add is the
static `init` and a ramdisk of empty mountpoints. The artifact never leaves the
user's own infrastructure.

THE INIT BINARY IS AN INPUT, NOT SOMETHING THIS BUILDS — the controller image
has no NDK. It is static, and its ARCHITECTURE must match the reference image's
KERNEL rather than being fixed: FireOS 5 boots a 64-bit kernel and FireOS 6 a
32-bit one. `reference_kernel_arch()` reads that off the reference and
`init_binary_problems()` checks the init against it.

Pure standard library on purpose, so the whole packer is unit-testable without
aiohttp. See controller/CLAUDE.md.
"""

import gzip
import hashlib
import io
import struct
import zlib

MTK_MAGIC = 0x58881688
PAGE = 2048

# Reproduced from emos/mkboot.py, and pinned against it by the test named
# above. The comment there explains why the region is this one; the short
# version is that the vendor device tree already reserves it for ram_console,
# so a kernel that panics before userspace is still readable afterwards.
RAMOOPS_CMDLINE = (
    "ramoops.mem_address=0x44400000 ramoops.mem_size=0x200000 "
    "ramoops.record_size=0x20000 ramoops.console_size=0x80000 "
    "ramoops.dump_oops=1"
)

# ELF header bytes. Checked rather than assumed because the failure they
# prevent is silent and expensive: an init of the wrong architecture flashes
# fine, and the device then produces no output at all, which is
# indistinguishable from a kernel that never started. See emos/README.md.
_ELF_MAGIC = b"\x7fELF"
_ELF_CLASS32 = 1
_ELF_CLASS64 = 2
_ELF_LITTLE = 1
_EM_ARM = 40
_EM_AARCH64 = 183

# The two kernel architectures biscuit boots, and what an init must be for each.
#
# THE INIT MUST MATCH THE KERNEL, NOT THE USERSPACE. FireOS 5 boots a 64-bit
# kernel; FireOS 6 — the only FireOS amonet-biscuit v2.0.0 boots — a 32-bit
# build of the same 3.18.19 source. The firmware is armv7a on both, because it
# targets the Android userspace, which is 32-bit either way; only the init
# differs. Getting this wrong is what cost five flashed images of a 32-bit
# kernel under a 64-bit boot chain.
ARCH_ARM = "arm"
ARCH_ARM64 = "arm64"
_ARCH_ELF = {
    ARCH_ARM:   (_ELF_CLASS32, _EM_ARM,     "32-bit", "ARM"),
    ARCH_ARM64: (_ELF_CLASS64, _EM_AARCH64, "64-bit", "AArch64"),
}


class BuildError(Exception):
    """Anything that should stop the build with something a person can act on."""


# ── cpio (newc), written here rather than shelled out ────────────────────────
#
# `emos/build.sh` pipes `find | cpio -o -H newc | gzip -9`, which is fine on a
# workstation and wrong here: the controller image has no cpio, and shelling
# out would make the packer untestable without one. It is forty lines of
# format.
#
# Every field that could carry the clock carries a zero instead, so the same
# inputs always produce the same image. A boot image that differs run to run
# cannot be compared against the one on the device, and comparing is how the
# flash step knows the write landed.

def _newc_entry(name: str, mode: int, data: bytes, ino: int) -> bytes:
    name_b = name.encode() + b"\0"
    hdr = b"070701"
    for field in (ino, mode, 0, 0, 1, 0, len(data),
                  0, 0, 0, 0, len(name_b), 0):
        hdr += b"%08X" % field
    out = hdr + name_b
    out += b"\0" * (-len(out) % 4)
    out += data
    out += b"\0" * (-len(out) % 4)
    return out


_S_IFDIR = 0o040000
_S_IFREG = 0o100000


def build_ramdisk(init_binary: bytes, version: str, build_id: str = "",
                  supplicant: bytes = b"") -> bytes:
    """The gzipped cpio the boot image carries: init, mountpoints, os-release.

    The mountpoints have to exist in the ramdisk because there is no devtmpfs
    and nothing populates anything on its own — see emos/README.md. Everything
    the running system uses beyond this is mounted from the device's own
    /system, which is why no Amazon code is redistributed.
    """
    if not init_binary:
        raise BuildError("no init binary was supplied")

    # os-release describes the IMAGE, so it is stamped in at build time rather
    # than written at boot: a running system must not be able to drift from it.
    # BUILD_ID defaults to a digest of the inputs rather than a timestamp,
    # which keeps the image reproducible.
    if not build_id:
        build_id = hashlib.sha256(init_binary + version.encode()).hexdigest()[:16]
    os_release = (
        'NAME="emOS"\n'
        "ID=emos\n"
        f'PRETTY_NAME="emOS {version}"\n'
        f'VERSION="{version}"\n'
        f'VERSION_ID="{version}"\n'
        f'BUILD_ID="{build_id}"\n'
        'HOME_URL="https://github.com/wilbowes/EchoMuse"\n'
    ).encode()

    out = io.BytesIO()
    ino = 1
    # Sorted and fixed, so the archive is byte-stable across Python versions
    # and filesystems. `find` order is not a promise.
    for d in ("dev", "proc", "sys", "system", "data", "etc"):
        out.write(_newc_entry(d, _S_IFDIR | 0o755, b"", ino))
        ino += 1
    out.write(_newc_entry("etc/os-release", _S_IFREG | 0o644, os_release, ino))
    ino += 1
    out.write(_newc_entry("init", _S_IFREG | 0o755, init_binary, ino))
    # emOS's own wpa_supplicant, when the caller has one. Optional because an
    # image without it falls back to /system/bin/wpa_supplicant -- which is
    # what the FireOS 5 fleet runs today, and works. FireOS 6's aborts under
    # emOS (it opens /dev/binder), so a FireOS 6 image needs this one.
    if supplicant:
        out.write(_newc_entry("sbin", _S_IFDIR | 0o755, b"", ino))
        out.write(_newc_entry("sbin/wpa_supplicant", _S_IFREG | 0o755,
                              supplicant, ino))
    ino += 1
    out.write(_newc_entry("TRAILER!!!", 0, b"", ino))
    # The archive is padded to a 512-byte boundary by convention; the kernel
    # does not require it and LK never looks, but tools that read the image
    # expect it.
    raw = out.getvalue()
    raw += b"\0" * (-len(raw) % 512)

    # mtime=0 in the gzip header for the same reproducibility reason as above:
    # gzip stamps the clock into the stream otherwise.
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


# ── The Android/MTK boot image ───────────────────────────────────────────────

def mtk_wrap(payload: bytes, name: bytes, header: bytes = b"") -> bytes:
    """Prepend the 0x200-byte MediaTek header LK validates before jumping.

    `header` is the reference image's OWN 0x200 header, reused verbatim when
    given. That is the only correct answer: the padding byte after the name
    field is NOT constant across images. Amazon's own images pad it with 0xff
    and anything repacked by magiskboot pads it with 0x00, so a packer that
    picks either one is right on half the fleet and puts 472 differing bytes
    inside the header LK validates on the other half. This code has now been
    wrong in BOTH directions: an early version padded 0xff on the strength of
    a note, was corrected to 0x00 against a device that had been through the
    FireOS flow, and then refused a stock FireOS 5 + f1r30s image at offset
    0x28 of this header (measured on 3611NF, 2026-09-06).

    Reusing it is safe because an emOS build does not touch the kernel: the
    zImage and the DTBs are carried over from the reference, so the payload
    this header describes is byte-identical and its size field still holds.
    That is checked rather than assumed.
    """
    if header:
        if len(header) != 0x200:
            raise BuildError(
                f"the reference kernel header is {len(header)} bytes, expected 512")
        magic, size = struct.unpack("<II", header[:8])
        if magic != MTK_MAGIC or size != len(payload):
            raise BuildError(
                "the reference kernel header does not describe its own payload "
                f"(size says {size}, payload is {len(payload)} bytes)")
        return header + payload
    hdr = struct.pack("<II", MTK_MAGIC, len(payload))
    hdr += name.ljust(32, b"\0")
    hdr = hdr.ljust(0x200, b"\0")
    return hdr + payload


def pad(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % PAGE)


def split_reference(ref: bytes) -> dict:
    """Take the device's own boot image apart into the pieces we reuse.

    Everything here is READ OUT OF THE REFERENCE rather than taken from
    documentation — pmOS's deviceinfo for this board gives a kernel load
    address that is not the one the device actually boots with, and copying it
    produces a device that takes the flash and then does nothing.
    """
    if len(ref) < PAGE or ref[:8] != b"ANDROID!":
        raise BuildError(
            "That file is not an Android boot image. It should be the whole of "
            "mmcblk0p10 read off the device, not a file from inside it.")
    ksz, kaddr, rsz, raddr, ssz, saddr, tags, psz, hdrv, osv = struct.unpack(
        "<10I", ref[8:48])
    if psz != PAGE:
        raise BuildError(f"unexpected page size {psz} in the reference image")
    kernel = ref[PAGE:PAGE + ksz]
    if len(kernel) < 0x200 or struct.unpack("<I", kernel[:4])[0] != MTK_MAGIC:
        raise BuildError("the reference kernel is not MTK-wrapped as expected")
    payload = kernel[0x200:]
    i = payload.find(b"\xd0\x0d\xfe\xed")   # first DTB magic ends the zImage
    if i < 0:
        raise BuildError("no DTB found in the reference kernel payload")
    roff = PAGE + len(pad(kernel))
    return dict(
        # The reference's own MTK kernel header, reused verbatim by pack().
        # See mtk_wrap: the padding byte after the name field is not constant
        # across images, so synthesising one is right on half the fleet.
        mtkhdr=kernel[:0x200],
        zimage=payload[:i],
        dtbs=payload[i:],
        ramdisk=ref[roff:roff + rsz],
        kaddr=kaddr, raddr=raddr, saddr=saddr, tags=tags, hdrv=hdrv, osv=osv,
        cmdline=ref[64:64 + 512].rstrip(b"\0"),
    )


def reference_kernel_arch(ref: bytes) -> str:
    """Which architecture the reference image's KERNEL is, or "" if unreadable.

    Read out of the reference rather than asked of the user or inferred from
    anything else, for split_reference's reason: the device's own image is the
    only thing that knows, and every other source has been wrong at least once.

    Two shapes, both taken off real images. An ARM zImage is a self-decompressing
    executable carrying 0x016f2818 at offset 0x24. An AArch64 kernel has no such
    stub — it is a raw gzip stream whose decompressed Image carries the literal
    "ARM\\x64" at 0x38.

    "" means the payload matched neither, which callers must NOT read as either
    architecture: emos/build.sh refuses to build on it, and init_binary_problems
    keeps demanding the FireOS 5 arch, so an unrecognised image can cost a
    refusal but never a wrong flash.
    """
    if len(ref) < PAGE or ref[:8] != b"ANDROID!":
        return ""
    ksz = struct.unpack("<I", ref[8:12])[0]
    payload = ref[PAGE:PAGE + ksz][0x200:]
    if payload[0x24:0x28] == b"\x18\x28\x6f\x01":
        return ARCH_ARM
    if payload[:2] == b"\x1f\x8b":
        try:
            # 31 = gzip wrapper. Only the first 0x40 bytes are needed, and a
            # truncated stream raises rather than answering — an Image whose
            # head cannot be decompressed is not evidence of anything.
            head = zlib.decompressobj(31).decompress(payload, 0x40)
        except zlib.error:
            return ""
        if head[0x38:0x3c] == b"ARM\x64":
            return ARCH_ARM64
    return ""


def pack(parts: dict, zimage: bytes, dtbs: bytes, ramdisk: bytes,
         extra_cmdline: str = RAMOOPS_CMDLINE) -> bytes:
    """Assemble a boot image from its parts, using the reference's own header."""
    cmdline = parts["cmdline"]
    # Appended only if it is not already there.
    #
    # The reference is normally a FireOS image, which carries none of this. But
    # rebuilding an emOS image FROM an emOS image — which is what an in-place
    # update does — hands us a cmdline that already ends in these parameters,
    # and appending blindly doubles them. Every rebuild would add another copy
    # until the 511-byte field overflowed and the build failed, on the third
    # pass. Found 2026-09-06 building 0.3 from Test Echo 2's own partition,
    # which is the first time anything has repacked an emOS image.
    if extra_cmdline and extra_cmdline.encode() not in cmdline:
        cmdline = cmdline + b" " + extra_cmdline.encode()
    if len(cmdline) > 511:
        raise BuildError(
            f"the kernel command line is too long for the 512-byte field "
            f"({len(cmdline)} bytes)")

    kernel = mtk_wrap(zimage + dtbs, b"KERNEL", parts.get("mtkhdr", b""))

    hdr = b"ANDROID!"
    hdr += struct.pack("<10I", len(kernel), parts["kaddr"],
                       len(ramdisk), parts["raddr"],
                       0, parts["saddr"], parts["tags"], PAGE,
                       parts["hdrv"], parts["osv"])
    hdr += b"\0" * 16                       # product name, empty in the vendor image
    hdr += cmdline.ljust(512, b"\0")
    # The Android image id: SHA1 over each region followed by its length, in
    # kernel/ramdisk/second order. Stock carries a real one, so we do too
    # rather than ship zeroes and find out the hard way what reads it.
    digest = hashlib.sha1()
    for region in (kernel, ramdisk, b""):
        digest.update(region)
        digest.update(struct.pack("<I", len(region)))
    hdr += digest.digest().ljust(32, b"\0")
    hdr += b"\0" * 1024                     # extra cmdline

    return pad(hdr) + pad(kernel) + pad(ramdisk)


def roundtrip_identical(ref: bytes) -> bool:
    """Repack the reference from its own parts and require the same bytes back.

    THE GATE THAT RUNS BEFORE ANY FLASH. It costs milliseconds, needs no
    hardware, and it is the only check available that exercises the packer
    against THIS device's actual image rather than against a fixture. It found
    two real defects at zero risk during development — a header padded with the
    wrong byte, and a kernel put back uncompressed.

    A False here means the packer does not understand this particular image,
    and the correct response is to refuse to build rather than to flash
    something assembled by a parser that has already been shown to be wrong.
    """
    return roundtrip_diff(ref) is None


# Which header field a byte offset falls in, for the message below. Offsets are
# the Android boot header v0 layout, which split_reference already assumes.
_HDR_FIELDS = (
    (0, 8, "the ANDROID! magic"),
    (8, 48, "the size/address fields"),
    (48, 64, "the product name"),
    (64, 576, "the kernel command line"),
    (576, 608, "the image id (a SHA1 of the regions)"),
    (608, 1632, "the extra command line"),
)


# The image id: 20 bytes of SHA1 padded to 32, at offset 576 of the header.
_ID_START, _ID_END = 576, 608


def roundtrip_diff(ref: bytes, ignore_id: bool = False):
    """Where the repacked reference stops matching the original, or None.

    Split out of roundtrip_identical because a bare False is not actionable.
    The refusal it drives is correct and the operator could do nothing with it:
    on 2026-09-06 a stock+f1r30s image was refused, and finding out why meant
    dumping the header by hand over adb and decoding it a field at a time.

    Returns (offset, description, ref_bytes, rebuilt_bytes) for the first
    difference, with 16 bytes of context either side.
    """
    parts = split_reference(ref)
    try:
        rebuilt = pack(parts, parts["zimage"], parts["dtbs"], parts["ramdisk"],
                       extra_cmdline="")
    except BuildError as e:
        # A structural refusal from pack() IS a difference, and a more specific
        # one than an offset — reported rather than raised so this function
        # keeps its contract of returning a diff or None.
        return (0, str(e), b"", b"")
    if rebuilt == ref:
        return None
    if len(rebuilt) != len(ref):
        return (min(len(rebuilt), len(ref)),
                f"the lengths differ: the reference is {len(ref)} bytes and "
                f"repacking it gives {len(rebuilt)}", b"", b"")
    rng = range(len(ref))
    if ignore_id:
        rng = [i for i in rng if not (_ID_START <= i < _ID_END)]
    diffs = [i for i in rng if ref[i] != rebuilt[i]]
    if not diffs:
        return None
    off = diffs[0]

    where = f"offset {off}"
    for start, end, name in _HDR_FIELDS:
        if start <= off < end:
            where = f"{name} (offset {off})"
            break
    else:
        page = parts.get("psz", PAGE)
        koff = page + len(pad(pack_kernel_of(parts)))
        where = (f"the kernel image (offset {off})" if off < koff
                 else f"the ramdisk (offset {off})")
    return (off, where, ref[off:off + 16], rebuilt[off:off + 16])


def pack_kernel_of(parts: dict) -> bytes:
    """The MTK-wrapped kernel as pack() will emit it. Used only for locating a
    difference; pack() builds its own."""
    return mtk_wrap(parts["zimage"] + parts["dtbs"], b"KERNEL",
                    parts.get("mtkhdr", b""))


def init_binary_problems(init_binary: bytes, arch: str = ARCH_ARM64) -> list:
    """Everything wrong with a candidate init, as sentences, or an empty list.

    Both properties are silent when wrong and fatal on the device: there is no
    dynamic loader at PID 1 time, and an init of the wrong architecture leaves
    a box that boots to nothing at all. Neither is worth discovering after a
    partition write.

    `arch` is the REFERENCE KERNEL's architecture, from reference_kernel_arch —
    not a property of the init and not a preference. It defaults to the FireOS 5
    answer, which is what this checked unconditionally before FireOS 6 was
    supported, so a caller with no reference to hand, or one holding an image
    whose kernel could not be read, keeps exactly the old behaviour: correct on
    the existing fleet, and a refusal rather than a bad flash anywhere else.
    """
    problems = []
    if len(init_binary) < 64 or init_binary[:4] != _ELF_MAGIC:
        return ["the init binary is not an ELF executable"]
    elf_class, e_machine_want, width, machine_name = _ARCH_ELF.get(
        arch, _ARCH_ELF[ARCH_ARM64])
    if init_binary[4] != elf_class or init_binary[5] != _ELF_LITTLE:
        problems.append(f"the init binary is not {width} little-endian")
    e_machine = struct.unpack("<H", init_binary[18:20])[0]
    if e_machine != e_machine_want:
        problems.append(
            f"the init binary is not {machine_name} (ELF machine {e_machine}); "
            f"this device's kernel is {arch}")
    e_type = struct.unpack("<H", init_binary[16:18])[0]
    # ET_EXEC (2) is what -static produces. ET_DYN (3) is a PIE, which needs an
    # interpreter this system does not have at PID 1.
    if e_type != 2:
        problems.append(
            "the init binary is not statically linked (it is position "
            "independent, so it needs a dynamic loader that does not exist "
            "at PID 1)")
    return problems


def build_emos_image(reference: bytes, init_binary: bytes, version: str,
                     build_id: str = "") -> dict:
    """Build the image, refusing rather than warning at every gate.

    Returns the image and what went into it, so the wizard can show the user
    the numbers it decided on rather than asking them to trust the result.
    """
    # Against the REFERENCE's kernel, not a constant: this same function builds
    # for both a 64-bit FireOS 5 kernel and a 32-bit FireOS 6 one, and the only
    # thing that knows which is the image the user read off their own device.
    arch = reference_kernel_arch(reference)
    problems = init_binary_problems(init_binary, arch or ARCH_ARM64)
    if problems:
        raise BuildError("; ".join(problems))

    parts = split_reference(reference)
    # Everything EXCEPT the image id has to reproduce byte for byte.
    #
    # The id is a SHA1 over the kernel and ramdisk — regions this build is
    # about to replace outright, since the whole point is to swap the ramdisk
    # for emOS's. So the reference's id is not an input to anything we emit:
    # pack() computes a fresh one over the new contents, and the image we flash
    # carries a correct id whatever the reference carried.
    #
    # Requiring it to match therefore tests the wrong thing. It asks whether
    # whichever tool last wrote this image recomputed a checksum, not whether
    # WE understand the layout — and a tool that repacks a ramdisk while
    # preserving the header verbatim leaves a stale id that is impossible to
    # reproduce by definition. That is what f1r30s does: measured 2026-09-06 on
    # a stock FireOS 5 + f1r30s image, the id was the ONLY difference, and
    # refusing over it blocked the exact state docs/rooting.md tells users to
    # be in.
    #
    # Everything else still has to match exactly, and that is the half that
    # carries the meaning: if the kernel, the ramdisk, the addresses, the
    # cmdline or the padding disagree, the packer has misread the image and
    # the refusal stands.
    # The id is not required to reproduce, and the reference's md5 is checked
    # on arrival instead (_post_provision_emos_image).
    #
    # This gate USED to double as an integrity check on the escrowed image: the
    # stored SHA1 covers the kernel and ramdisk, so a byte corrupted in
    # transfer changed the repack and was refused. That property is real and
    # was worth keeping, so it has been replaced rather than dropped — by an
    # md5 over the WHOLE transfer, which is strictly better than a checksum
    # covering two of its regions.
    #
    # Note there is no way to keep it here. "Stored id does not match the
    # regions" is equally true of a tool that left a stale id and of a byte
    # corrupted in flight, so any rule that tolerates the first tolerates the
    # second. A heuristic that appears to separate them would fire in exactly
    # the cases the strict check was for, which is worse than not having one.
    diff = roundtrip_diff(reference, ignore_id=True)
    if diff is not None:
        _, where, got, made = diff
        detail = f" It first differs in {where}"
        if got or made:
            detail += (f": the image has {got.hex()} and repacking it produces "
                       f"{made.hex()}")
        detail += "."
        raise BuildError(
            "The packer could not reproduce this boot image byte for byte, so "
            "it does not fully understand it. Refusing to build rather than "
            "flash something assembled by a parser already shown to be wrong."
            + detail)

    ramdisk = build_ramdisk(init_binary, version, build_id)
    image = pack(parts, parts["zimage"], parts["dtbs"], ramdisk)
    return dict(
        image=image,
        md5=hashlib.md5(image).hexdigest(),
        sha256=hashlib.sha256(image).hexdigest(),
        size=len(image),
        reference_md5=hashlib.md5(reference).hexdigest(),
        reference_size=len(reference),
        zimage_size=len(parts["zimage"]),
        dtb_size=len(parts["dtbs"]),
        ramdisk_size=len(ramdisk),
        kernel_addr=parts["kaddr"],
        # Read back out of the image rather than reconstructed, so what the
        # wizard shows is what was actually written. Rebuilding it here meant
        # duplicating pack()'s rule, and the copies disagreed the moment pack
        # learned not to append a cmdline it already had — reporting the
        # ramoops parameters twice for an image that carried them once.
        cmdline=image[64:64 + 512].rstrip(b"\0").decode(errors="replace"),
    )
