#!/usr/bin/env sh
# profile.sh — read-only hardware profile of an unlocked Echo, for porting.
#
# Runs on the HOST and drives the device over adb. Collects what a new board
# needs resolving by name (#541): ALSA cards and mixer controls, input devices,
# LEDs, i2c/IIO sensors, WiFi driver, kernel arch, and the boot/partition
# layout emOS would have to live in. Output is a directory plus a .tar.gz meant
# for attaching to a PUBLIC issue, so it is redacted before it is written out.
#
#     porting/profile.sh [-s <adb serial>] [-v] [output-dir]
#
# -v keeps the CONTENTS of the vendor's audio files (Amazon's mixer paths,
# audio policy, and the DSP tuning under audio-algorithms). Off by default:
# the output is meant for a public issue, and posting those redistributes
# vendor data — the same reason emOS ships an init rather than an image.
# Without -v each is recorded by path, size and sha256, which is enough to
# tell two devices' tuning apart. Share a -v run privately.
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

SERIAL=""; VENDOR=0
while [ $# -gt 0 ]; do
  case "$1" in
    -s) SERIAL="$2"; shift 2 ;;
    -v) VENDOR=1; shift ;;
    *)  break ;;
  esac
done
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
  echo "vendor file contents: $([ $VENDOR = 1 ] && echo INCLUDED — do not post publicly || echo not included)"
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

# The whole device tree. No /sys/firmware/fdt on these kernels, so it comes
# as the /proc/device-tree directory (world-readable; ~1 minute over adb).
# With dtc on the host it becomes readable source plus a summary of every
# node with a compatible string; /chosen carries the cmdline, serial included,
# so the raw copy is only kept when there is no dts to redact instead.
sec "device tree nodes (enabled, by compatible)"
echo "pulling the device tree (about a minute)…" >&2
$ADB pull /proc/device-tree "$OUT/files/device-tree" >/dev/null 2>&1 || true
# Some boards publish Amazon's idme block in the device tree too: the Dot 3
# carries /idme with the serial, both MACs and mac_sec. idme is read above
# through an allowlist, so the tree's copy goes before anything is built from
# it, dts or raw. Found in a public attachment on #527, 2026-09-18.
rm -rf "$OUT/files/device-tree/idme" "$OUT/files/device-tree/chosen"
if command -v dtc >/dev/null && [ -d "$OUT/files/device-tree" ]; then
  dtc -q -I fs -O dts -o "$OUT/files/device-tree.dts" "$OUT/files/device-tree" 2>/dev/null || true
