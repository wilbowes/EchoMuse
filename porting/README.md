# Porting: profiling an Echo EchoMuse doesn't support yet

Two scripts that tell us what a new Echo is made of and how its stock firmware
drives it. Run them on an unlocked, rooted device, attach the results to the
device's issue, and we can judge what support would take.

They don't make the device work with EchoMuse. They collect what a port needs.

| Script | What it does | Changes anything? |
|---|---|---|
| `profile.sh` | Hardware inventory: device tree, audio chips and mixer, buttons, LEDs, sensors, thermal, storage, boot layout | **No.** Read-only |
| `probe.sh` | Watches the stock firmware play a sound, logs the buttons as you press them, and optionally records the mics | **Yes, briefly.** See below |

## Before you start

- **The device must be unlocked and rooted, and still on its stock firmware.**
  Unlocking is your responsibility and follows that device's own XDA thread.
  Nothing here unlocks anything.
- **`adb` on your computer**, with the device showing in `adb devices`.
- **Optional on your computer:** `python3` (analyses the mic recording) and
  `dtc` (turns the device tree into readable source; `apt install
  device-tree-compiler`, `brew install dtc`).
- **Don't run these on a device running EchoMuse.** Its server holds the audio
  devices. `probe.sh` refuses to run if it's there.

## Step 1: profile

```bash
porting/profile.sh                 # add -s <serial> with more than one device
```

About 2–3 minutes, most of it copying the device tree. It writes a folder and
a `.tar.gz` of it.

**Read `profile.txt` before you post it.** The serial number, MAC and IP
addresses are masked, and WiFi settings aren't read at all. But masking only
catches what it knows to look for.

**Amazon's audio files are recorded by name, size and fingerprint only.**
`-v` includes their contents: mixer paths, audio policy, and the DSP tuning.
Posting those publicly would redistribute Amazon's files, so share a `-v` run
privately, and only if we ask.

## Step 2: probe

```bash
porting/probe.sh                   # route + buttons
porting/probe.sh --mics --capture pcm_capture   # plus a mic recording
```

It lists what it's about to do and waits for `y`. Stages:

- **Route.** Presses volume up then volume down, so the device plays its own
  chime and ends at the volume it started at. While the chime plays it
  snapshots the mixer, codec registers and audio routing. The difference from
  idle is how the stock firmware makes sound.
- **Buttons.** The script names one button at a time: action, mute, volume
  up, volume down, then "any other". Press the one it names, once. A button
  the device doesn't have is skipped after 12 seconds. The result is a table
  of which button produces which key code, and from which input device.
  - The action button may wake Alexa. That's fine; she times out.
  - After mute, the script asks you to press it again, to unmute.
- **Mics (opt-in, `--mics`).** Stops the service holding the microphones
  (Android's media server on FireOS 5, one of Amazon's own services on the
  Dot 3) for about 15 seconds. It records 10 seconds and then starts that
  service again. **Stay quiet until told, then clap once at the front** (the
  action button side), **and once at the back**, when the script says.
  - Stock `tinycap` can't record every mic format; the Dot 2's packed 24-bit
    array defeats it. For those devices the recording needs `pcm_capture`,
    which we build and attach to the issue for you. Or build it yourself:
    `porting/pcm_capture/build.sh` (needs the `echomuse-compiler` Docker
    image, see `device/CLAUDE.md`).

**`mics.raw` is a recording of your room.** If anyone spoke during those 10
seconds, leave it out of the public post. `probe.txt` already holds the
analysis, which needs no audio.

## Things to be mindful of

- **A reboot undoes everything `probe.sh` changes.** If a stage is
  interrupted, the stopped service is restarted on the way out, Ctrl-C included.
  If sound or the mics seem off afterwards, reboot.
- **Don't run ALSA tools like `tinypcminfo`, `tinycap` or `tinyplay` yourself
  while the stock firmware is running.** Opening a device another service
  holds doesn't fail; it hangs until killed. The scripts avoid it for that
  reason.
- **Unplug anything in the headphone jack first.** It changes the audio
  routing, and the route stage would record the jack route rather than the
  speaker's.
- **The device tree lists what the board could carry, not what's fitted.**
  The Dot 2's declares four LED drivers, a battery charger and two light
  sensors that aren't there. The `i2c` section's `driver=` shows what actually
  started.
- **Neither script is a promise of support.** Some devices will turn out to
  need more than a board definition, and some won't be practical at all.

## What to do with the results

1. Post both `.tar.gz` files (minus `mics.raw` if in doubt) on the device's
   issue: #527 for the Echo Dot 3, #535 for the
   Echo 2. For anything else, the Echo Spot included, open a new issue.
2. We read them and say whether a port looks practical and what's missing.
3. If it does look practical, the next step is a **test build you run by
   hand**. You copy it to the device, start it yourself, and send the logs
   back. It isn't installed and never goes out as an update. Expect a few
   rounds of that before anything is ready to ship.

## Reading the output

**profile.txt**

| Section | Answers |
|---|---|
| properties, idme, device tree | which board this is. idme's `device_type_id` identifies it where the device tree only names the chip |
| device tree nodes | every enabled node with a compatible string |
| kernel | arch (`uname -m`), modules, and whether the kernel config is readable |
| alsa, mixer, mixer controls in detail | sound cards and streams by name, and every mixer control with its value, range and options |
| asoc | codecs, audio interfaces, and the routing graph (DAPM) |
| codec and PMIC registers, audio front end | register dumps of the audio chips |
| input devices, leds, i2c, iio | button names, LED drivers, sensors. `driver=` is what's actually fitted |
| thermal | every temperature zone with its trip points, and every cooling device |
| debugfs | GPIO names and states, and eMMC wear (`life_a=0x04` means 30–40% of rated life used) |
| boot images, unlock and slot signals | partition layout, each boot image's cmdline and kernel format (`00 00 a0 e1` after the MTK header means a 32-bit kernel), and amonet v2 signs |

**probe.txt**

| Section | Answers |
|---|---|
| route: playing | which playback stream opened and in what format; which mixer controls, registers and routing widgets the stock firmware changed to make sound |
| buttons: mapping | each button, the key code it produces, and the input device (by name) it came from |
| mics | the capture format; per channel: dead or live, noise floor, DC offset; duplicate channels; and which mic heard each clap first |

A clap marked **NOT A CLEAN CLAP** caught room noise rather than the clap, and
its order means nothing. A channel that's **silent** throughout is either
unconnected or a playback loopback. The Dot 2's Ch7 and Ch8 are loopbacks, and
carry sound only while the speaker plays.
