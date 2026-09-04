# emOS

A Linux distribution for the Amazon Echo Dot Gen 2 ("biscuit") that runs
EchoMuse with **no Amazon userspace at all** — no Android init, no
`system_server`, no `mediaserver`, no audio HAL.

It is a distribution in the ordinary sense: it does not include a kernel of its
own. It pairs the device's existing MediaTek 3.18 kernel with our own PID 1,
busybox, and bionic and tinyalsa mounted read-only from the device's `/system`.

**Status: 0.1, bench-proven, not a release.** One device, one day. A complete
voice turn has run on it — wake word scored on-device, Home Assistant pipeline,
spoken answer — along with WiFi, the 9-channel mic array, hardware AEC, the BLE
proxy, buttons, ambient light, jack detect and the LED ring. The known gaps are
listed at the bottom and none of them is a research problem.

## Why

EchoMuse's stated direction is to not depend on Amazon's software. The
Android-specific surface in the firmware is about twenty call sites, and the
assumption was always that the remaining dependency was thin. It was thinner
than that in one sense and much thicker in another:

- SETUP.md claimed Amazon's audio HAL was what started the I2S clock and that
  playback would hang without it. **That was reasoning, not measurement, and it
  is false.** Both directions clock with no HAL, no mediaserver and no
  framework.
- But the HAL *was* silently configuring the codec for us. Nothing in the
  firmware ever closed the codec's DAPM routes, because the HAL always got
  there first. On a device with no Android, both the microphones and the
  speaker are powered down. See `device/internal/bindings/codec`.

That second finding is the argument for keeping a device on emOS permanently
even while the fleet runs FireOS: it is the only instrument that makes this
class of dependency visible. No log, test or dashboard could show it.

## Build and install

```sh
# 1. Pull the device's own boot partition. KEEP THIS FILE — it is both the
#    build input and the recovery image.
adb shell su -c 'dd if=/dev/block/mmcblk0p10' > boot_a_x.img

# 2. Build. Reuses the kernel and DTBs out of your own image.
./build.sh boot_a_x.img emos-boot.img

# 3. Flash (TWRP, or over the network from a running emOS — see below).
dd if=emos-boot.img of=/dev/block/mmcblk0p10
```

**Recovery is a `dd` of the file from step 1 and takes about ten seconds.**
Prove recovery works before the first flash, not after. Doing that first is
what made a day of failed boots cheap rather than frightening.

Once emOS is running and on the network, flashing no longer needs TWRP:
`curl` the image onto the device and `dd` it, about thirty seconds a cycle.

## What the boot actually does

`init/init.c` is a static AArch64 binary, PID 1, ~500 lines. In order: mount
`proc`/`sys`/`devpts`; create every device node by hand; mount `/system`
read-only and `/data` read-write; symlink `/etc` and `/vendor`; link busybox's
applets; bring up the USB serial console; fork the WiFi stage; then supervise a
shell on the console.

Five things in there each failed *silently* before they were understood, and
every one of them is a comment in the source rather than folklore:

1. **The init must be a static C binary.** Three boots with a busybox shell
   init produced no output at all — no marker, nothing — which is
   indistinguishable from a kernel that never started, and cost most of a day.
   A static binary removes the interpreter, `PATH` and dynamic loader from
   between the kernel and the evidence. **The instrument was the bug**, and a
   broken instrument reads exactly like a broken subject.
2. **There is no devtmpfs.** Android's `/dev` is a tmpfs populated by `ueventd`
   from uevents, and we do not run `ueventd`, so nothing appears on its own.
   Every node is `mknod`'d from numbers read off a running device, never
   guessed.
3. **MUSB must be put in device mode** — write `1` to
   `/sys/devices/platform/mt_usb/cmode`. `cmode 2` is charging-only, and the
   gadget above it reports `DISCONNECTED` forever no matter how correctly it is
   configured.
4. **`f_acm`, not adbd.** adbd needs functionfs descriptors and Android's
   property service. `f_acm` needs no daemon: the kernel exposes `/dev/ttyGS0`
   and init puts a shell on it.
