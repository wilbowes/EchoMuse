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

Or use the **provisioning wizard**, which does all three from TWRP and never
boots Android: it escrows your boot partition (and hands you the file), sends
it to the controller to be repacked, flashes it and reads it back to check.
The wizard is the emOS path by default; `?flow=fireos` still runs the old
thirteen-step FireOS install.

### Releasing emOS

emOS has its **own tag namespace**, `emos-v*`. `build.sh` stamps the image
with `git describe --match 'emos-v*'`, so without those tags it stamps
whatever tag is nearest — a controller release number, which is worse than
"unknown" because it looks plausible. The namespace also keeps emOS out of the
firmware OTA's way: `_fetch_latest_release` selects a tag starting `v` with a
`server` asset, and `emos-v0.1` matches neither.

```sh
git tag -a --cleanup=verbatim emos-v0.1 -m "..."   # -a always; the annotation IS the notes
git push origin emos-v0.1
```

`emos-release.yml` compiles the init with the pinned NDK, asserts it is
aarch64 and static, runs the ring and password checks against the source being
published, and attaches **`init` and nothing else**.

**Only the init is published, and it cannot be otherwise.** A bootable image
contains the device's own kernel and device trees, so shipping one would mean
redistributing Amazon's code. The init is ours; the image is assembled from
the boot partition each user reads off their own device.

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


### Talking to the console: turn ECHO OFF

**A serial port opened with default termios has `ECHO` on, so everything the
device sends is echoed straight back into its own input.** The shell then
executes its own prompt —

```
/system/bin/sh: root@androi: not found
/system/bin/sh: 127: not found
```

— and every command returns 127. The console looks alive, echoes what you
type, and runs nothing.

This cost most of an evening on 2026-09-04 and produced a confident wrong
diagnosis (two shells fighting over one tty; there was one, and the second
`/system/bin/sh` was `start_server.sh`) plus a commit that had to be reverted.
`tty.setraw(fd)` or `stty raw -echo` fixes it instantly. Anything driving this
console — a script here, or the provisioning wizard over WebSerial, which has
no `stty` to remind you — must do it explicitly.

### The console password

The console is an unauthenticated root shell — anyone with a cable gets one,
and the WiFi PSK is in `/data/misc/wifi/wpa_supplicant.conf`. Stock FireOS is
better than us here, since adbd honours `ro.adb.secure`.

Set it fleet-wide from the dashboard (Config → Advanced → USB console). The
controller hashes it, pushes the record, and the firmware writes
`/data/local/etc/echomuse/console.pw`; **init reads that file and prompts before
handing over the shell**. Only the shell is gated — the boot trail and
everything else the device prints stay readable, so a dead device is still
diagnosable.

**It is a nod to security, not Fort Knox.** The record is on `/data`: delete the
file from TWRP and you are back in. It is an inconvenience for a casual
opportunist and nothing more, and it should not be hardened later into
something more complicated.

Hashing is still worth it, for a different asset: it does not protect the
DEVICE, it protects the PASSWORD, which the owner has probably reused somewhere
that matters. Salted SHA-256, iterated, written out by hand in `init.c` because
this is a static binary with no crypto library. `init/pwcheck.c` includes
`init.c` whole and prints a record for each of its vectors, leading with the
password that produced it, so the two implementations are compared rather than
assumed:

```sh
cd init && cc -O2 -o /tmp/pwcheck pwcheck.c && /tmp/pwcheck
```

CI runs that and recomputes every record with `hashlib`, so a drift between the
C and the Python fails a check rather than a login over USB. The password is in
the output for exactly that reason: the checker derives the expected hash from
`pwcheck`'s own inputs and needs no second copy of the table.

Two behaviours chosen so a mistake fails open rather than shut: a record that
cannot be parsed means NO password, and wrong answers slow down but never lock
out. Any lockout a power cycle clears is theatre, and one that survived a reboot
would only ever trap the owner.

### What the kernel cmdline actually contains

`/proc/cmdline` is not the boot image's cmdline: **LK appends its own
parameters after ours, including a DUPLICATE `androidboot.selinux=enforce`
that supersedes the `permissive` token the provisioning wizard patches in.**
LK also supplies `androidboot.hardware`, `androidboot.slot_suffix` and
`androidboot.serialno` — the last being where the firmware's serial fallback
gets it on a system with no property service.

