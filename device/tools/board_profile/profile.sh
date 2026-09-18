#!/usr/bin/env sh
# profile.sh — read-only hardware profile of an unlocked Echo, for porting.
#
# Runs on the HOST and drives the device over adb. Collects what a new board
# needs resolving by name (#541): ALSA cards and mixer controls, input devices,
# LEDs, i2c/IIO sensors, WiFi driver, kernel arch, and the boot/partition
# layout emOS would have to live in. Output is a directory plus a .tar.gz meant
# for attaching to a PUBLIC issue, so it is redacted before it is written out.
#
#     device/tools/board_profile/profile.sh [-s <adb serial>] [output-dir]
#
# Rules this keeps, each learned on biscuit:
#   - Nothing is written to the device, nothing is started or stopped. Every
#     partition access is a read of a header, never a whole partition.
#   - Only NAMED files are read. Blind sysfs walks hit files that block on
#     read (/sys/power/wakeup_count) and hang the run.
#   - No head/tr/grep assumed on the device (stock FireOS 5 has neither head
#     nor tr); all text processing happens here on the host.
#   - Device commands contain no single quotes, because they travel inside
#     su -c '...'.
#   - Redaction is an allowlist where it can be (getprop, /proc/idme) and a
#     mask where it cannot (MACs, IPv4, the serial wherever it appears). No
#     wpa_supplicant contents, no SSIDs.
set -eu

SERIAL=""
if [ "${1:-}" = "-s" ]; then SERIAL="$2"; shift 2; fi
ADB="adb"
[ -n "$SERIAL" ] && ADB="adb -s $SERIAL"
command -v adb >/dev/null || { echo "adb not found on PATH" >&2; exit 1; }
# macOS has no timeout(1); without it a hung read hangs the run, so say so.
T=""
if command -v timeout >/dev/null; then T="timeout 30"; else
  echo "note: no timeout(1) on this host — a hung read will need Ctrl-C" >&2
fi

$ADB get-state >/dev/null 2>&1 || { echo "no device (adb get-state failed)" >&2; exit 1; }

# ── How to get root ────────────────────────────────────────────────────────
# TWRP and adb-root are already uid 0. Otherwise Magisk/SuperSU take su -c;
# AOSP's toolbox su takes su 0 <cmd>.
PFX=""; SFX=""; ROOT="none"
# Parse uid=0( rather than id -u: FireOS 5's toolbox id ignores -u.
isroot() { $ADB exec-out "$1" 2>/dev/null | grep -q 'uid=0('; }
if isroot "id"; then ROOT="uid0"
elif isroot "su -c id"; then ROOT="su -c"; PFX="su -c '"; SFX="'"
elif isroot "su 0 id"; then ROOT="su 0"; PFX="su 0 sh -c '"; SFX="'"
fi

# idme values are NUL-terminated, and one stray NUL makes grep call the
# whole profile binary.
dev()  { $T $ADB exec-out "$PFX$1$SFX" 2>&1 | tr -d '\r\000' || true; }
# Raw bytes. exec-out merges the device's stderr into stdout, so the device
# side silences it — dd's "1+0 records in" would otherwise land in the data.
devb() { $T $ADB exec-out "$PFX$1 2>/dev/null$SFX" 2>/dev/null || true; }

model=$(dev "getprop ro.product.device" | tr -cd 'A-Za-z0-9_.-')
[ -n "$model" ] || model="unknown"
OUT="${1:-./echomuse-profile-$model-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT/files"
P="$OUT/profile.txt"
: > "$P"

sec() { printf '\n===== %s\n' "$1" >> "$P"; }
run() { printf '$ %s\n' "$1" >> "$P"; dev "$1" >> "$P"; }

echo "Profiling $model into $OUT (root: $ROOT)…" >&2
{
  echo "EchoMuse board profile"
  echo "profile.sh format 1"
  echo "host date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "root: $ROOT"
} >> "$P"
[ "$ROOT" = "none" ] && echo "WARNING: no root — partitions, sysfs and mixer reads will be incomplete." >> "$P"

# ── Identity ───────────────────────────────────────────────────────────────
sec "mode"
run "getprop ro.twrp.version"
run "getprop ro.bootmode"
run "getenforce"

sec "properties (allowlisted)"
# Allowlist rather than denylist: a property we did not think of stays out.
dev "getprop" | grep -E '^\[(ro\.(product|build|board|hardware|arch|chipname|mediatek|boot\.(slot_suffix|hardware|selinux|bootreason|mode|verifiedbootstate|flash\.locked|veritymode))|ro\.(twrp|sf\.lcd_density|config\.low_ram|zygote|kernel\.android)|init\.svc\.)' \
  | grep -v -iE 'serial|mac|fingerprint.*user_[0-9]+' >> "$P" || true
