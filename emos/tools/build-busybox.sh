#!/bin/sh
# Build busybox for emOS: static ARM32, no Android in it at all.
#
# Run inside an armv7 Alpine container (see emos-release.yml); it fetches its
# own source, or uses busybox-$BB_VER/ if that is already beside it.
#
#   ./build-busybox.sh [output dir]
#
# WHY ALPINE AND NOT THE NDK, which builds everything else here.
#
# This binary is STATIC, and a static binary needs nothing but the kernel's
# syscall ABI -- not Android's linker, not bionic. So the libc it is built
# against is a free choice, and bionic is the wrong one: busybox does not
# target it. Building defconfig against the NDK needed eight source patches
# (utmpx.h, sys/kd.h, in6_ifreq defined twice, no SYSLOG_NAMES, union semun,
# missing SWAP_FLAG_*, then getusershell/ether_hostton/setbit/gethostid/
# explicit_bzero absent at link), twenty applets disabled to get that far, and
# clang 9 SEGFAULTS compiling hush.c. Against musl the same defconfig builds
# first try with zero patches and zero disables. Measured 2026-09-14.
#
# The NDK is still right for the firmware, which is cgo against Android's
# tinyalsa and runs under Android on FireOS 5. It is not right for this.
#
# Alpine is the toolchain because it is a distribution's own gcc and musl,
# built and signed by a project rather than by one maintainer's convenience
# image -- the same reason the hostap pin is cross-checked against a distro's
# published sum rather than against one download of ours.
#
# It is an armv7 container running under binfmt/qemu, so this is a NATIVE
# build, not a cross one. That costs about 8 minutes and buys a toolchain with
# no cross-compilation surface to get wrong.
set -e

OUT=${1:-$(cd "$(dirname "$0")/.." && pwd)/prebuilt}

# Loud rather than subtly wrong: a cross-built or x86 binary would pass most of
# the checks below and fail on the device.
[ "$(uname -m)" = armv7l ] || {
    echo "build-busybox.sh must run in an armv7 container (uname -m says" >&2
    echo "$(uname -m)). See emos-release.yml." >&2
    exit 1
}

# The source, PINNED by sha512 -- and the value is ALPINE's published sum for
# the same tarball, cross-checked rather than taken from one download of ours.
# Same rule as the hostap pin in build-wpa-supplicant.sh, and the same reason:
# the thing an auditor of a published artifact most needs to know is what it
# was built from.
BB_VER=1.38.0
BB_SHA512=ba3866f0a9ceabafe8c4d43afaf031bc5554742903b3d08e59a8592351dc09c6e091bb0cd2852b3ed1e79e841de871bac4dc48d9bb1cef9309c3f8495aba0d00

apk add --no-cache build-base linux-headers perl diffutils >/dev/null

# The TARBALL is fetched whether or not the source tree is already unpacked.
#
# It is not just the build input: busybox is GPL-2.0, so this exact tarball is
# the corresponding source we are obliged to distribute beside the binary, and
# it is published as a release asset. Keying the download off the source
# DIRECTORY -- as this did -- meant a pre-seeded tree produced a binary with no
# source to ship with it, which is the one failure here that is silent.
BB_TARBALL=$PWD/busybox-$BB_VER.tar.bz2
if [ ! -f "$BB_TARBALL" ]; then
    echo "fetching busybox $BB_VER"
    wget -q -O "$BB_TARBALL" \
        "https://busybox.net/downloads/busybox-$BB_VER.tar.bz2"
fi
# Checked EVERY run, not only after a download: a cached or pre-seeded tarball
# has had no less opportunity to be wrong than a fresh one.
echo "$BB_SHA512  $BB_TARBALL" | sha512sum -c - >/dev/null || {
    echo "busybox tarball does not match its pin -- refusing" >&2
    exit 1
}
[ -d "busybox-$BB_VER" ] || tar xjf "$BB_TARBALL"

cd "busybox-$BB_VER"

# Reproducible: without this the banner carries a wall-clock timestamp and no
# two builds agree, which makes the published binary unverifiable. Measured
# byte-identical across separate build directories with it set.
export SOURCE_DATE_EPOCH=1600000000
export KBUILD_BUILD_TIMESTAMP="@1600000000"

make defconfig >/dev/null

off() {
    for s in "$@"; do
        sed -i "s|^CONFIG_$s=y\$|# CONFIG_$s is not set|" .config
    done
}

# The ONLY thing trimmed from defconfig, and it is a posture choice rather than
# a build one: none of these is ever invoked by emOS, and each one is a
# listening socket on a device whose whole job is to be quiet. The project
# already declined to run NTP as a server for the same reason.
#
# Nothing else is removed. In particular the applets Android also ships are
# KEPT: init.c calls `busybox ifconfig` and `busybox route` BY NAME for the
# udhcpc lease script, and `busybox syslogd`/`klogd` for logging, so compiling
# them out breaks the DHCP path this binary exists to provide. Shadowing is a
# PATH question, not a build question, and init.c's applet stage already
# answers it per device by linking only names /system does not already have.
off TELNETD HTTPD INETD FTPD TFTPD DNSD UDHCPD DHCPRELAY TCPSVD UDPSVD FAKEIDENTD
yes "" | make oldconfig >/dev/null 2>&1

