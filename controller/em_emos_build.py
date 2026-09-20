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
import json
import struct
import zipfile
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

# The init must match the KERNEL, not the userspace: FireOS 5 boots 64-bit,
# FireOS 6 a 32-bit build of the same 3.18.19 source. The firmware is armv7a on
# both. Getting this wrong cost five flashed images that never executed.
ARCH_ARM = "arm"
ARCH_ARM64 = "arm64"
_ARCH_ELF = {
    ARCH_ARM:   (_ELF_CLASS32, _EM_ARM,     "32-bit", "ARM"),
    ARCH_ARM64: (_ELF_CLASS64, _EM_AARCH64, "64-bit", "AArch64"),
}


class BuildError(Exception):
    """Anything that should stop the build with something a person can act on."""


# ── The payload bundle ───────────────────────────────────────────────────────
#
# One archive per release: both inits, the WiFi userspace, and a manifest of
# sha256s. Four separate assets could each be missing or fail on their own, so a
# partially published release could hand a build a mismatched set. The manifest
# is also the first publisher-side digest in this path — em_firmware's md5 only
# compares a cached file against bytes we downloaded ourselves.
#
# The loose `init` asset stays published alongside: _fetch_latest_emos_release
# matches it by exact name, so dropping it strands every fielded controller.
PAYLOAD_MANIFEST = "manifest.json"

# zipfile stamps the clock into every entry otherwise; 1980 is the zip minimum.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def build_payload_bundle(files: dict, version: str) -> bytes:
    """The bundle for one emOS release: `files` is name -> bytes.

    Deterministic: fixed timestamps, sorted entries, fixed compression.
    """
    if not files:
        raise BuildError("no files to bundle")
    for name in files:
        # Flat by construction, so nothing downstream reasons about traversal.
        if "/" in name or "\\" in name or name in ("", ".", "..") \
                or name == PAYLOAD_MANIFEST:
            raise BuildError(f"bad name for a bundle entry: {name!r}")

    manifest = {
        "version": version,
        "files": {n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
                  for n, d in sorted(files.items())},
    }
    body = json.dumps(manifest, indent=2, sort_keys=True).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in [(PAYLOAD_MANIFEST, body)] + sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o755 << 16
            z.writestr(info, data)
    return buf.getvalue()