# The fingerprint names the exact build, which is diagnostic and not personal.
run "getprop ro.build.fingerprint"

sec "idme (allowlisted fields)"
for f in board_id productid productid2 device_type_id bootmode dev_flags; do
  printf '%s: ' "$f" >> "$P"; dev "cat /proc/idme/$f" >> "$P"; echo >> "$P"
done

sec "device tree"
printf 'model: ' >> "$P"; dev "cat /proc/device-tree/model" | tr '\0' ' ' >> "$P"; echo >> "$P"
printf 'compatible: ' >> "$P"; dev "cat /proc/device-tree/compatible" | tr '\0' ' ' >> "$P"; echo >> "$P"

# ── Kernel and CPU ─────────────────────────────────────────────────────────
sec "kernel"
run "cat /proc/version"
run "uname -m"
run "cat /proc/cmdline"
run "cat /proc/modules"
# exec-out folds the device's stderr into stdout, so "No such file" arrives
# as content: keep the file only if it really is gzip.
devb "cat /proc/config.gz" > "$OUT/files/config.gz"
if [ "$(od -An -tx1 -N2 "$OUT/files/config.gz" | tr -d ' ')" != "1f8b" ]; then
  rm -f "$OUT/files/config.gz"; echo "(no /proc/config.gz)" >> "$P"
fi

sec "cpu and memory"
run "cat /proc/cpuinfo"
run "cat /sys/devices/system/cpu/online"
run "cat /sys/devices/system/cpu/possible"
run "cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
run "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies"
run "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
run "cat /proc/meminfo"
run "for z in /sys/class/thermal/thermal_zone*; do echo \$z \$(cat \$z/type); done"

# ── Audio ──────────────────────────────────────────────────────────────────
sec "alsa"
run "cat /proc/asound/cards"
run "cat /proc/asound/pcm"
run "cat /proc/asound/devices"
run "for i in /proc/asound/card*/pcm*/info; do echo == \$i; cat \$i; done"
run "ls -l /dev/snd"
sec "mixer (tinymix, read only)"
run "tinymix"
sec "audio configuration files"
run "ls -l /system/etc /vendor/etc /system/vendor/etc /system/lib/hw /vendor/lib/hw /system/vendor/lib/hw"
run "ls /system/lib /vendor/lib /system/vendor/lib"
for f in $(dev "ls /system/etc/*mixer* /system/etc/*audio* /vendor/etc/*mixer* /vendor/etc/*audio* /system/vendor/etc/*mixer* /system/vendor/etc/*audio*" \
           | grep -E '^/' | grep -E '\.(xml|conf|cfg|txt)$' | sort -u); do
  devb "cat $f" > "$OUT/files/$(echo "$f" | sed 's|^/||; s|/|_|g')"
done

# ── Buttons, LEDs, sensors ─────────────────────────────────────────────────
sec "input devices"
run "cat /proc/bus/input/devices"
sec "leds"
run "ls -l /sys/class/leds"
run "for l in /sys/class/leds/*; do echo \$l max=\$(cat \$l/max_brightness); done"
sec "i2c"
run "for d in /sys/bus/i2c/devices/*; do echo \$d name=\$(cat \$d/name) driver=\$(ls -l \$d/driver); done"
sec "iio"
run "for d in /sys/bus/iio/devices/*; do echo \$d name=\$(cat \$d/name); done"
sec "spi, platform drivers"
run "ls /sys/bus/spi/devices"
run "ls /sys/bus/platform/drivers"
sec "display and camera (Spot)"
run "ls -l /sys/class/graphics /sys/class/drm /sys/class/video4linux /sys/class/backlight"
run "cat /sys/class/graphics/fb0/virtual_size"
run "for v in /sys/class/video4linux/*; do echo \$v \$(cat \$v/name); done"

# ── Radios ─────────────────────────────────────────────────────────────────
sec "network interfaces (addresses masked)"
run "ls -l /sys/class/net"
run "for n in /sys/class/net/*; do echo \$n driver=\$(ls -l \$n/device/driver); done"
run "ls /system/etc/firmware /vendor/firmware /system/vendor/firmware /lib/firmware"
# Names of the files only: the conf holds networks and passwords.
run "ls -l /data/misc/wifi"
run "ls -l /dev/stpbt /dev/stpwmt /dev/wmtWifi"
run "ls -l /sys/class/bluetooth"
sec "usb gadget (the emOS console rides this)"
run "ls /sys/class/udc"
run "ls /config/usb_gadget /sys/class/android_usb"

