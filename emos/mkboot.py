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
import sys

MTK_MAGIC = 0x58881688
PAGE = 2048


def mtk_wrap(payload: bytes, name: bytes) -> bytes:
    """Prepend the 0x200-byte MediaTek header LK validates before jumping."""
    hdr = struct.pack("<II", MTK_MAGIC, len(payload))
    hdr += name.ljust(32, b"\0")
    # Padded with 0x00, READ OFF THE DEVICE'S OWN IMAGE. An earlier version of
    # this script padded with 0xff on the strength of a note that turned out to
    # be wrong; round-tripping stock through this packer put 472 differing
    # bytes inside the very header LK validates before it jumps.
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
    return payload[i:], dict(kaddr=kaddr, raddr=raddr, saddr=saddr,
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


def main():
    ref_p, z_p, rd_p, out_p = sys.argv[1:5]
    dtb_p = sys.argv[5] if len(sys.argv) > 5 else None
    extra = sys.argv[6] if len(sys.argv) > 6 else RAMOOPS_CMDLINE
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
    if extra:
        cmdline = cmdline + b" " + extra.encode()
    if len(cmdline) > 511:
        raise SystemExit(f"cmdline too long for the 512-byte field: {len(cmdline)}")

    kernel = mtk_wrap(zimage + dtbs, b"KERNEL")

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
    print(f"built     : {out_p} ({len(img)} bytes)")
    print(f"  zImage            : {len(zimage)} bytes")
    print(f"  initramfs         : {len(ramdisk)} bytes")


if __name__ == "__main__":
    main()