def read_payload_bundle(data: bytes) -> dict:
    """Open a bundle: {"version": str, "files": {name: bytes}}.

    Files are read by name from the manifest and checked against its sha256s;
    nothing is extracted to disk, so entries not in the manifest are never
    touched. Every fault raises BuildError — the caller's next move is a
    partition write.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BuildError(f"the emOS payload is not a readable archive: {e}")

    try:
        manifest = json.loads(z.read(PAYLOAD_MANIFEST))
    except KeyError:
        raise BuildError(
            f"the emOS payload carries no {PAYLOAD_MANIFEST}, so there is "
            f"nothing to check its contents against")
    except (ValueError, zipfile.BadZipFile) as e:
        raise BuildError(f"the emOS payload's manifest is unreadable: {e}")

    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries:
        raise BuildError("the emOS payload's manifest lists no files")

    out = {}
    for name, meta in entries.items():
        try:
            blob = z.read(name)
        except KeyError:
            raise BuildError(
                f"the emOS payload's manifest names {name!r}, which is not in "
                f"the archive")
        except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as e:
            # Callers handle BuildError and nothing else; zipfile's own
            # exception would surface as a 500 pointing at nothing.
            raise BuildError(
                f"{name} in the emOS payload could not be read ({e}) — the "
                f"download is corrupt")
        want = (meta or {}).get("sha256")
        if not want:
            raise BuildError(f"the manifest records no sha256 for {name!r}")
        got = hashlib.sha256(blob).hexdigest()
        if got != want:
            raise BuildError(
                f"{name} in the emOS payload does not match its manifest "
                f"(sha256 {got[:16]}… against {want[:16]}…) — the download is "
                f"corrupt or the release was built wrong")
        out[name] = blob

    return {"version": manifest.get("version", ""), "files": out}


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
# In newc a symlink is an ordinary entry whose DATA is the target path — no
# terminator, no special casing anywhere else in the writer.
_S_IFLNK = 0o120000


def build_ramdisk(init_binary: bytes, version: str, build_id: str = "",
                  sbin: dict = None) -> bytes:
    """The gzipped cpio the boot image carries: init, mountpoints, os-release.

    The mountpoints have to exist in the ramdisk because there is no devtmpfs
    and nothing populates anything on its own — see emos/README.md. Everything
    the running system uses beyond this is mounted from the device's own
    /system, which is why no Amazon code is redistributed.

    `sbin` maps name -> bytes for what emOS carries in /sbin: `wpa_supplicant`,
    `wpa_cli`, `em-wifi`, `busybox`. Optional — a FireOS 5 image falls back to
    /system/bin/wpa_supplicant, which the fleet runs today. FireOS 6 needs ours,
    since Amazon's aborts under emOS before main() (it opens /dev/binder).

    `busybox` also gets a `sbin/udhcpc` SYMLINK, because init execs
    /sbin/udhcpc by path and busybox picks its applet from argv[0]. The link is
    written here rather than left to init's applet stage: that stage is what
    populates /sbin from whatever busybox it finds, and if it fails, DHCP on
    FireOS 6 fails with it. A symlink in the archive costs nine bytes and does
    not depend on a stage having run.
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
    _sbin_written = False
    # Sorted and fixed, so the archive is byte-stable across Python versions
    # and filesystems. `find` order is not a promise.
    for d in ("dev", "proc", "sys", "system", "data", "etc"):
        out.write(_newc_entry(d, _S_IFDIR | 0o755, b"", ino))
        ino += 1
    out.write(_newc_entry("etc/os-release", _S_IFREG | 0o644, os_release, ino))
    ino += 1
    out.write(_newc_entry("init", _S_IFREG | 0o755, init_binary, ino))
    ino += 1

    # /sbin only if something goes in it: build.sh produces none for FireOS 5,
    # and this archive is compared byte for byte against that one.
    #
    # Every entry needs its OWN inode — in newc, c_ino plus c_nlink is how
    # hardlinks are represented, so shared inodes are a malformed archive an
    # extractor may read as links to one file. Sorted, so a dict's insertion
    # order cannot reach the image.
    for name in sorted((sbin or {})):
        data = (sbin or {})[name]
        if not data:
            continue
        if not _sbin_written:
            out.write(_newc_entry("sbin", _S_IFDIR | 0o755, b"", ino))
            ino += 1
            _sbin_written = True
        out.write(_newc_entry(f"sbin/{name}", _S_IFREG | 0o755, data, ino))
        ino += 1

    # After the files, so the name ordering above stays purely alphabetical and
    # the archive is a function of the inputs alone.
    if (sbin or {}).get("busybox"):
        out.write(_newc_entry("sbin/udhcpc", _S_IFLNK | 0o777, b"busybox", ino))
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

    Read off the reference for split_reference's reason: the device's own image
    is the only thing that knows. An ARM zImage carries 0x016f2818 at 0x24; an
    AArch64 kernel is a gzip stream whose Image carries "ARM\\x64" at 0x38.

    "" must not be read as either architecture — build.sh refuses on it and
    init_binary_problems keeps demanding AArch64, so it costs a refusal, never a
    wrong flash.
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


# ── The flattened device tree ────────────────────────────────────────────────
#
# reference_board_id() reads `compatible` out of the first DTB in the
# reference kernel payload. The property is a string list; the most
# specific entry is LAST by FDT convention, and on the Echo Dot Gen 2
# it is `mediatek,mt8163-biscuit`. The kernel picks the most specific
# entry against its own match tables, so the LAST one is the answer
# we want to stamp.
#
# Parsing an FDT in ~70 lines of pure stdlib avoids dragging in dtc,
# libfdt, or any C extension. The format is documented in the
# boot.txt in the kernel tree and stable across the 3.x and 4.x
# versions that ship with the devices emOS supports.
FDT_MAGIC       = 0xd00dfeed
FDT_BEGIN_NODE  = 1
FDT_END_NODE    = 2
FDT_PROP        = 3
FDT_NOP         = 4
FDT_END         = 9


