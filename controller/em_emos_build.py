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

THE INIT BINARY IS AN INPUT, NOT SOMETHING THIS BUILDS. It is aarch64 static,
and the controller image has no NDK — see `init_binary_problems()` for the
checks applied to whatever it is handed.

Pure standard library on purpose, so the whole packer is unit-testable without
aiohttp. See controller/CLAUDE.md.
"""

import gzip
import hashlib
import io
import struct

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

# ELF header bytes for a 64-bit little-endian AArch64 executable. Checked
# rather than assumed because the failure it prevents is silent and expensive:
# an init of the wrong architecture flashes fine, and the device then produces
# no output at all, which is indistinguishable from a kernel that never
# started. See emos/README.md.
_ELF_MAGIC = b"\x7fELF"
_ELF_CLASS64 = 2
_ELF_LITTLE = 1
_EM_AARCH64 = 183


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


def build_ramdisk(init_binary: bytes, version: str, build_id: str = "") -> bytes:
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

def mtk_wrap(payload: bytes, name: bytes) -> bytes:
    """Prepend the 0x200-byte MediaTek header LK validates before jumping."""
    hdr = struct.pack("<II", MTK_MAGIC, len(payload))
    hdr += name.ljust(32, b"\0")
    # Padded with 0x00, read off the device's own image. Padding with 0xff on
    # the strength of a note put 472 differing bytes inside the header LK
    # validates — see emos/mkboot.py.
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
        zimage=payload[:i],
        dtbs=payload[i:],
        ramdisk=ref[roff:roff + rsz],
        kaddr=kaddr, raddr=raddr, saddr=saddr, tags=tags, hdrv=hdrv, osv=osv,
        cmdline=ref[64:64 + 512].rstrip(b"\0"),
    )


def pack(parts: dict, zimage: bytes, dtbs: bytes, ramdisk: bytes,
         extra_cmdline: str = RAMOOPS_CMDLINE) -> bytes:
    """Assemble a boot image from its parts, using the reference's own header."""
    cmdline = parts["cmdline"]
    if extra_cmdline:
        cmdline = cmdline + b" " + extra_cmdline.encode()
    if len(cmdline) > 511:
        raise BuildError(
            f"the kernel command line is too long for the 512-byte field "
            f"({len(cmdline)} bytes)")

    kernel = mtk_wrap(zimage + dtbs, b"KERNEL")

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
    parts = split_reference(ref)
    rebuilt = pack(parts, parts["zimage"], parts["dtbs"], parts["ramdisk"],
                   extra_cmdline="")
    return rebuilt == ref


def init_binary_problems(init_binary: bytes) -> list:
    """Everything wrong with a candidate init, as sentences, or an empty list.

    Both properties are silent when wrong and fatal on the device: there is no
    dynamic loader at PID 1 time, and an init of the wrong architecture leaves
    a box that boots to nothing at all. Neither is worth discovering after a
    partition write.
    """
    problems = []
    if len(init_binary) < 64 or init_binary[:4] != _ELF_MAGIC:
        return ["the init binary is not an ELF executable"]
    if init_binary[4] != _ELF_CLASS64 or init_binary[5] != _ELF_LITTLE:
        problems.append("the init binary is not 64-bit little-endian")
    e_machine = struct.unpack("<H", init_binary[18:20])[0]
    if e_machine != _EM_AARCH64:
        problems.append(
            f"the init binary is not AArch64 (ELF machine {e_machine}); "
            "biscuit boots an ARM64 kernel")
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
    problems = init_binary_problems(init_binary)
    if problems:
        raise BuildError("; ".join(problems))

    parts = split_reference(reference)
    if not roundtrip_identical(reference):
        raise BuildError(
            "The packer could not reproduce this boot image byte for byte, so "
            "it does not fully understand it. Refusing to build rather than "
            "flash something assembled by a parser already shown to be wrong.")

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
        cmdline=(parts["cmdline"] + b" " + RAMOOPS_CMDLINE.encode()).decode(
            errors="replace"),
    )
