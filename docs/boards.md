# Boards: how the firmware finds its hardware

The firmware is one binary. At start-up it works out which board it is on,
looks up where that board's parts are **by name**, and opens what it found.
Adding a board means describing it in one file; the drivers stay as they are.

Today only the Echo Dot 2nd gen (`biscuit`) is described. This page is for
anyone trying the firmware on something else.

Related reading: [`porting/README.md`](../porting/README.md) for the scripts
that profile an unsupported Echo, and
[device-controller-interface.md](device-controller-interface.md) for the wire
protocol a board has to speak.

## The aim

- **One binary for every board**, on FireOS and on emOS. emOS is where every
  board is meant to end up, so a board description must not depend on
  Android's audio HAL or services to work.
- **Nothing is opened by number.** `/dev/input/event2` is the volume keys on
  a Dot 2 and a touchscreen on an Echo Show 5. Opening the wrong one succeeds
  and reads nothing, so the failure is silent.
- **A board that is not described is not guessed at.** A part the board does
  not state is not opened.

## How it works

1. **Detect.** `board.Detect` reads Amazon's product id from
   `/proc/idme/device_type_id` and matches it exactly against the known
   boards. The device tree cannot do this: every MT8163 product reports the
   same model.
2. **Describe.** Each board has a `Hardware` value in
   `device/pkg/board/hardware.go` naming its parts.
3. **Resolve.** `board.Resolve` finds each named part on the running device,
   using the files below. Two parts with the same name is an error, never
   "take the first".
4. **Open.** The drivers under `device/internal/bindings/` ask
   `board.CurrentLayout()` and open what it returned.

| Part | Named by | Looked up in |
|---|---|---|
| Action and mute keys, volume keys | input device name | `/proc/bus/input/devices` |
| LED ring | i2c driver name | `/sys/bus/i2c/devices/*/name` |
| Microphone and speaker PCMs | ALSA stream name | `/proc/asound/pcm` |
| Ambient light sensor | i2c driver name, plus the attribute that reads lux | `/sys/bus/i2c/devices/*/name` |
| Mute button LED | sysfs GPIO number | stated by the board |
| Bluetooth controller | HCI device path | stated by the board |

The firmware prints the result at start-up, one `[board]` line per part.

## See what a device resolves to

```
server board
```

This prints the board and where each part was found, and changes nothing. It
is safe to run next to a running firmware. On a Dot 2:

```
board: biscuit
dot keys: "mtk-kpd" at /dev/input/event1
volume keys: "keys" at /dev/input/event2
led ring: "is31fl3236" at /sys/bus/i2c/devices/0-003f
capture: "TLV320AIC3101 Capture" at card 0 device 24
playback: "TLV320AIC3204 Playback" at card 0 device 23
mute led gpio: "444"
light sensor: "tsl2540" (als_lux)
bluetooth hci: "/dev/stpbt"
```

On a board the firmware does not know, the first line is `board: unknown` and
the rest is the Dot 2's layout, because that is what the firmware assumed of
every device before boards were told apart. **Do not start the full firmware
on an unknown board on the strength of that output** — see the mute LED
warning below.

## Trying a new board

Before any code, collect what the device reports. Run
`porting/profile.sh` (read-only) and keep the output: it contains everything
the steps below ask for.

1. **Get the product id.** `cat /proc/idme/device_type_id`. The value ends in
   a NUL byte; the 14 characters before it are the id.
2. **Read the names.**
   - `cat /proc/bus/input/devices`: the `N: Name=` of the device that carries
     each key. Press the buttons under `getevent -l` to see which device each
     one arrives on.
   - `cat /proc/asound/pcm`: the stream name is the text after `CC-DD: ` up
     to the DAI name. On a Dot 2, `TLV320AIC3204 Playback`.
   - `for d in /sys/bus/i2c/devices/*; do echo "$d $(cat $d/name)"; done`:
     the ring driver and the light sensor.
3. **Add the board** in `device/pkg/board/`:
   - `board.go`: a `Board` with its `ID` and `DeviceTypeID`, added to `Known`.
   - `hardware.go`: its `Hardware`. Leave out anything you have not
     confirmed. Leave `Fallback` fields empty: they exist so the Dot 2 fleet
     keeps working if a kernel renames something, and a new board has no
     fleet to protect.
4. **Add a fixture** under `device/pkg/board/testdata/<board>/` with the
   files from step 2, and a test like
   `TestBiscuitResolvesToTheNumbersItAlwaysUsed` that pins where each part
   resolves. Every fixture must detect as exactly one board.
5. **Build and run `server board`** on the device. Every part should be found
   by name. A line saying `not available` is a part the firmware will not
   open.
6. **Then** start the firmware, with the speaker volume low and the device
   somewhere you can power-cycle it.