def _fdt_compatible(dtb: bytes) -> str:
    """The last `compatible` string in the FDT root, or empty on no answer.

    A walk that stops at the first depth-zero END_NODE rather than
    parsing every node: `compatible` lives at the root by FDT
    convention, so a deeper search would burn cycles for nothing.
    Anything malformed (truncated header, offset past end, unterminated
    node name) returns empty rather than raising -- the failure surface
    is "stamp defaults to biscuit", not "build refuses", which keeps an
    unusual DTB from blocking a working image.

    Property values in FDT are a 4-byte-aligned byte string; for
    `compatible`, which is a string list, the bytes are two NUL-
    separated strings and a trailing NUL. Split, take the LAST entry
    (the most specific), strip the NUL. """
    # The packer above searches for the FDT magic by the byte sequence
    # `b"\\xd0\\x0d\\xfe\\xed"` (the pre-existing search bug, see the
    # reference_board_id comment). Comparing against the same byte
    # sequence directly keeps this parser in step with the search,
    # rather than depending on the byte-order of struct.unpack.
    if len(dtb) < 40 or dtb[:4] != b"\xd0\x0d\xfe\xed":
        return ""
    # FDT header fields are big-endian on the wire per the spec. Use ">I"
    # rather than "<I" so the parser matches what a real device emits.
    off_struct, off_strings = struct.unpack(
        ">2I", dtb[8:16])
    if off_struct >= len(dtb) or off_strings >= len(dtb):
        return ""

    # Walk the struct block looking for `compatible` at the root. depth
    # starts at 0 (outside any node) and BEGIN_NODE increments it; the
    # root node's properties live at depth 1. We only record properties
    # at depth 1 because `compatible` is a root property on every
    # device tree we care about, and a deeper search would burn cycles
    # for nothing.
    depth = 0
    p = off_struct
    compatible = ""
    while p + 8 <= len(dtb):
        # FDT tokens and property-length fields are big-endian too.
        tok = struct.unpack(">I", dtb[p:p + 4])[0]
        p += 4
        if tok == FDT_END:
            break
        if tok == FDT_NOP:
            continue
        if tok == FDT_BEGIN_NODE:
            # Node name: NUL-terminated, padded to a multiple of 4 bytes
            # TOTAL (including the NUL) from the start of the name. The
            # FDT spec rounds from the START of the field, not from
            # `name_end + 1`, which matters for empty names: a single NUL
            # at an aligned position needs three padding bytes, and
            # rounding from the wrong end loses them.
            name_start = p
            name_end = dtb.find(b"\x00", p)
            if name_end < 0:
                return ""
            name_size = ((name_end - name_start) + 1 + 3) & ~3
            p = name_start + name_size
            depth += 1
            continue
        if tok == FDT_END_NODE:
            depth -= 1
            continue
        if tok == FDT_PROP:
            if p + 8 > len(dtb):
                return ""
            valen, nameoff = struct.unpack(">II", dtb[p:p + 8])
            p += 8
            # Property name from the strings block.
            nend = dtb.find(b"\x00", off_strings + nameoff)
            if nend < 0:
                return ""
            name = dtb[off_strings + nameoff:nend].decode(
                "ascii", errors="replace")
            if name == "compatible" and depth == 1:
                # String list: NUL-separated, last entry is the most
                # specific. Read valen bytes, split, take the last.
                if p + valen > len(dtb):
                    return ""
                entries = dtb[p:p + valen].split(b"\x00")
                # Filter empty trailing entries from the split.
                entries = [e for e in entries if e]
                if entries:
                    compatible = entries[-1].decode("ascii", errors="replace")
            # Align from the start of the value, not from p + valen -- the
            # latter is already aligned for valen % 4 == 0 and would skip
            # the mandatory 4 bytes of padding the spec requires.
            p += (valen + 3) & ~3
            continue
        # Unknown token -- bail rather than guess.
        return ""
    return compatible


def reference_board_id(ref: bytes) -> str:
    """The DTB-compatible string of the reference, or empty on no answer.

    Mirrors reference_kernel_arch's empty-on-failure rule: a builder
    that cannot tell which board the device is gets a refusal-or-default
    surface rather than a wrong guess. The packer falls back to a
    caller-supplied value (env var, wizard input) on empty, so a
    missing DTB just means the caller picks the board. """
    if len(ref) < PAGE or ref[:8] != b"ANDROID!":
        return ""
    ksz = struct.unpack("<I", ref[8:12])[0]
    payload = ref[PAGE:PAGE + ksz][0x200:]
    i = payload.find(b"\xd0\x0d\xfe\xed")   # first DTB magic ends the zImage
    if i < 0:
        return ""
    dtb = payload[i:]
    name = _fdt_compatible(dtb)
    # The most specific FDT entry on biscuit is "mediatek,mt8163-biscuit".
    # Normalise it to "biscuit" so init.c's BOARD_DEFAULT ("biscuit") is
    # what the stamp carries. A new board adds one elif here.
    if name == "mediatek,mt8163-biscuit":
        return "biscuit"
    if name:
        return name.split(",")[-1]   # vendor,family -> family, conservatively
    return ""


# The partition holding the FireOS userspace an image was built beside, stamped
# onto its own cmdline so emOS can mount the right one — see cmdline_system_part
# in emos/init/init.c, and emos/init/cmdlinecheck.c, which pins this format.
#
# A full device path rather than a bare number because this is the field
# somebody supporting a device gets asked to read out of `od` on the image or
# `/proc/cmdline` on the device, and "13" alone says nothing.
SYSTEM_CMDLINE_KEY = "emos.system="

# The board this image is built against, stamped onto its own cmdline so
# init.c can resolve which boards_*.c runtime and which constants to use.
# See cmdline_board() in emos/init/init.c, and emos/init/cmdlinecheck.c,
# which pins this format.
#
# Default is "biscuit" — see BOARD_DEFAULT in init.c — so older images
# built before this stamp existed keep booting against the only board
# init currently supports. The packer passes an explicit value when it
# has one (read off the DTB or set via env), so the stamp travels with
# the image rather than being guessed at each boot.
BOARD_CMDLINE_KEY = "emos.board="
BOARD_DEFAULT = "biscuit"