# -no-pie for ET_EXEC. gcc defaults to PIE, and -static then yields a static
# PIE, which this 3.18 kernel would have to self-relocate; ET_EXEC is the shape
# already proven on these devices, and it is 3KB smaller.
make CONFIG_STATIC=y CONFIG_EXTRA_CFLAGS="-no-pie" CONFIG_EXTRA_LDFLAGS="-no-pie" \
     -j"$(nproc)" busybox >/dev/null

# ---- Verification. Each check is something that fails SILENTLY on hardware.

# A dynamically linked binary finds no loader under emOS and dies with nothing
# useful said; the DHCP stage would simply never produce an address.
readelf -l busybox | grep -q INTERP && {
    echo "busybox is dynamically linked -- refusing" >&2; exit 1; }
readelf -h busybox | grep -q "Type:.*EXEC" || {
    echo "busybox is not ET_EXEC -- refusing" >&2; exit 1; }
readelf -h busybox | grep -q "Machine:.*ARM" || {
    echo "busybox is not an ARM binary -- refusing" >&2; exit 1; }

# The applets emOS actually invokes, as opposed to the ones it merely offers.
# init.c needs the first four to bring up the network and the log; em-wifi
# needs the rest, and a missing one there reads as "no networks found" on a
# device whose radio is fine.
./busybox --list > /tmp/bb-applets
for a in udhcpc ntpd syslogd klogd ifconfig route ip awk sed cut head sort \
         pgrep killall vi tail less find grep; do
    grep -qx "$a" /tmp/bb-applets || {
        echo "applet $a is missing -- refusing" >&2; exit 1; }
done

# And the daemons really are gone: `make oldconfig` can re-enable a symbol that
# something else selects, silently.
for d in telnetd httpd inetd ftpd tftpd dnsd udhcpd dhcprelay tcpsvd udpsvd \
         fakeidentd; do
    grep -qx "$d" /tmp/bb-applets && {
        echo "listening daemon $d is still built in -- refusing" >&2; exit 1; }
done

# PROVE the binary came from the source we are about to publish.
#
# The tarball is only sufficient as "complete corresponding source" because we
# do not patch busybox -- we only configure it. That is a claim about the build,
# and claims rot: the supplicant build beside this one rewrites its sources with
# inline python, so a future maintainer has a worked example of patching that
# no keyword grep would catch.
#
# So this checks the OUTCOME instead of the method. Extract the published
# tarball again and diff it against the tree we built. Files only in the build
# tree are generated (.config, .o, the binary) and are expected; a MODIFIED or
# MISSING upstream file means the tarball is no longer the source of this
# binary, and publishing it would be a false statement. Measured against a
# fully built tree: zero differences.
pristine=$(mktemp -d)
tar xjf "$BB_TARBALL" -C "$pristine"
changed=$(diff -rq "$pristine/busybox-$BB_VER" "$PWD" 2>&1 \
          | grep -v "^Only in $PWD" || true)
rm -rf "$pristine"
if [ -n "$changed" ]; then
    echo "the build tree differs from the published tarball -- refusing," >&2
    echo "because the tarball alone would no longer be the corresponding" >&2
    echo "source for this binary. Publish the patches too, or drop them:" >&2
    echo "$changed" >&2
    exit 1
fi

mkdir -p "$OUT"
install -m 0755 busybox "$OUT/busybox"

# The SOURCE ships beside the binary, and that is the compliance mechanism
# rather than a courtesy.
#
# busybox is GPL-2.0, the only licence in emOS that obliges us to offer source.
# A link to busybox.net would not do it: §3(a) wants the source to accompany
# the binary, and pointing at a third party's server means our compliance
# depends on them not reorganising. We have already downloaded and verified
# this exact tarball, so publishing it costs a copy.
#
# This is the UNMODIFIED upstream tarball -- nothing here patches busybox, only
# configures it -- so together with this script (which is the "scripts used to
# control compilation" §3 also asks for, and is in the public repo) it is the
# complete corresponding source.
install -m 0644 "$BB_TARBALL" "$OUT/busybox-$BB_VER.tar.bz2"
# §1 wants the licence text to travel with the program. It is in the tarball
# too, but a reader should not have to unpack 2.7MB to find out the terms.
install -m 0644 LICENSE "$OUT/busybox-LICENSE"
# Only the BINARY goes in the payload bundle the controller downloads. init
# execs /sbin/udhcpc by path and busybox picks its applet from argv[0], so the
# packer writes that name as a symlink into the ramdisk rather than shipping a
# second copy of a 1MB binary. Source and licence are release assets, not
# payload: a controller building an image has no use for 2.7MB of tarball.
echo
echo "built into $OUT:"
printf '  %-28s %8d bytes  %s…  %d applets\n' busybox \
    "$(stat -c%s "$OUT/busybox")" \
    "$(sha256sum "$OUT/busybox" | cut -c1-16)" \
    "$(wc -l < /tmp/bb-applets)"
for f in "busybox-$BB_VER.tar.bz2" busybox-LICENSE; do
    printf '  %-28s %8d bytes  (GPL-2.0 corresponding source)\n' \
        "$f" "$(stat -c%s "$OUT/$f")"
done
