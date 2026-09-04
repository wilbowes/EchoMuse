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
read-only and `/data` read-write; build the `/etc` symlink farm; link busybox's
applets; preserve the previous boot's kernel log; bring up the USB serial
console; then supervise the service table below. `/data` is checked with
`e2fsck -p` before it is mounted.

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
5. **`/etc` must resolve to `/system/etc`.** The kernel firmware loader
   searches `/etc/firmware` for the WiFi blob, so without it the combo chip
   powers on, reports success, and no `wlan0` is ever created. Nothing in the
   failure mentions `/etc`. It is a real DIRECTORY of symlinks rather than a
   symlink to `/system/etc`, because `/system` is read-only and a symlink
   would mean the device could never have a writable `resolv.conf`.

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

There is no UART without opening the case. Three channels exist instead — and
the LED ring is the first of them, since init takes it off the kernel's own
`is31fl3236` boot animation and drives it as a progress bar (above):

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

## How it boots, and what the ring tells you

Twelve stages, twelve LEDs. The ring is claimed before the first step that can
fail, fills blue one LED per stage, goes fully green when the network is up and
then fades out — handing the LEDs to the firmware, so anything shown afterwards
is EchoMuse's to explain. **A stage that fails leaves the ring RED at the point
it reached**, so a device that dies says where without a cable.

**Amber means a rollback is in progress** (see below). Cold boot to a working
voice assistant is about 35 seconds.

## Services

PID 1 keeps a table and one supervisor loop that reaps every child (orphans
reparent to init and nobody else collects them), respawns with backoff, and on
`SIGTERM` stops services, syncs and remounts `/data` read-only before resetting.
**`kill -TERM 1` is the clean reboot.**

- A run shorter than `FAST_EXIT` counts as a failure and backs off 2→60s; a
  longer one resets the count. It never gives up permanently — the causes here
  are usually transient, and a device that has silently stopped trying is worse
  than one still retrying slowly.
- `req` means "must exist or give up, reported once"; `after` means "wait for
  this and keep waiting". EchoMuse uses both: it is absent on a device where it
  is not installed, and it waits on `/run/net-up` so the boot ring has finished
  before the firmware claims the same twelve LEDs.
- EchoMuse is started through `start_server.sh`, not directly, because that
  script owns the A/B slot symlink and fast-exit backoff the OTA system depends
  on. Starting the binary would silently bypass firmware rollback.

## Logs, crashes and storage

The device runs for years on eMMC that cannot be replaced, so **routine logging
never touches flash**:

| what | where | flash cost |
|---|---|---|
| live syslog + kernel log | `/run` tmpfs | none |
| log at shutdown | `/data/emos/messages.last` | one bounded write |
| previous boot's kernel log | `/data/emos/last_kmsg.prev`, `.1`, `.2` | one per boot |

`last_kmsg` is **rotated three deep** on purpose: keeping one copy meant a
device that reboot-loops overwrote the crash that started it with the boring
ones that followed — the exact case it exists for.

Note busybox's own `-C` RAM ring does NOT work here: it needs System V shared
memory this kernel lacks, and the failure is silent (`logread` reports
"can't find syslogd buffer"), leaving no logging at all. tmpfs gives the same
property and does work.

## Rollback

init keeps the running image at `/data/emos/boot-good.img` and counts boots
that were never confirmed. **A boot is confirmed when the network comes up** —
reachability is the property that makes a device fixable without hands on it.
After three unconfirmed boots init restores the known-good image, shows an
amber ring and reboots.

This is deliberately NOT the bootloader's A/B. biscuit has a real second slot
(`boot_b_x`, `mmcblk0p11`) but LK chooses between them and we have not reverse
engineered how; the standing guess is three tries per slot and then a soft
brick, which is plausible and **untested**. Building on that guess risks a
device booting something we did not choose.

**The limit, plainly: an image that fails before init runs executes none of
this and still needs recovery over USB.** The byte-exact packer and the
flash verification below exist to make that rare.

