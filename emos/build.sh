#!/bin/bash
# Build an emOS boot image from a device's OWN boot partition.
#
# The reference image is required and is never shipped: mkboot.py reuses the
# kernel and DTBs out of it, so the artifact is reconstructed on the user's
# side from software already on their device. See README.md.
#
#   ./build.sh <reference boot_a_x.img> [output.img]
#
# Pull the reference off a rooted device first:
#   adb shell su -c 'dd if=/dev/block/mmcblk0p10' > boot_a_x.img
#
# and KEEP IT. It is the recovery image as well as the build input.
#
# EMOS_SYSTEM_PART names the partition holding the FireOS userspace this
# reference was read beside, and is stamped onto the image's cmdline so emOS
# mounts that one rather than assuming. It is optional here because this script
# runs against a FILE and cannot know which slot it came from -- unset, the
# image carries no stamp and emOS falls back to the partition it hardcoded
# before this existed. The provisioning wizard always sets it, because it reads
# the partition by name off the device.
#
#   EMOS_SYSTEM_PART=13 ./build.sh boot_a_x.img     # built beside system_a
#   EMOS_SYSTEM_PART=14 ./build.sh boot_a_x.img     # built beside system_b
#
# EMOS_BOARD picks which per-board runtime to link. Today: "biscuit"
# (default) and "radar". Adding a board is a new boards/<name>.{h,c} and
# a line in init.c's table -- see controller/CLAUDE.md for the
# interface. The reference image is the device's OWN boot partition;
# stamp `emos.board=<name>` onto it by exporting EMOS_BOARD before
# invoking the script. Default "biscuit" preserves today's behaviour.
set -e

REF=${1:?usage: build.sh <reference boot_a_x.img> [output.img]}
OUT=${2:-emos-boot.img}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

[ -f "$REF" ] || { echo "reference image not found: $REF" >&2; exit 1; }

# The init is a STATIC binary with no interpreter and no dynamic loader.
# That is not a size optimisation — three separate boots with a busybox
# `#!/bin/busybox sh` init produced no output whatsoever, which is
# indistinguishable from a kernel that never started. See README.md.
NDK=${NDK:-/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin}

# The init must match the REFERENCE kernel's architecture. FireOS 5 boots a
# 64-bit (AArch64) kernel and FireOS 6 a 32-bit ARM one — the same 3.18.19
# source, compiled both ways — and an init of the wrong architecture boots to
# nothing at all, with no output. So it is read out of the reference rather
# than assumed.
#
# The sniffer lives in the CONTROLLER's packer and is called from here rather
# than reimplemented. It used to be a second copy inline in this file, which is
# the shape everything else in this pair has a test against: the wizard and this
# script must agree about which architecture an image wants, and two copies of
# the rule can disagree without either one being wrong on its own. Note the
# import is by path — emos/ is outside the controller package, exactly as
# tests/test_emos_build.py loads mkboot.py from the other direction.
PACKER=$HERE/../controller/em_emos_build.py
[ -f "$PACKER" ] || {
    echo "cannot find the packer at $PACKER — it is where the architecture" >&2
    echo "sniffer lives, so run this from a full checkout rather than a copy" >&2
    echo "of emos/ on its own." >&2
    exit 1
}
ARCH=$(python3 - "$PACKER" "$REF" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_eb", sys.argv[1])
eb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eb)
print(eb.reference_kernel_arch(open(sys.argv[2], "rb").read()) or "unknown")
EOF
)
case "$ARCH" in
    arm64) TRIPLE=aarch64-linux-android21 ;;
    arm)   TRIPLE=armv7a-linux-androideabi21 ;;
    *)     echo "cannot tell the reference kernel's architecture — not building" >&2
           exit 1 ;;
esac
echo "reference kernel is $ARCH: building a matching init"
CC=${CC:-$NDK/$TRIPLE-clang}

# The board runtime is a separate translation unit so a second board
# adds boards_<name>.c without touching init.c. EMOS_BOARD selects
# which one; init.c picks its constants up via the matching header so
# the same compile rules apply to both translation units. The known
# boards are the two Amazon MT8163 reference designs emOS has been
# ported to; a new board is a new boards/<name>.{h,c} pair plus a
# stamp on the image's cmdline so the kernel hands it the right
# partition layout.
EMOS_BOARD=${EMOS_BOARD:-biscuit}
case "$EMOS_BOARD" in
    biscuit|radar) ;;
    *) echo "unknown EMOS_BOARD: $EMOS_BOARD (known: biscuit, radar)" >&2
       exit 1 ;;
esac
BOARD_SRC="$HERE/init/boards/boards_$EMOS_BOARD.c"
if [ ! -f "$BOARD_SRC" ]; then
    echo "board runtime not found at $BOARD_SRC" >&2
    exit 1
fi

if [ -x "$CC" ]; then
    "$CC" -static -O2 -Wall -DEMOS_BOARD="$EMOS_BOARD" -o "$WORK/init" \
        "$HERE/init/init.c" "$BOARD_SRC"
else
    echo "building init in the echomuse-compiler image ($CC not found)"
    docker run --rm -v "$HERE":/emos -v "$WORK":/out -w /emos echomuse-compiler \
        bash -lc "$NDK/$TRIPLE-clang -static -O2 -Wall \
            -DEMOS_BOARD=$EMOS_BOARD -o /out/init \
            init/init.c init/boards/boards_$EMOS_BOARD.c"