Nothing under emOS reads any of them: there is no `/sys/fs/selinux` and no
SELinux line in `dmesg`. So the wizard's permissive patch is inert here, which
is why an emOS install does not need the boot patch at all.

### WiFi comes from /data, and wpa_supplicant makes its own entropy

`wpa_supplicant.conf` and `entropy.bin` both live in `/data/misc/wifi`, so
they survive a boot-partition write — which is what lets a device be
configured over adb on FireOS and then crossed to emOS. **`entropy.bin` is
created if absent**: deleted on EFF, the device rebooted and was back on WiFi
in 25 seconds with the file recreated. A device that never completed Alexa
setup is not stranded.

`wpa_cli -p /data/misc/wifi/sockets -i wlan0` works for `scan`,
`scan_results` (bssid, frequency, signal, flags), `get_capability key_mgmt`
and `save_config`. Note `get_capability key_mgmt` returns
`NONE IEEE8021X WPA-EAP WPA-PSK` — **no SAE, so this radio cannot join WPA3**,
and `save_config` needs `update_config=1` in the conf or it silently keeps
nothing.

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

The ring passes through three owners on the way up, and only the last is ours.
Knowing which one you are looking at is most of the diagnosis:

| what you see | who is driving it | what it means |
|---|---|---|
| solid dark blue | the bootloader | powered, before Linux. We have not confirmed whether this is the preloader or LK |
| full blue ring, one cyan segment orbiting | the **kernel**, via the `is31fl3236` driver's `boot_animation` | the kernel is up and **our init has not run**. It orbits until userspace claims the ring, so an orbit that never becomes a progress head means PID 1 never started, or died before its first instruction |
| the ring fading out, one cyan LED left at position 1 | emOS init | the handover: we have just taken the ring |
| blue arc growing behind a cyan head | emOS init | booting, and the head's position is how far |
| positions 12 and 1 lit and throbbing | emOS init | every stage done, waiting on the network — most of the boot |
| both sides filling to the top, then white, then fading | emOS init | up, on the network, ring handed back |
| red, stopped | emOS init | a stage failed, at the point it reached |
| solid amber | emOS init | rolling back to the known-good image |

### Positions

The ring is twelve LEDs at 30°, numbered here the way it physically sits:
**1 just left of 6 o'clock**, up the left side to **6 just left of 12
o'clock**, then **7 just right of 12 o'clock**, down to **12 just right of 6
o'clock**. So there is a GAP at dead bottom and dead top rather than an LED,
which is why the balanced pairs are the ones either side of each gap. The
kernel starts its orbit on physical index 0, which is position **2** — the
mapping constant is `LED_BOTTOM`, and getting it wrong rotates the whole
display by one LED including where the closing sweep meets.

### The handover

Ours is a continuation of the kernel's orbit rather than a different display,
and the palette is measured off the driver rather than eyeballed — `frame` is a
read/write attribute, so writing 1 to `boot_animation` on a running device
replays the orbit and each frame reads back exactly as displayed. Measured on
EFF, 2026-09-05: ground `0000ff` on **all twelve** LEDs, head `00ffff` cyan,
rising physical index, 109ms per step (min 100, max 120, n=13), 1.31s per
revolution. Sampling at 100ms first suggested exactly one step per sample,
which is aliasing rather than a measurement — the figures come from a second
pass at full speed against `/proc/uptime`.

The animation runs until userspace stops it, so WHEN we stop it is ours to
choose: `anim_claim` polls the position and takes the ring the instant the
orbit's head reaches **position 12**. Our next act is lighting position 1 —
the step the orbit would have taken anyway — so the motion carries straight on.
The blue ring then fades out from under that head, which is what buys the
contrast: against a lit ring the progress arc was two shades of blue from the
ground and did not read at all, so the ring is cleared and the tail **repaints**
the orbit's own blue as the head travels.

### The head

**It moves continuously, not in twelve jumps**, and that is the point of it
rather than a flourish. It eases towards the next stage and decelerates as it
approaches, never arriving until that stage actually completes; completing one
gives it a visible kick. It never claims progress that has not happened.

It is **steady while travelling and throbs only once it stops**, because those
say opposite things — movement is progress, the throb is waiting. "Stopped"
means visually stationary, not arithmetically: the ease decelerates to 1/256 of
a segment per tick, which changes no pixel.