| File | What goes there |
|---|---|
| `device/pkg/board/board.go` | The board's id and Amazon product id |
| `device/pkg/board/hardware.go` | Where its parts are, by name |
| `device/pkg/board/tuning.go` | Thermal and CPU policy for emOS. Optional; without it the kernel's stricter defaults stay |
| `device/pkg/board/testdata/` | What the device reported, as a fixture |
| `device/pkg/board/guard_test.go` | Nothing to add. It fails the build if a driver opens hardware by number |

## What the description does not cover yet

These are still written for the Dot 2 inside the drivers. A board that
differs in any of them needs code, and the description will grow to hold
them as the second board is added.

- **Microphone format and layout.** The pipeline expects 9 channels of 24-bit
  audio: six mics around the edge, one in the centre, and two channels of
  playback loopback.
- **Audio routes.** `internal/bindings/codec/routes.go` closes ten mixer
  switches by name, all of them the Dot 2's.
- **Amplifier and volume.** The amp switch, the DAC's unity value (127) and
  the click-free start-up order.
- **Headphone jack.** The detect switch and the two controls written on
  plug-in and removal.
- **Start-up order.** Some boards need one PCM opened before another.
- **Android services.** `stop mixer`, `stop media`, `stop ledcontroller` and
  `stop acebutton` are run as they are named on the Dot 2.
- **CPU core parking** under `/proc/hps`, which is MediaTek's.
- **WiFi.** `wlan0` and wpa_supplicant. What differs here is FireOS against
  emOS, which the firmware already handles.

## Caveats

**A wrong GPIO can silence the speaker until reboot.** The Dot 2's mute
button LED is sysfs GPIO 444. On the Echo Dot 3rd gen that number is a
different SoC pin, in use as an audio clock; exporting it switches the pin
to plain GPIO and the amplifier shuts down until the next boot. A board
states its own `MuteLEDGPIO`, and a board that states none has no mute LED
driven. An *unknown* board still gets the Dot 2's layout, including 444, so
add the board before running the firmware on it.

**The i2c bus listing is not an inventory.** Names under
`/sys/bus/i2c/devices` come from the kernel's board file and appear whether
or not the chip is soldered on. The Dot 2 lists two light sensors and has
one. A name with a working attribute behind it is a part; a name alone is
not.

**A device node existing does not mean the hardware is behind it.**
`/dev/stpbt` is MediaTek's combo-chip Bluetooth node. It is present on the
Dot 3 too, where the radio is a different chip. That is why each board states
its Bluetooth device.

**Resolve keys by device name, not by what a device claims to support.** On
the Dot 2 both input devices advertise the volume-down key, and only one of
them ever sends it.

**Mixer control numbers move between kernels.** FireOS 6's kernel has two
more controls than FireOS 5's, so every number after them shifts. Writing to
the wrong control succeeds. Use names, through `internal/bindings/mixer`.

**Do not use Amazon's HAL constants as measurements.** The HAL's mute LED
constant is off by one from the kernel's numbering and its amp gain control
does nothing on the Dot 2. Measure on the device.

**Things that hang or wedge a device.** Do not `unbind` a driver to
experiment (the next read hangs until a power cycle). Do not run `tinyplay`
while the firmware is running (they contend for the same PCM). Do not read
`power_button_state` under the FireOS 6 privacy driver (it blocks).

**Do not carry Amazon's files.** Read values off your own device and put the
numbers in the description. Config files, firmware and libraries from the
stock system do not go in the repository.

**One unit is one data point.** Parts are second-sourced between production
batches: some Dot 2s have a different light sensor on a different bus. Say
how many units a value was read from.

## What reaches the controller

A part that was not found by name is reported to the controller once per
start, as a warning in the device's log on the dashboard: `[board] dot keys:
no input device named "mtk-kpd"; using /dev/input/event1 as on every unit
measured`. A device no board matched reports `[board] not identified`. If you
see either on a Dot 2, please open an issue with the output of `server board`.

## Credits

What this page says about boards other than the Dot 2 comes from people who
put the firmware on their own hardware and wrote down what they found:

- **Echo Dot 3rd gen:** @shortgame11 (the first working port, and the
  findings about the amplifier, its clock and GPIO 444), @technotiger
  (profile and probe runs).
- **Echo 2nd gen:** @vithurshanselvarajah and @jcsnider.
- **Echo Show 5:** @imduffy15.

## Where the Dot 2's values came from

Names were read from three units on 2026-10-04: one on stock FireOS 5, one on
emOS with the FireOS 6 (32-bit) kernel and one on emOS with the FireOS 5
(64-bit) kernel. All three report the same names at the same numbers. The
first unit's output is the fixture at
`device/pkg/board/testdata/biscuit-fireos5/`.