fi

# The ramdisk is init plus the empty mountpoints it needs. Everything else the
# system uses is mounted from the device's own /system at runtime, which is why
# no Amazon code is redistributed.
mkdir -p "$WORK/root"/{dev,proc,sys,system,data,etc,sbin}

# emOS's own wpa_supplicant, when one has been built. FireOS 6's cannot be
# used at all -- it is linked against Android IPC and aborts when /dev/binder
# is absent -- and ours (hostap 2.10, static ARM32, nl80211, internal crypto)
# needs nothing from Android. Optional on purpose: an image built without it
# falls back to /system/bin/wpa_supplicant, which is what the FireOS 5 fleet
# runs today. See tools/build-wpa-supplicant.sh.
SUPPLICANT=${SUPPLICANT:-$HERE/prebuilt/wpa_supplicant}
if [ -f "$SUPPLICANT" ]; then
    install -m 0755 "$SUPPLICANT" "$WORK/root/sbin/wpa_supplicant"
    echo "including wpa_supplicant ($(stat -c%s "$SUPPLICANT") bytes)"
fi

# wpa_cli and em-wifi, the console's way to set WiFi without the wizard. The
# console is the only channel left when the network is the broken thing, so
# these ride in the ramdisk rather than living on /data.
WPA_CLI=${WPA_CLI:-$HERE/prebuilt/wpa_cli}
if [ -f "$WPA_CLI" ]; then
    install -m 0755 "$WPA_CLI" "$WORK/root/sbin/wpa_cli"
    install -m 0755 "$HERE/device/em-wifi" "$WORK/root/sbin/em-wifi"
    echo "including wpa_cli and em-wifi"
fi

# busybox, when one has been built. A FireOS 6 /system ships toybox and NO
# busybox, so without this there is no udhcpc (hence no address), no ntpd, no
# syslogd/klogd, and no awk for em-wifi to parse a scan with. FireOS 5 keeps
# using /system's copy either way -- busybox_path() looks there first.
#
# The udhcpc symlink is explicit because init execs /sbin/udhcpc by path and
# busybox chooses its applet from argv[0]. init's applet stage would create it
# too, but DHCP must not depend on that stage having succeeded.
# See tools/build-busybox.sh.
BUSYBOX=${BUSYBOX:-$HERE/prebuilt/busybox}
if [ -f "$BUSYBOX" ]; then
    install -m 0755 "$BUSYBOX" "$WORK/root/sbin/busybox"
    ln -sf busybox "$WORK/root/sbin/udhcpc"
    echo "including busybox ($(stat -c%s "$BUSYBOX") bytes)"
fi

# Build identity, stamped in at build time rather than written at boot: it
# describes the IMAGE, so it must not be something a running system can drift
# from. /etc/os-release is the standard location and format, so ordinary
# tooling can read it without knowing anything about emOS. init's /etc symlink
# farm leaves it alone — symlink() into an existing name simply fails.
# emOS has its own tag namespace. Without --match, `git describe` picks up the
# nearest tag of ANY kind and stamps the image with a controller release
# number, which is worse than "unknown" because it looks plausible.
EMOS_VERSION=${EMOS_VERSION:-$(git -C "$HERE" describe --tags --match 'emos-v*' --dirty 2>/dev/null \
    || echo "0.1-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)")}
cat > "$WORK/root/etc/os-release" <<OSREL
NAME="emOS"
ID=emos
PRETTY_NAME="emOS $EMOS_VERSION"
VERSION="$EMOS_VERSION"
VERSION_ID="$EMOS_VERSION"
BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ)"
HOME_URL="https://github.com/wilbowes/EchoMuse"
OSREL
install -m 0755 "$WORK/init" "$WORK/root/init"
( cd "$WORK/root" && find . | cpio -o -H newc 2>/dev/null | gzip -9 ) > "$WORK/ramdisk.gz"

# LK gunzips an AArch64 Image, so the kernel must go back in COMPRESSED — the
# same bytes the reference image carries. Handing it an uncompressed Image
# silently doubles the image and does not boot.
EMOS_SYSTEM_PART="${EMOS_SYSTEM_PART:-}" python3 "$HERE/mkboot.py" "$REF" <(python3 - "$REF" <<'EOF'
import struct, sys
ref = open(sys.argv[1], "rb").read()
ksz = struct.unpack("<I", ref[8:12])[0]
kernel = ref[2048:2048 + ksz][0x200:]
i = kernel.find(b"\xd0\x0d\xfe\xed")          # first DTB ends the kernel
sys.stdout.buffer.write(kernel[:i])
EOF
) "$WORK/ramdisk.gz" "$OUT"

echo
echo "built $OUT for board=$EMOS_BOARD — flash with:"
case "$EMOS_BOARD" in
    radar) echo "  dd if=$OUT of=/dev/block/mmcblk0p10   (boot_a on radar)";;
    *)     echo "  dd if=$OUT of=/dev/block/mmcblk0p10   (boot_a_x on $EMOS_BOARD)";;
esac
echo "recover with:"
echo "  dd if=$REF of=/dev/block/mmcblk0p10"