5. **`/etc` must be a symlink to `/system/etc`.** The kernel firmware loader
   searches `/etc/firmware` for the WiFi blob, so without it the combo chip
   powers on, reports success, and no `wlan0` is ever created. Nothing in the
   failure mentions `/etc`.

### WiFi

Amazon's own sequence, read off a running FireOS device rather than
reconstructed — but two steps appear in no `.rc` file on the device and were
each found the hard way. `wmt_loader` must run **first** (it registers the
stp/wmt/BT character devices; without it `6620_launcher` idles forever with
nothing to open), and `wpa_supplicant` does not associate on its own here — it
needs a `wpa_cli reassociate` nudge, which the supervisor issues until carrier
appears. `dhcpcd` is started only after association, because it is given `-K`
to match stock and so never retries a lease it failed to get before the link
was up.

Cold boot to on-the-network is about 32 seconds.

## Diagnostics

There is no UART without opening the case, and the LED ring is the kernel's
own `is31fl3236` boot animation, so it says nothing about our progress. Two
channels exist instead:

- **The progress trail.** init writes a marker at offset 0 of the cache
  partition, `/proc/version` at 512, and an append-only trail at 1024, flushed
  after every step. It is what turned "nothing happened" into
  "`ttyGS0` open failed errno=2" and then into a shell, in three boots.
- **`/proc/last_kmsg`.** There is no pstore on this kernel, but MediaTek's
  ram_console publishes the *previous* boot's kernel log here across a warm
  reset. It is how a spontaneous reboot was root-caused to a kernel crash in
  the audio IRQ handler. **Read it first after any unexplained reboot** — the
  next reboot destroys it.

## Licensing

Three layers, three different answers, and the design follows from them:

- **Amazon's kernel** — a boot image contains it, so redistributing one carries
  GPL-2.0 obligations. **We therefore distribute a builder, not an image.**
  `build.sh` takes the user's own boot partition as its reference and reuses
  that kernel and those DTBs, so the artifact is reconstructed on their side
  and emOS ships no Amazon code.
- **Amazon's `/system`** — bionic, the linker, tinyalsa, wpa_supplicant. No
  licence to redistribute. Mounting it at runtime on a device that already has
  it is a different act from shipping it.
- **Our code** — MIT, like the rest of the repo. busybox is GPL-2.0 and is the
  device's own copy, not ours.

Leaning on `/system` is legally clean but pins emOS to one FireOS build. A
self-contained ramdisk would need our own userspace — a static Go binary and
busybox, no bionic. Not legal advice; and never ship Amazon marks or branding.

## Known gaps

None of these is unsolved, only unbuilt:

- **The clock is wrong** (reads 2010) — no NTP. Every log timestamp is
  therefore uncorrelatable, which blocks most other observability work. busybox
  has `ntpd`. Fix this first.
- **`/proc/last_kmsg` is not captured at boot**, so a crash is lost on the
  following reboot unless someone is watching.
- **No clean shutdown and no `fsck`.** `/data` is mounted read-write and
  reboots do not sync, unmount or signal children.
- **PID 1 does not reap orphans** — it waits only on the console shell.
- **The EchoMuse server is not started by init**; it is launched by hand. Each
  capability is its own hand-coded stage with its own ad-hoc supervisor, which
  does not scale. A declarative service table would replace all of them.
- **Line out plays the left channel only.** The codec is symmetric and the
  firmware duplicates mono into both channels, so the jack's right pin is
  likely not driven by the headphone right driver on this board. The unused
  `LOL`/`LOR` line-out drivers are the untested lead.
- The speaker amp idles on and hisses. Gating it on idle is **not** the fix —
  toggling it clicks audibly, which is why the injected silence stream exists.

## What emOS is worth beyond the stunt

It is a known-good userspace that boots on this hardware, so any future
kernel can be tested with this exact initramfs and debugged interactively
rather than one bit per ten-minute cycle. It also removes the reasons for
several Android call sites in the firmware: the `stop <service>` sites exist
only to fight mediaserver for the audio path, and the jack drift reconciler
exists only because the HAL rewrites our mixer settings. Neither has anything
to fight here.