# ── Storage and boot ───────────────────────────────────────────────────────
sec "storage"
run "cat /proc/partitions"
run "ls -l /dev/block/platform/*/by-name /dev/block/by-name /dev/block/platform/*/*/by-name"
run "cat /proc/mounts"
run "cat /sys/block/mmcblk0/device/name /sys/block/mmcblk0/device/type /sys/block/mmcblk0/size"

# Boot headers, parsed here. Resolved by NAME from the by-name map, since the
# numbering differs by board and even between TWRP and Android on one board.
sec "boot images (header only)"
byname=$(dev "ls -l /dev/block/platform/*/by-name /dev/block/by-name /dev/block/platform/*/*/by-name")
echo "$byname" | grep -E ' (boot|recovery|kernel)[_a-z0-9]* -> ' | sed -E 's/.* ([a-z_0-9]+) -> (.*)$/\1 \2/' | sort -u |
while read -r name node; do
  h="$OUT/files/.hdr"
  devb "dd if=$node bs=4096 count=1" > "$h"
  magic=$(dd if="$h" bs=1 count=8 2>/dev/null | tr -cd 'A-Za-z0-9!')
  echo "--- $name ($node): magic=${magic:-none}" >> "$P"
  if [ "$magic" = "ANDROID!" ]; then
    u32() { od -An -tu4 -j "$1" -N4 "$h" | tr -d ' '; }
    echo "kernel_size=$(u32 8) ramdisk_size=$(u32 16) second_size=$(u32 24) page_size=$(u32 36) header_version=$(u32 40)" >> "$P"
    printf 'cmdline: ' >> "$P"; dd if="$h" bs=1 skip=64 count=512 2>/dev/null | tr -d '\0' >> "$P"; echo >> "$P"
    # What the kernel payload starts with: an MTK wrapper (88 16 88 58) puts
    # the real image 512 bytes further on. gzip, ARM64 Image or zImage then
    # say which kernel arch the init has to match.
    pg=$(u32 36)
    printf 'kernel starts: ' >> "$P"; od -An -tx1 -j "$pg" -N4 "$h" >> "$P"
    printf 'after mtk hdr: ' >> "$P"; od -An -tx1 -j $((pg + 512)) -N8 "$h" >> "$P"
    printf 'arm64 magic @+56: ' >> "$P"; dd if="$h" bs=1 skip=$((pg + 512 + 56)) count=4 2>/dev/null | tr -cd 'A-Za-z0-9' >> "$P"; echo >> "$P"
  fi
  rm -f "$h"
done

sec "unlock and slot signals"
# expdb starting 88 16 88 58 is amonet v2's LK; misc+864 is the A/B BCB.
for p in expdb misc; do
  node=$(echo "$byname" | sed -nE "s/.* $p -> (.*)$/\1/p" | sed -n 1p)
  [ -n "$node" ] || { echo "$p: not in by-name map" >> "$P"; continue; }
  if [ "$p" = expdb ]; then
    printf 'expdb first bytes: ' >> "$P"; devb "dd if=$node bs=16 count=1" | od -An -tx1 -N4 >> "$P"
  else
    printf 'misc+864 (BCB): ' >> "$P"; devb "dd if=$node bs=16 skip=54 count=1" | od -An -tx1 >> "$P"
  fi
done

# ── What Android is running ────────────────────────────────────────────────
sec "processes"
run "ps"
sec "kernel log (filtered)"
dev "dmesg" | grep -v -iE 'ssid|associat|password|psk' >> "$P" || true

# ── Redaction ──────────────────────────────────────────────────────────────
# The serial is replaced wherever it appears — cmdline, dmesg, file contents.
serial=$(dev "getprop ro.serialno" | tr -cd 'A-Za-z0-9')
[ -n "$serial" ] || serial=$(dev "getprop ro.boot.serialno" | tr -cd 'A-Za-z0-9')
for f in "$P" "$OUT"/files/*; do
  case "$f" in *.gz) continue ;; esac
  [ -f "$f" ] || continue
  sed -E \
    -e "${serial:+s/$serial/<serial>/g}" \
    -e 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<mac>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g' \
    -e 's/(serialno|serial|macaddr|mac_addr|btaddr|wifimac)=[^ ]*/\1=<redacted>/Ig' \
    "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done

tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo "Done: $OUT.tar.gz" >&2
echo "Read profile.txt before posting it — redaction masks what it knows to look for." >&2
