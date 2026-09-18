# board_profile — what an unlocked Echo is made of

A read-only hardware profile of a device EchoMuse does not support yet, for
deciding what supporting it would take. It collects what a new board has to
resolve by name (#541) and what emOS would need to boot on it.

```bash
device/tools/board_profile/profile.sh [-s <adb serial>] [-v] [output-dir]
```

Needs `adb` on the host and a rooted device booted into its own OS (TWRP also
works, but `/system` is not mounted there, so the audio config files are
missing). It takes under a minute and writes `<output-dir>/profile.txt`, the
audio config files it found, `/proc/config.gz` if the kernel exposes it, and a
`.tar.gz` of all of it.

## What it does not do

- **It writes nothing to the device** and starts or stops nothing. Partitions
  are read header-only (4KB), never whole.
- **It reads only named files.** A blind walk of sysfs finds files that block
  on read (`/sys/power/wakeup_count`) and hangs.
- **It does not read the WiFi configuration**, only the names of the files in
  `/data/misc/wifi`.

## Vendor files: hashes by default, contents with `-v`

Amazon's audio files (mixer paths, audio policy, and the DSP tuning under
`audio-algorithms`: AFE, beamformer coefficients, EQ, MBCL) are recorded by
path, size and sha256. That's enough to tell whether two devices share a
tuning. Their **contents** are included only with `-v`, because posting them
publicly redistributes vendor data. That's the same reason emOS ships an init
and not an image. Share a `-v` run privately.

## Redaction

The output is meant for a public issue. Properties and `/proc/idme` are
**allowlisted**, so anything not named stays out. The device serial is replaced
wherever it appears, MAC and IPv4 addresses are masked, and kernel log lines
mentioning SSIDs, association or keys are dropped. **Read `profile.txt` before
posting it.** Masking only catches what it knows to look for.

## Reading one

**The device tree is a list of what the board COULD carry, not what it does.**
biscuit's declares four `lp55231` LED drivers, a `bq24297` charger, an
`lp855x` backlight and two light sensors (one per production batch). On a
stock Dot only `is31fl3236` (the ring), `tsl2540` and `sym827` have a driver
bound. The `i2c` section's `driver=` shows which is which, and the kernel log's
probe lines confirm it.


| Section | Answers |
|---|---|
| properties, idme, device tree | which board this is. `device_type_id` identifies it where the device tree only names the SoC |
| kernel | arch (`uname -m`), modules, and whether the kernel config is readable |
| alsa, mixer | cards and PCM devices by name, every mixer control with its value, range and enum options |
| asoc | codecs, DAIs, and every DAPM widget's state and paths: the routing graph, read from the kernel |
| registers | codec and PMIC regmap dumps, and MediaTek's audio front end registers |
| open PCM streams, android audio services | what the vendor HAL has open right now and how AudioFlinger sees it |
| input devices | button names for `EVIOCGNAME`, never `eventN` numbers |
| leds, i2c, iio | ring driver, light sensor, anything else on the buses |
| boot images | slot layout, each image's cmdline, and whether its kernel is MTK-wrapped, gzip, arm64 or arm32 (`00 00 a0 e1` after the MTK header is a 32-bit zImage) |
| device tree nodes | every enabled node with a compatible string, from the whole tree pulled and decompiled (needs `dtc` on the host; without it the raw tree is kept) |
| thermal | every zone with its trip points, and every cooling device. biscuit has an audio cooler (`thermal-audio`), so the vendor throttles volume on heat |
| other hardware classes | hwmon, regulators, power supply, jack switches, RTC, watchdog, PWM |
| what actually probed | `/proc/interrupts` and `/proc/iomem` |
| debugfs | GPIO names and states, the regmap list, and eMMC wear from EXT_CSD 267–269 (`life_a=0x04` is 30–40% of rated life used) |
| unlock and slot signals | `expdb` starting `88 16 88 58` is amonet v2's LK. `misc`+864 is the A/B boot control block |