def _stamp_cmdline_key(cmdline: bytes, key: str, value: str) -> bytes:
    """Set `key=value` on a cmdline, replacing any value already there.

    Not the same dedup as the ramoops block below, and it cannot be: that one
    asks whether the WHOLE string is already present, which is right for a
    fixed block and wrong for a key whose value changes. Rebuilding an emOS
    image stamped p13 against a device wanting p14 would append a second
    `emos.system=`, and the kernel takes the LAST of a repeated parameter — so
    the image would work, carry two contradictory stamps, and read as whichever
    one somebody happened to look at.
    """
    kept = [tok for tok in cmdline.split() if not tok.startswith(key.encode())]
    kept.append(f"{key}{value}".encode())
    return b" ".join(kept)


def pack(parts: dict, zimage: bytes, dtbs: bytes, ramdisk: bytes,
         extra_cmdline: str = RAMOOPS_CMDLINE, system_part: int = None,
         board_id: str = BOARD_DEFAULT) -> bytes:
    """Assemble a boot image from its parts, using the reference's own header.

    `board_id` is the value to stamp under emos.board=. Defaults to
    BOARD_DEFAULT ("biscuit") so an explicit None is not necessary and
    older callers stay byte-for-byte equivalent. mkboot.py stamps the
    same default when its env var is unset and its DTB has no answer,
    which is what the cross-implementation parity test pins against. """
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
    # After the ramoops block, so the stamps are last and most visible in a
    # dump. BOARD is stamped last so the order in a hex dump reads
    # "ramoops ... emos.system= ... emos.board=".
    if system_part is not None:
        if not 1 <= int(system_part) <= 127:
            raise BuildError(
                f"the /system partition must be an mmcblk0 partition number, "
                f"not {system_part!r}")
        cmdline = _stamp_cmdline_key(
            cmdline, SYSTEM_CMDLINE_KEY,
            f"/dev/block/mmcblk0p{int(system_part)}")
    if board_id:
        # Same printable-ASCII rule as cmdline_board() in init.c -- the
        # value is a string the kernel prints verbatim, and a corrupt one
        # is a board id we cannot reason about.
        if not all(0x21 <= ord(c) <= 0x7e for c in board_id):
            raise BuildError(
                f"the board id must be non-empty printable ASCII, "
                f"not {board_id!r}")
        cmdline = _stamp_cmdline_key(cmdline, BOARD_CMDLINE_KEY, board_id)
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

    `arch` is the REFERENCE KERNEL's architecture, from reference_kernel_arch.
    It defaults to the FireOS 5 answer — what this checked unconditionally before
    FireOS 6 — so a caller without a reference keeps the old behaviour.
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
                     build_id: str = "", sbin: dict = None,
                     system_part: int = None,
                     board_id: str = None) -> dict:
    """Build the image, refusing rather than warning at every gate.

    Returns the image and what went into it, so the wizard can show the user
    the numbers it decided on rather than asking them to trust the result.

    `system_part` is the partition holding the FireOS userspace this reference
    was read beside, stamped onto the image's own cmdline. It is passed rather
    than derived because only the caller knows it: the wizard resolves
    system_a/system_b through TWRP's by-name map, which is the one place those
    names exist. Omitted, the image carries no stamp and emOS falls back to the
    partition it hardcoded before this existed, so older behaviour is kept.

    `board_id` is the value to stamp under emos.board=. None means "let the
    packer decide": it tries reference_board_id() against the DTB and falls
    back to BOARD_DEFAULT ("biscuit") on no match, the same default init.c
    applies. An explicit value here wins over both -- the wizard passes one
    when the user has named the device. """
    # Against the REFERENCE's kernel, not a constant: the same function builds
    # for both, and only the user's own image knows which.
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

    ramdisk = build_ramdisk(init_binary, version, build_id, sbin)
    # Resolve the board id: explicit caller wins, then DTB-compatible
    # string from the reference, then BOARD_DEFAULT. The DTB sniff is the
    # honest source for a board we have not been told about by name; the
    # default keeps older callers (and older images) working.
    resolved_board = board_id or reference_board_id(reference) or BOARD_DEFAULT
    image = pack(parts, parts["zimage"], parts["dtbs"], ramdisk,
                 system_part=system_part, board_id=resolved_board)
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
        board_id=resolved_board,
        kernel_addr=parts["kaddr"],
        # Read back out of the image rather than reconstructed, so what the
        # wizard shows is what was actually written. Rebuilding it here meant
        # duplicating pack()'s rule, and the copies disagreed the moment pack
        # learned not to append a cmdline it already had — reporting the
        # ramoops parameters twice for an image that carried them once.
        cmdline=image[64:64 + 512].rstrip(b"\0").decode(errors="replace"),
    )
