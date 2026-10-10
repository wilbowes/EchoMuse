#!/usr/bin/env python3
"""Pack a 6.1 zImage + initramfs into the MTK/Android boot image biscuit's LK expects.

Every constant here is READ OUT OF THE DEVICE'S OWN WORKING IMAGE rather than
taken from documentation. pmOS's deviceinfo for this board, for instance, gives
a kernel load address of 0x40008000; the image the device actually boots uses
0x40080000. Copying the wrong one produces a device that takes the flash and
then does nothing, which is the single most expensive failure mode available
here — so the reference image is the source of truth and this script asserts
against it.

Layout, confirmed by parsing boot_a_x off a running device:

    page 0            Android boot header ("ANDROID!"), page_size 2048
    kernel area       MTK header (0x200, magic 0x58881688, name "KERNEL")
                      followed by zImage then one or more appended DTBs
    ramdisk area      RAW gzip cpio — NOT MTK-wrapped, unlike the kernel

The DTBs are carried over verbatim from the reference image. LK selects among
the three by board revision, and reproducing that selection ourselves is work
with no upside for a first boot test.
"""
import hashlib
import struct
import os
import sys

MTK_MAGIC = 0x58881688
PAGE = 2048


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
            raise SystemExit(
                f"the reference kernel header is {len(header)} bytes, expected 512")
        magic, size = struct.unpack("<II", header[:8])
        if magic != MTK_MAGIC or size != len(payload):
            raise SystemExit(
                "the reference kernel header does not describe its own payload "
                f"(size says {size}, payload is {len(payload)} bytes)")
        return header + payload
    hdr = struct.pack("<II", MTK_MAGIC, len(payload))
    hdr += name.ljust(32, b"\0")
    hdr = hdr.ljust(0x200, b"\0")
    return hdr + payload


def pad(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % PAGE)


def split_reference(ref: bytes):
    """Return (dtb_blob, header_fields) from the device's own boot image."""
    if ref[:8] != b"ANDROID!":
        raise SystemExit("reference image is not an Android boot image")
    f = struct.unpack("<10I", ref[8:48])
    ksz, kaddr, rsz, raddr, ssz, saddr, tags, psz, hdrv, osv = f
    if psz != PAGE:
        raise SystemExit(f"unexpected page size {psz}")
    kernel = ref[PAGE:PAGE + ksz]
    if struct.unpack("<I", kernel[:4])[0] != MTK_MAGIC:
        raise SystemExit("reference kernel is not MTK-wrapped as expected")
    payload = kernel[0x200:]
    # First DTB magic marks the end of the zImage and the start of the blobs.
    i = payload.find(b"\xd0\x0d\xfe\xed")
    if i < 0:
        raise SystemExit("no DTB found in reference kernel payload")
    return payload[i:], dict(mtkhdr=kernel[:0x200],
                             kaddr=kaddr, raddr=raddr, saddr=saddr,
                             tags=tags, hdrv=hdrv, osv=osv,
                             cmdline=ref[64:64 + 512].rstrip(b"\0"))


# Point ramoops at the region the VENDOR device tree already reserves:
#
#   ram_console-reserved-memory@44400000 {
#       compatible = "mediatek,ram_console";
#       reg = <0x00 0x44400000 0x00 0x200000>;   /* 2MB */
#   };
#
# That matters twice over. It is memory nothing else claims, so we are not
# guessing at a safe address; and FireOS's own MediaTek ram_console driver
# reads that same region back after a reboot and publishes it as
# /proc/last_kmsg — verified holding 64,622 bytes of a previous boot. So a 6.1
# kernel that panics before userspace can still be read afterwards from
# FireOS, with no UART and without opening the case.
#
# Clear of the initramfs, which loads at 0x44000000 and is ~1MB.
RAMOOPS_CMDLINE = (
    "ramoops.mem_address=0x44400000 ramoops.mem_size=0x200000 "
    "ramoops.record_size=0x20000 ramoops.console_size=0x80000 "
    "ramoops.dump_oops=1"
)


# The partition holding the FireOS userspace this image is built beside,
# stamped onto its own cmdline so emOS mounts the right one. Mirrors
# SYSTEM_CMDLINE_KEY in controller/em_emos_build.py; test_agrees_with_mkboot
# pins the two together.
#
# Taken from the ENVIRONMENT rather than a seventh positional argument: the
# existing four-then-optional-two CLI is what build.sh and the parity test both
# drive, and appending to it makes the dtb and extra-cmdline slots mandatory to
# reach this one.
#
# Unset means no stamp, which is right for this tool: build.sh runs on a
# workstation against a file and cannot know which slot the reference came
# from. The wizard can, because it reads the partition by name off the device,
# so that is where the value normally comes from. An unstamped image falls back
# to the partition emOS hardcoded before this existed.
SYSTEM_CMDLINE_KEY = "emos.system="