fi
if [ -s "$OUT/files/device-tree.dts" ]; then
  rm -rf "$OUT/files/device-tree"
  awk '
    /{$/ { d++; n[d]=$1; c[d]=""; st[d]=""; next }
    /compatible =/ { v=$0; sub(/.*compatible = /,"",v); gsub(/[";]/,"",v); gsub(/\\0/,",",v); c[d]=v }
    /status =/ { v=$0; sub(/.*status = /,"",v); gsub(/[";]/,"",v); st[d]=v }
    /^[ \t]*};/ { if (c[d] != "" && st[d] !~ /^disable/) { p=""; for (i=2;i<=d;i++) p=p "/" n[i]; printf "%-8s %-44s %s\n", (st[d]=="" ? "-" : st[d]), c[d], p } d-- }
  ' "$OUT/files/device-tree.dts" | sort -k2 >> "$P"
else
  echo "(no dtc on this host: raw tree kept in files/device-tree, without /chosen and /idme)" >> "$P"
fi

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
# Per control, for what the listing leaves out: enum options (> marks the
# current one) and integer ranges. The count comes from the listing, so the
# loop needs no grep or wc on the device.
nctl=$(grep -a '^Number of controls:' "$P" | sed -n '$s/[^0-9]//gp')
sec "mixer controls in detail (ranges and enum options)"
if [ -n "$nctl" ]; then
  dev "i=0; while [ \$i -lt $nctl ]; do echo \"\$i: \$(tinymix \$i)\"; i=\$((i+1)); done" >> "$P"
else
  echo "(no control count in the listing)" >> "$P"
fi

sec "asoc (cards, codecs, DAIs, and the DAPM routing graph)"
# Each DAPM widget file gives its power state and every path in and out of
# it: the route map, read from the kernel rather than rediscovered by hand.
run "cat /sys/kernel/debug/asoc/codecs /sys/kernel/debug/asoc/dais /sys/kernel/debug/asoc/platforms"
run "for w in /sys/kernel/debug/asoc/*/dapm/* /sys/kernel/debug/asoc/*/*/dapm/*; do [ -f \$w ] && { echo == \$w; cat \$w; }; done"
sec "codec and PMIC registers (regmap)"
run "for r in /sys/kernel/debug/regmap/*; do echo == \$r \$(cat \$r/name); cat \$r/registers; done"
sec "mediatek audio front end registers"
run "cat /sys/kernel/debug/mtksocaudio"
run "cat /sys/kernel/debug/mtksocanaaudio"
sec "open PCM streams (what is running right now)"
run "for f in /proc/asound/card*/pcm*/sub*/status /proc/asound/card*/pcm*/sub*/hw_params; do echo == \$f; cat \$f; done"
sec "android audio services"
run "dumpsys media.audio_flinger"
run "dumpsys media.audio_policy"
sec "audio configuration files (vendor)"
run "ls -l /system/etc /vendor/etc /system/vendor/etc /system/lib/hw /vendor/lib/hw /system/vendor/lib/hw"
run "ls /system/lib /vendor/lib /system/vendor/lib"
run "ls -lR /system/vendor/etc/audio-algorithms /vendor/etc/audio-algorithms /system/etc/audio-algorithms"
# Recorded by size and sha256 always; contents kept only with -v (see top).
if command -v sha256sum >/dev/null; then SHA="sha256sum"; else SHA="shasum -a 256"; fi
[ $VENDOR = 1 ] && mkdir -p "$OUT/files/vendor"
echo "size sha256 path" >> "$P"
for f in $( { dev "ls /system/etc/*mixer* /system/etc/*audio* /vendor/etc/*mixer* /vendor/etc/*audio* /system/vendor/etc/*mixer* /system/vendor/etc/*audio*"
              dev "ls /system/vendor/etc/audio-algorithms/* /vendor/etc/audio-algorithms/* /system/etc/audio-algorithms/*"; } \
           | grep -E '^/' | grep -E '\.(xml|conf|cfg|txt|sh|bin)$' | sort -u); do
  tmp="$OUT/files/.vendor.tmp"
  devb "cat $f" > "$tmp"
  echo "$(wc -c < "$tmp" | tr -d ' ') $($SHA "$tmp" | cut -d' ' -f1) $f" >> "$P"
  if [ $VENDOR = 1 ]; then mv "$tmp" "$OUT/files/vendor/$(echo "$f" | sed 's|^/||; s|/|_|g')"; else rm -f "$tmp"; fi
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

sec "thermal (zones with trip points, and every cooling device)"
# What throttles, at what temperature, and what the vendor wired to heat —
# on biscuit that includes an audio cooler (thermal-audio) and a budget.
run "for z in /sys/class/thermal/thermal_zone*; do echo == \$z \$(cat \$z/type) temp=\$(cat \$z/temp) mode=\$(cat \$z/mode); for t in \$z/trip_point_*_temp; do [ -e \$t ] && echo \"  \$t \$(cat \$t)\"; done; done"
run "for c in /sys/class/thermal/cooling_device*; do echo \$c \$(cat \$c/type) cur=\$(cat \$c/cur_state) max=\$(cat \$c/max_state); done"
sec "other hardware classes"
run "ls /sys/class"
run "for h in /sys/class/hwmon/*; do echo \$h \$(cat \$h/name); done"
run "for r in /sys/class/regulator/*; do echo \$r \$(cat \$r/name) \$(cat \$r/microvolts); done"
run "for p in /sys/class/power_supply/*; do echo \$p type=\$(cat \$p/type) online=\$(cat \$p/online); done"
run "for s in /sys/class/switch/*; do echo \$s \$(cat \$s/name) state=\$(cat \$s/state); done"
run "ls -l /sys/class/rtc /sys/class/watchdog /sys/class/pwm /sys/class/timed_output /sys/class/lirc /sys/class/rc /dev/watchdog"
run "cat /proc/misc"
sec "what actually probed (interrupts and memory map)"
# The device tree declares second-sourced parts that are not fitted (biscuit
# lists two light sensors, one per batch). A driver with a live interrupt, or
# a probe line in the kernel log below, is what is really on the board.
run "cat /proc/interrupts"
run "cat /proc/iomem"
sec "debugfs (listings, gpio, eMMC health)"
run "ls /sys/kernel/debug /sys/kernel/debug/regmap"
run "cat /sys/kernel/debug/gpio"
# EXT_CSD bytes 267-269: pre-EOL and the two life-time estimates (0x01 = 0-10%
# used ... 0x0b = beyond rated life). Parsed below from the hex dump.
extcsd=$(dev "cat /sys/kernel/debug/mmc0/mmc0:0001/ext_csd" | tr -cd '0-9a-fA-F')
if [ ${#extcsd} -ge 540 ]; then
  b() { echo "$extcsd" | cut -c$(($1 * 2 + 1))-$(($1 * 2 + 2)); }
  echo "eMMC ext_csd: pre_eol=0x$(b 267) life_a=0x$(b 268) life_b=0x$(b 269) rev=0x$(b 192)" >> "$P"
else
  echo "eMMC ext_csd: not readable" >> "$P"
fi
sec "android sensor list"
run "dumpsys sensorservice"

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
# Android 7's toybox ps lists only the current session without -A, and
# FireOS 5's toolbox ps treats -A as a name filter — so take whichever is
# longer. The first Dot 3 profile came back with three processes.
printf '$ ps -A || ps\n' >> "$P"
psA=$(dev "ps -A"); ps0=$(dev "ps")
if [ "$(echo "$psA" | wc -l)" -gt "$(echo "$ps0" | wc -l)" ]; then echo "$psA" >> "$P"; else echo "$ps0" >> "$P"; fi
sec "kernel log (filtered)"
dev "dmesg" | grep -v -iE 'ssid|associat|password|psk' >> "$P" || true

# ── Redaction ──────────────────────────────────────────────────────────────
# The serial is replaced wherever it appears — cmdline, dmesg, file contents.
serial=$(dev "getprop ro.serialno" | tr -cd 'A-Za-z0-9')
[ -n "$serial" ] || serial=$(dev "getprop ro.boot.serialno" | tr -cd 'A-Za-z0-9')
# The raw device tree is many small files; the serial is scrubbed from them too.
for f in "$P" "$OUT"/files/* $(find "$OUT/files/device-tree" -type f 2>/dev/null); do
  case "$f" in *.gz) continue ;; esac
  [ -f "$f" ] || continue
  LC_ALL=C sed -E \
    -e "${serial:+s/$serial/<serial>/g}" \
    -e 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<mac>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g' \
    -e 's/(serialno|serial|macaddr|mac_addr|btaddr|wifimac)=[^ ]*/\1=<redacted>/Ig' \
    "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done

# Last line of defence. Masking only catches what it knows to look for, and a
# board once published its whole idme block through the device tree. If the
# device's own serial survives anywhere, the archive is not made at all.
if [ -n "$serial" ] && LC_ALL=C grep -a -r -l "$serial" "$OUT" >/dev/null 2>&1; then
  echo "STOPPED: the device serial is still present in:" >&2
  LC_ALL=C grep -a -r -l "$serial" "$OUT" >&2
  echo "No archive was made. Please report this on #527 instead of posting the files." >&2
  exit 1
fi
tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo "Done: $OUT.tar.gz" >&2
echo "Read profile.txt before posting it — redaction masks what it knows to look for." >&2
