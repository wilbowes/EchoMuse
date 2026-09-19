# Radar (Amazon Echo 2) — first boot report

Booted 2026-09-17 from a custom emOS image built on the `radar`
branch. Reference image pulled from `/dev/block/mmcblk0p10`
(16 MB Android boot image, kernel `armv7l` 3.18.19). Build run:

```
EMOS_BOARD=radar EMOS_SYSTEM_PART=13 \
  emos/build.sh boot_a.img emos-boot.img
```

Image was sideloaded to `/data/local/tmp/`, then `dd`'d over the
boot partition from TWRP.

## What works

| Subsystem | Status |
|---|---|
| Kernel boots | ✅ `Linux em-G2A0P308816702JC 3.18.19-gecb8cb4-dirty armv7l` |
| emOS init | ✅ Static ARM ELF (`file init`: EABI5, NDK r21e 7075529) |
| Cmdline stamp | ✅ `emos.board=radar emos.system=/dev/block/mmcblk0p13` visible in `/proc/cmdline` |
| `/system` mount | ✅ system_a (mmcblk0p13) mounted ro |
| `/data` mount | ✅ userdata (mmcblk0p16) mounted rw |
| Hostname | ✅ `em-G2A0P308816702JC` |
| `/etc/os-release` | ✅ `PRETTY_NAME="emOS emos-v0.7-18-g4fb2565"` |
| `/run` tmpfs + net.log | ✅ Init's netlog mechanism runs |
| USB serial console | ✅ `/dev/ttyACM0` (CDC ACM, ID_MODEL=emOS, label `EchoMuse_emOS_G2A0P308816702JC`) |
| LED ring | ✅ `frame` sysfs at `0-003f` accepts writes; ring animates (alternating red/blue pulse, two-LED trail) |
| ALSA card | ✅ All 26 PCM devices created, including `pcmC0D24c` (TLV320AIC3101 8-mic capture) |
| WMT chrdevs | ✅ `/dev/wmtdetect` (154,0), `/dev/wmtWifi` (153,0), `/dev/stpbt` (192,0) created by init's `board_nodes()` |
| Buttons | ✅ gpio-keys (`event2`) created |

## What does not work

**WiFi.** The consys chip does not come up. The kernel logs:

```
[    4.279421] mtk_wcn_consys_hw_reg_ctrl(550): Read CONSYS chipId(0x00000000)
[    4.350000] wmt_core_stp_init(623): WMT-CORE: no hif info!
[    4.350055] opfunc_pwr_on(885): WMT-CORE: wmt_core_stp_init fail (-1)
```

and `mtk_wmtd` (kernel thread PID 179) loops forever trying to
power the chip on. The regulators that feed the consys are all
disabled:

```
vcn33_wifi: disabled
vcn33_bt:   disabled
vcn18:      disabled
vcn28:      disabled
```

### Why

The radar kernel (3.18.19 MT8163 conn_soc, build path
`/mnt/build/workspace/wnb0/VARIANT/user/label/FOS6/kernel/mediatek/mt8163/3.18_hl/`)
**does not implement the SET_STP_MODE ioctl on `/dev/wmtdetect`**.
The unknown-cmd log line is the giveaway:

```
[    2.278197] wmt_detect_unlocked_ioctl(140): unknown cmd (1074044933)
```

`1074044933 = 0x4004A005`, which decodes as `_IOR(0xA0, 5, 4)` —
the kernel sees our request but has no handler for it. biscuit's
kernel DID have that handler; radar's doesn't.

The radar kernel expects the consys bring-up to happen entirely
in-kernel: `mtk_wmtd` (kernel thread) does power on → read chipId →
init STP → power off, retrying forever. Userspace is expected to
either (a) supply patches via `wmt_loader` once the chip is alive
(but the chip never becomes alive because STP init fails on no
HIF info), or (b) hand control over to the Amazon `wmt_launcher`
which the kernel module doesn't even know about.

### What this means for the radar port

The `boards_radar.c` runtime is currently a straight copy of
`boards_biscuit.c`. On radar, the right runtime would either:

1. **Talk to `wmt_loader` (FireOS 6) instead of raw `/dev/wmtdetect`**
   — invoke `/vendor/bin/wmt_loader` (the Amazon-side binary that
   does the proper handshake), then wait for `mtk_wmtd` to bring
   the chip up. This requires copying the FireOS bring-up sequence
   without the Android property service it normally uses.

2. **Force-on the regulators from userspace** via `regulator_force_disable()`
   / `regulator_enable()` — but the sysfs path is read-only on
   this build (Permission denied on `/sys/class/regulator/.../state`),
   so the only path is an ioctl on the regulator device, which
   requires a consumer handle that emOS doesn't have.

3. **Patch the kernel** to expose SET_STP_MODE on `/dev/wmtdetect`
   — out of scope for a porting task.

None of these are quick. The first-boot landed in the state we
have because **the radar port compiles, builds, boots, mounts,
animates the LED, exposes ALSA, and runs the serial console —
but cannot bring up WiFi on this kernel**. That is the same shape
of limitation the first biscuit boot had in 2024, and the same
shape the radar port's eventual bring-up will need to clear.

## LED ring observations

The frame sysfs (`/sys/devices/soc/11007000.i2c/i2c-0/0-003f/frame`)
accepts writes — `init`'s `anim_claim` is in charge. Observed
animation on this boot:

```
LEDs 0-9 : 0000ff (blue) static
LED  10  : brightness pulsing 3200ff → 9e00ff → 5c00ff → 4700ff (purple-pink ramp)
LED  11  : same pulse as LED 10
```

Two-LED pulse on the trailing edge of the ring, with the first
ten LEDs at a static blue. This is the **emOS idle pattern**, not
the kernel's stock cyan orbit (which is what `boot_animation` had
been running before init cleared it). The pulse period looks
smoother than 109 ms but the exact cadence would need a longer
sample to measure precisely.

The LED layout numbers (`LED_BOTTOM`, `LED_DIR`, `ORBIT_STEP_MS`)
in `boards/radar.h` are still biscuit placeholders — they should
be measured against this animation by lining up the pulsing LEDs
with the physical LED ring.

## Reference files

- `/tmp/radar-build/boot_a.img` — pulled stock boot image (16 MB)
- `/tmp/radar-build/emos-boot.img` — built emOS radar image (7 MB)
- `/tmp/radar-build/boot.log` — first-boot serial console capture
- `/data/local/tmp/emos-boot.img` — staged image on device
- `/data/local/tmp/radar-dmesg.txt` — full dmesg from boot