# The board this image is built against, stamped onto its own cmdline so
# init.c can resolve which boards_*.c runtime to use. Mirrors
# BOARD_CMDLINE_KEY in controller/em_emos_build.py; the parity test in
# tests/test_emos_build.py pins the two together.
#
# Taken from EMOS_BOARD in the environment: build.sh has no opinion on
# the board because it is a workstation tool with no device to ask, so
# an explicit override is the only way it gets one. The controller's
# packer reads the DTB and falls back to BOARD_DEFAULT; the standalone
# tool here does the same and defaults to "biscuit" when the env var is
# unset and the DTB has no answer.
BOARD_CMDLINE_KEY = "emos.board="
BOARD_DEFAULT = "biscuit"

# FDT parsing, duplicated byte-for-byte from controller/em_emos_build.py.
# The parity test pins this against the controller's copy; two
# implementations of one binary format can only be known to agree by
# running both. Same trade as SYSTEM_CMDLINE_KEY above.
FDT_MAGIC       = 0xd00dfeed
FDT_BEGIN_NODE  = 1
FDT_END_NODE    = 2
FDT_PROP        = 3
FDT_NOP         = 4
FDT_END         = 9


def _fdt_compatible(dtb):
    if len(dtb) < 40 or struct.unpack(">I", dtb[:4])[0] != FDT_MAGIC:
        return ""
    # FDT header fields and tokens are big-endian on the wire. Use ">I"
    # to match what real devices emit; little-endian works for the magic
    # because 0xd00dfeed reads the same in both byte orders' first byte,
    # but every other field round-trips byte-swapped.
    off_struct, off_strings = struct.unpack(">2I", dtb[8:16])
    if off_struct >= len(dtb) or off_strings >= len(dtb):
        return ""

    depth = 0
    p = off_struct
    compatible = ""
    while p + 8 <= len(dtb):
        tok = struct.unpack(">I", dtb[p:p + 4])[0]
        p += 4
        if tok == FDT_END:
            break
        if tok == FDT_NOP:
            continue
        if tok == FDT_BEGIN_NODE:
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
            nend = dtb.find(b"\x00", off_strings + nameoff)
            if nend < 0:
                return ""
            name = dtb[off_strings + nameoff:nend].decode(
                "ascii", errors="replace")
            if name == "compatible" and depth == 1:
                if p + valen > len(dtb):
                    return ""
                entries = dtb[p:p + valen].split(b"\x00")
                entries = [e for e in entries if e]
                if entries:
                    compatible = entries[-1].decode("ascii", errors="replace")
            # Align from the start of the value, not from p + valen -- the
            # latter is already aligned for valen % 4 == 0 and would skip
            # the mandatory 4 bytes of padding the spec requires.
            p += (valen + 3) & ~3
            continue
        return ""
    return compatible


def reference_board_id(ref):
    if len(ref) < PAGE or ref[:8] != b"ANDROID!":
        return ""
    ksz = struct.unpack("<I", ref[8:12])[0]
    payload = ref[PAGE:PAGE + ksz][0x200:]
    i = payload.find(b"\xd0\x0d\xfe\xed")
    if i < 0:
        return ""
    dtb = payload[i:]
    name = _fdt_compatible(dtb)
    if name == "mediatek,mt8163-biscuit":
        return "biscuit"
    if name:
        return name.split(",")[-1]
    return ""


def stamp_cmdline_key(cmdline: bytes, key: str, value: str) -> bytes:
    """Set `key=value`, replacing any value already there.

    Not the same dedup as the ramoops block: that asks whether the whole string
    is already present, which is right for a fixed block and wrong for a key
    whose value changes. Rebuilding a p13 image for a p14 device would
    otherwise carry two stamps, and the kernel takes the LAST of a repeated
    parameter — so the image works and reads as whichever one you looked at.
    """
    kept = [tok for tok in cmdline.split() if not tok.startswith(key.encode())]
    kept.append(f"{key}{value}".encode())
    return b" ".join(kept)