## Security posture

**Nothing listens.** Zero TCP and zero UDP listening sockets — that is the
primary control, and it is the property to re-check whenever a daemon is added.
Stock FireOS is also zero, so this is parity to preserve rather than a win.

The packet filter is the second layer: outbound-only, `INPUT`/`FORWARD` DROP on
both IPv4 and IPv6, applied before the interface comes up and with every ACCEPT
installed before the policy flips. Four exceptions, each verified on hardware —
loopback, `ESTABLISHED,RELATED`, DHCP offers (broadcast, so conntrack cannot
help), and **mDNS responses, without which the device boots perfectly and never
finds its controller**. ICMP is allowed deliberately: not meaningful attack
surface, and ping is the cheapest liveness check there is.

**No SELinux policy and no unprivileged service user, deliberately.** The server
needs root for what it does — codec mixer controls, `/dev/snd`, raw HCI on
`/dev/stpbt`, i2c LED sysfs, GPIO, evdev, CPU governor, and reflashing itself
for OTA — so dropping privileges would mean granting almost all of it back
through another mechanism. A policy for a single-purpose device running one
process would mostly encode "our process may do everything" at permanent
maintenance cost, and helps against neither realistic threat: a compromised
controller already has a root shell by design, and physical access means
reflashing.

Remote shell is the controller's outbound-dialled `/shell/{device_id}` plane,
not sshd. If someone genuinely needs sshd it is dropbear, per-device opt-in,
off by default — not a listener every device carries.

## Flashing safely

**A `dd` to the boot partition can complete at page-cache speed and be lost on
the next reboot.** One flash reported 222MB/s; real writes to this eMMC run
2.5–9MB/s. Verifying by reading back *from the same cache* passes and proves
nothing — the device then boots the old image. So:

```sh
busybox dd if=new.img of=/dev/block/mmcblk0p10 bs=2048 conv=fsync
sync; echo 3 > /proc/sys/vm/drop_caches       # BEFORE reading back
busybox dd if=/dev/block/mmcblk0p10 bs=2048 count=<blocks> | busybox md5sum
```

Toolbox `dd` rejects `conv=fsync`; use busybox. And "the device is reachable"
is not proof it rebooted — compare uptime or a build fingerprint.

## Known gaps

- **The clock is wrong** (reads 2010), so every timestamp is uncorrelatable.
  `ntpd` cannot use a hostname because **bionic resolves through Android's
  property service, not `/etc/resolv.conf`** — no bionic-linked binary has DNS
  here, though Go binaries are fine since Go carries its own resolver. Pointed
  at the gateway, which does not serve NTP on the network tested. **The agreed
  direction is for the controller to tell the device the time**, over the
  authenticated outbound link it already trusts: no DNS, no listener, no
  external dependency.
- **Line out plays the left channel only.** The codec is symmetric and the
  firmware duplicates mono into both channels, so the jack's right pin is
  probably not driven by the headphone right driver on this board. The unused
  `LOL`/`LOR` line-out drivers are the untested lead.
- The speaker amp idles on and hisses. Gating it on idle is **not** the fix —
  toggling it clicks audibly, which is why the injected silence stream exists.
- Hardware is resolved by fixed major/minor numbers, against the project's own
  "resolve by name, not number" rule. Fine for biscuit, wrong for a second
  board.
- The boot trail is a fixed-size buffer rewritten in place, so a shorter trail
  leaves the tail of the previous boot's behind and can be misread.

## What emOS is worth beyond the stunt

It is a known-good userspace that boots on this hardware, so any future
kernel can be tested with this exact initramfs and debugged interactively
rather than one bit per ten-minute cycle. It also removes the reasons for
several Android call sites in the firmware: the `stop <service>` sites exist
only to fight mediaserver for the audio path, and the jack drift reconciler
exists only because the HAL rewrites our mixer settings. Neither has anything
to fight here.