The head is never a plain cross-fade across the two segments it straddles. That
halves its brightness at the midpoint, and at the bottom of a breath each half
falls *below* the tail — so the leading edge stops being the brightest thing on
the ring. The nearer LED takes the head at full and the further one a
proportional glow.

### The stages

| # | reached when | measured |
|---|---|---|
| 1 | `proc`/`sys`/`devpts` mounted, every device node created | 2210ms |
| 2 | `/system` mounted read-only | 2218ms |
| 3 | `e2fsck -p` on `/data` finished | 2254ms |
| 4 | `/data` mounted read-write | 2261ms |
| 5 | the `/etc` symlink farm is built | 2265ms |
| 6 | the previous boot's kernel log is preserved | 2277ms |
| 7 | busybox applets linked | 2849ms |
| 8 | the USB gadget is up | 2886ms |
| 9 | `/dev/ttyGS0` exists — the console is open | 3888ms |
| 10 | `wlan0` exists | 7738ms |
| 11 | associating | 7738ms |
| 12 | carrier and an address — the boot is confirmed | ~35366ms |

Those are real, off EFF on 2026-09-05 (`note()` and `netlog()` timestamp every
line from `CLOCK_MONOTONIC`). They are worth reading before changing anything
here, because the shape is not what anyone assumes: **`fsck` is 36ms**, stages
1-8 all land inside 676ms, and **association plus DHCP is 27.6 seconds — four
fifths of the whole boot**. The head therefore arrives at position 12 at 7.7s
and waits there for the rest, which is what the throbbing pair at 12 and 1 is
for. Pacing the ring by elapsed time instead was considered and is not needed
for that reason; it would also mean predicting durations, which this does not.

Stage 10 did not exist until 2026-09-05: the wlan0 and associating steps pinned
the same number, so one LED never lit. Note they still fire in the same
millisecond, so 10 is lit and superseded immediately.

### When a stage fails

**Only two stages can fail this way** — `led_fail()` has exactly two callers,
mounting `/system` (2) and mounting `/data` (4). The head turns red at the
point it reached and **everything stops**, including the throb: a device that
is stuck should look stuck.

Everything else fails by *hanging* rather than by reporting, and that looks
different: the head simply stays where it is. A hang before `/data` is mounted
is the one to know about, because the rollback counter lives in
`/data/emos/boot.state` and cannot be incremented until then — so a boot that
dies earlier than that is never counted, never rolls back, and needs recovery
over USB.

**Amber means a rollback is in progress** (see below). Cold boot to a working
voice assistant is about 35 seconds.

### Judging it without a device

`init/ringsim.c` includes `init.c` whole and drives the real animation
functions against a simulated clock, so the ring can be watched as text and its
invariants checked without flashing anything:

```sh
cd init && cc -O2 -o /tmp/ringsim ringsim.c -lm
/tmp/ringsim            # every frame, as a brightness ramp
/tmp/ringsim --check    # the invariants
/tmp/ringsim x 3        # what a failure at stage 3 looks like
```

Nothing is reimplemented there, so it cannot drift from what the device runs.
It checks that the ring is never still for 400ms, that the head never travels
backwards, that nothing lights ahead of it, and that the ring is handed back
dark. CI runs `--check` on every PR, so those invariants hold without anyone
remembering to look. Only the clean boot is asserted: the failure display ends
with the ring lit on purpose, so it fails the handed-back-dark check by design
and needs its own invariants before it can be asserted too. Note the display is gamma-corrected: rendered linearly the dark blue
trail reads as blank, which is a property of the ramp and not of the ring.

**Three constants in `init.c` are still guesses** and are gathered under "Four
numbers nobody has measured yet": the two blues, and which way round the index
runs. `ORBIT_PROBE` reads the kernel's own frames back out of `frame` — it is a
read/write attribute, so the driver hands back exactly what it is displaying —
and writes them to the boot trail, which gives the palette exactly rather than
by eye, plus the orbit's direction and period. One boot answers all three. The
bottom LED is settled: position 0 is the one just left of bottom centre, where
the ring's fill has always started.

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
- ~~Line out plays the left channel only.~~ **Fixed 2026-09-04.** A plug-out
  and plug-in cycle now moves audio between the external speaker and the
  internal driver in both directions with no interruption, verified against
  the controller rather than by ear: no `no mic frames` warning, no defensive
  `mic_start`, no escalation and no connection close — which together were the
  whole of #117's signature, and which FireOS produced within seconds of every
  unplug.
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