def main():
    ref_p, z_p, rd_p, out_p = sys.argv[1:5]
    dtb_p = sys.argv[5] if len(sys.argv) > 5 else None
    extra = sys.argv[6] if len(sys.argv) > 6 else RAMOOPS_CMDLINE
    sys_part = os.environ.get("EMOS_SYSTEM_PART", "").strip()
    # EMOS_BOARD, when set, overrides the DTB sniff. The wizard has a board
    # in hand from the device; build.sh does not, and lets the DTB answer.
    # Empty (the default) means "let the packer decide".
    board_id = os.environ.get("EMOS_BOARD", "").strip()
    ref = open(ref_p, "rb").read()
    dtbs, hf = split_reference(ref)
    if dtb_p:
        # Ship ONE dtb, the one this board actually boots.
        #
        # The vendor image carries three and LK chooses; but amonet-k32 ships a
        # single blob, which implies LK may simply take the first. Ours is the
        # THIRD (identified by cpu@0-3 matching this quad-core board, and by the
        # dacmux pin groups present in the live tree) — so leaving all three in
        # risks booting dtb1, which has neither.
        dtbs = open(dtb_p, "rb").read()
    zimage = open(z_p, "rb").read()
    ramdisk = open(rd_p, "rb").read()

    cmdline = hf["cmdline"]
    # Appended only if it is not already there.
    #
    # The reference is normally a FireOS image, which carries none of this. But
    # rebuilding an emOS image FROM an emOS image — which is what an in-place
    # update does — hands us a cmdline that already ends in these parameters,
    # and appending blindly doubles them. Every rebuild would add another copy
    # until the 511-byte field overflowed and the build failed, on the third
    # pass. Found 2026-09-06 building 0.3 from Test Echo 2's own partition,
    # which is the first time anything has repacked an emOS image.
    if extra and extra.encode() not in cmdline:
        cmdline = cmdline + b" " + extra.encode()
    # After the ramoops block, so the stamps are last and most visible in a
    # dump. BOARD is stamped last so the order reads ramoops ... emos.system=
    # ... emos.board=.
    if sys_part:
        if not (sys_part.isdigit() and 1 <= int(sys_part) <= 127):
            raise SystemExit(
                f"EMOS_SYSTEM_PART must be an mmcblk0 partition number "
                f"(1-127), not {sys_part!r}")
        cmdline = stamp_cmdline_key(cmdline, SYSTEM_CMDLINE_KEY,
                                    f"/dev/block/mmcblk0p{int(sys_part)}")
    # Resolve the board: env override, then DTB sniff, then BOARD_DEFAULT.
    # The DTB sniff runs only when no override is set, because an explicit
    # EMOS_BOARD is the wizard saying "this device, not what the kernel
    # claims it is". A failed DTB sniff falls through to BOARD_DEFAULT,
    # which is the value init.c itself defaults to -- so an unstamped image
    # is consistent end-to-end.
    resolved_board = board_id or reference_board_id(ref) or BOARD_DEFAULT
    if not all(0x21 <= ord(c) <= 0x7e for c in resolved_board) or not resolved_board:
        raise SystemExit(
            f"the resolved board id must be non-empty printable ASCII, "
            f"not {resolved_board!r}")
    cmdline = stamp_cmdline_key(cmdline, BOARD_CMDLINE_KEY, resolved_board)
    if len(cmdline) > 511:
        raise SystemExit(f"cmdline too long for the 512-byte field: {len(cmdline)}")

    kernel = mtk_wrap(zimage + dtbs, b"KERNEL", hf.get("mtkhdr", b""))

    hdr = b"ANDROID!"
    hdr += struct.pack("<10I", len(kernel), hf["kaddr"],
                       len(ramdisk), hf["raddr"],
                       0, hf["saddr"], hf["tags"], PAGE, hf["hdrv"], hf["osv"])
    hdr += b"\0" * 16                       # product name, empty in the vendor image
    hdr += cmdline.ljust(512, b"\0")        # device cmdline + our ramoops params
    # The Android image id: SHA1 over each region followed by its length, in
    # kernel/ramdisk/second order. Stock carries a real one and reproducing it
    # is free, so there is no reason to ship zeroes and find out the hard way
    # whether anything downstream reads it.
    digest = hashlib.sha1()
    for region, size in ((kernel, len(kernel)), (ramdisk, len(ramdisk)), (b"", 0)):
        digest.update(region)
        digest.update(struct.pack("<I", size))
    hdr += digest.digest().ljust(32, b"\0")
    hdr += b"\0" * 1024                     # extra cmdline

    img = pad(hdr) + pad(kernel) + pad(ramdisk)
    open(out_p, "wb").write(img)

    print(f"reference : {ref_p} ({len(ref)} bytes)")
    print(f"  dtb               : {dtb_p or 'carried over from reference'} ({len(dtbs)} bytes)")
    print(f"  kernel load addr  : 0x{hf['kaddr']:08x}")
    print(f"  ramdisk addr      : 0x{hf['raddr']:08x}")
    print(f"  cmdline           : {cmdline.decode(errors='replace')}")
    print(f"  board             : {resolved_board}")
    print(f"built     : {out_p} ({len(img)} bytes)")
    print(f"  zImage            : {len(zimage)} bytes")
    print(f"  initramfs         : {len(ramdisk)} bytes")


if __name__ == "__main__":
    main()
