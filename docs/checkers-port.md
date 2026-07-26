# Echo Show 5 gen 1 ("checkers") port

Support for the Amazon Echo Show 5 (2019) running LineageOS 18.1, alongside
the original Echo Dot gen 2 ("biscuit").

Everything below was measured on the hardware, not inferred from datasheets.

## Why this device is close to biscuit

The Echo Show 5 uses the same MT8163 SoC and, more usefully, the **same
microphone driver**. `amzn-mt-spi-pcm` presents the mic array over SPI from an
FPGA that converts I2S, and its channel count is a compile-time kernel option:

```c
#if defined CONFIG_SND_SOC_8_MICS
#define SPI_N_CHANNELS   9      /* biscuit */
#elif defined CONFIG_SND_SOC_4_MICS
#define SPI_N_CHANNELS   6
#else
#define SPI_N_CHANNELS   4      /* checkers */
#endif
```

checkers builds the `#else` branch, confirmed against the device's own
`/proc/config.gz`, which has both `SND_SOC_8_MICS` and `SND_SOC_4_MICS` unset
and ships `CONFIG_EXTRA_FIRMWARE="i2s_to_spi_4ch_v193.bin"`.

So the capture path is not a rewrite. It is the same driver with different
constants, which is what `internal/profile` now expresses.

## Hardware map

|                | biscuit (Dot 2)        | checkers (Show 5 gen 1)   |
| -------------- | ---------------------- | ------------------------- |
| SoC            | MT8163                 | MT8163                    |
| Mic ADCs       | 4x TLV320ADC3101       | 1x TLV320AIC3101          |
| Capture PCM    | card 0, device 24      | **card 0, device 22**     |
| Capture format | 9ch S24_3LE @16k       | **4ch S24_3LE @16k**      |
| Playback PCM   | card 0, device 23      | card 0, device 23         |
| Output codec   | TLV320AIC32x4          | **RT5616** + amp on GPIO35 |
| Output         | mono                   | mono                      |
| LED ring       | 12x IS31FL3236A        | **none** (screen)         |
| ABI            | armeabi-v7a            | armeabi-v7a               |

Capture constraints, as reported by the driver's own `HW_REFINE` rather than
assumed:

```
format      S24_3LE only
channels    4 (channels_min == channels_max)
rate        16000 .. 48000
period      257 .. 2570 frames  (3084 .. 30840 bytes, 12 bytes/frame)
periods     1 .. 10
access      RW_INTERLEAVED only
```

Channel roles:

```
ch0, ch1    microphones      (0.92 inter-mic correlation)
ch2, ch3    AEC reference    (see below)
```

## The hardware AEC reference

ch2/ch3 carry a loopback of the playback signal, resampled 48k -> 16k by the
FPGA and delivered **in the same stream as the mics**, so it is inherently
sample-aligned. Measured:

```
correlation ch2 vs ch3   1.000000   (bit-identical; max |ch2-ch3| = 0)
echo path delay          40 samples (2.50 ms)
echo attenuation         -13.0 dB
mic/reference correlation 0.83
```

**This is characterised but not yet consumed.** The AEC currently takes its far
end from the speaker's software echo tap, the same as biscuit, and that works, `aecEnabled: true` with `aecDelayMs: 0`. Feeding it ch2 instead is a follow-up:
the hardware reference is aligned by construction, so it should remove the need
for the drift governor biscuit uses, but that change belongs with a measurement
of the improvement rather than bundled into the port.

The channel roles are recorded in the profile (`Mic.RefChannels`) so the
plumbing is ready.

The reference is silent whenever nothing is playing, so an all-zero ch2/ch3 in
a capture means "no playback", not "no loopback".

## The one non-obvious setting

```
Ext_Speaker_Amp_Switch  must be "Off" for audio to reach the speaker
```

Its boot default is `On`, and `On` silences output completely. Nothing about
the name suggests this. It was found by snapshotting the mixer at the instant
Android's audio HAL opened the output PCM and diffing against a pristine
post-boot baseline: that control and the expected DAPM unmute were the *only*
two differences.

`Profile.MixerInit` sets it; `Profile.AmpOff` puts it back to `On` on shutdown,
which is the quiet state.

Playback settings are copied from what the Amazon HAL negotiates:
`S16_LE, 2ch, 48000 Hz, period 1536, 2 periods`.

## What changed in the tree

| Path | Purpose |
| ---- | ------- |
| `internal/alsa/` | Dependency-free ALSA PCM client (raw ioctls, no CGO, no tinyalsa). Needed because the mic PCM only accepts S24_3LE. |
| `internal/profile/` | Per-device hardware description; replaces compile-time constants. Autodetects from `ro.product.device`. |
| `internal/bindings/mic/profile_microphone.go` | Profile-driven capture with the same fan-out and capture-loss telemetry as the original. |
| `internal/bindings/speaker/profile_speaker.go` | Profile-driven playback. |
| `internal/bindings/led/null_controller.go` | LED backend for devices with no ring. |
| `internal/beamformer/bypass.go` | Passthrough for devices without a steerable array, bit-identical to `extractChannel`. |
| `tools/profile_smoke/` | On-device end-to-end validation. |
| `controller/device_payloads/start_server_checkers.sh` | Launcher without the Fire OS assumptions. |

Nothing existing was removed; the biscuit bindings are untouched and remain the
default when the profile is unknown.

## Why no beamformer

Two microphones fed by one stereo ADC is not an array worth steering, and the
0.92 inter-mic correlation confirms both channels see substantially the same
field. `Bypass` extracts a single channel (or the mean of both) using
byte-for-byte the same gain, clipping and telemetry path as
`Beamformer.extractChannel`, verified by `TestBypassMatchesExtractChannel`.

This removes ~500 lines of the hardest, most empirical work from the port
rather than deferring it.

## Building

The bindings need no CGO:

```sh
GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 go build ./internal/... 
```

The full server still needs the Android NDK for the AEC and evdev; retarget
`device/compiler/Dockerfile` from `API=22` to `API=30`. The `armv7a` triple is
unchanged, checkers reports `ro.product.cpu.abi=armeabi-v7a` despite the
64-bit A53.

## Validating on hardware

```sh
GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 go build -o profile_smoke ./tools/profile_smoke/
adb push profile_smoke /data/local/tmp/
adb shell /data/local/tmp/profile_smoke
```

It verifies the profile against the driver, captures a baseline, plays a tone
through the `Speaker` interface, and asserts that the microphones and the AEC
reference both hear it. It restarts the Android audio services and resets the
amp on exit, including on SIGINT, so a failed run does not leave the device
silent.

Expected output ends with:

```
PASS: microphones hear the speaker, playback and capture both work
PASS: hardware AEC reference is live
```

## Device access notes

- The LineageOS build is `userdebug` with `ro.debuggable=1`, so `adb root`
  works; Magisk is not required for bring-up.
- SELinux is already permissive, so no policy work is needed. biscuit needed
  its boot image patched because Fire OS's LK bootloader hardcoded
  `androidboot.selinux=enforce`.
- **Persistence is still unsolved.** With no Magisk there is no `service.d` to
  launch from. Either install Magisk, or remount `/system` and add an
  `init.rc` service. This does not block anything above.

## Wake acknowledgement without an LED ring

biscuit's 12-LED ring turning green is what tells you the device heard the wake
word. A device with a screen and no ring has nothing, and without feedback you
talk over a device that is not listening.

`internal/cue` renders that feedback from hardware the Show 5 does have. It
hangs off the signal the device already receives: the controller's explicit
"this frame is the listening ring" hint: so no new control message was needed:

| Event | Cue |
| ----- | --- |
| Listening starts | rising two-note chime, screen backlight to full |
| Listening continues | screen **held** bright |
| Listening ends | falling two-note chime, backlight restored |

The screen is held rather than blipped deliberately. A flash answers "did it
hear me" but not "is it still listening", and the second question is the one
you need answered while deciding whether to keep talking.

Implementation notes worth keeping:

- Fires on **edges only**. The controller repaints the listening frame
  throughout a turn; without edge detection every repaint re-chimes.
- Each note gets a **raised-cosine envelope**. A bare sine switched on and off
  clicks, and on this speaker the click is louder than the tone.
- The chime goes through `PumpPeriod`, the same path as TTS, so it also lands
  in the AEC reference rather than being a signal the canceller never saw.
- A 60s safety net restores the backlight if the falling edge never arrives
  (controller drops, turn times out), so the screen cannot stay stuck bright.
- Disabled for biscuit, whose ring already does the job.

## Tuning found on hardware

- **Mic gain 12 dB, not the 24 dB default.** At 24 dB the capture clipped hard
  (`clipped=1555`, rms 0.43) and Whisper hallucinated text out of the clipped
  noise, transcripts came back as `' Rules of woodworking and metal- What time
  is it?'`. Turns also hit the 50s timeout because the noise floor held the VAD
  gate open. At 12 dB clipping stops and rms sits around 0.02.
- **AEC on, `aecDelayMs: 0`.** The hardware reference is already sample-aligned
  (2.5 ms), so there is no bulk latency to compensate for; the 300 ms tail
  covers room reverb.

Both are controller-side config (`system_config` → `global_device_config`),
not device constants.

## Android rewrites the amp control behind you

Setting `Ext_Speaker_Amp_Switch` once at startup is not enough. Android's audio
HAL runs a device turnoff sequence whenever it opens or closes an output of its
own, and that sequence writes the same control:

```
AudioALSADeviceConfigManager: ApplyDeviceTurnoffSequenceByName() DeviceName = ext_speaker_output
AudioALSADeviceConfigManager: cltname = Ext_Speaker_Amp_Switch cltvalue = On
```

On is the silent state, so the daemon goes quiet with nothing logged. From its
own side playback is still succeeding: the controller reports the reply
delivered and played (`tts_bytes=270000, playback=+4075ms`). The audio reaches
the DAC and dies at the amp.

This is latent on a device running nothing else, and becomes routine as soon as
any Android app plays a sound. Pairing this daemon with a display app such as
View Assist Companion is enough to trigger it within seconds of startup.

`Profile.AmpOn` and `ProfileSpeaker.armAmp` handle it by re-applying the amp
state at the start of every stream and every chime, rate limited to once every
three seconds.

Worth being precise about the shape of the conflict. The PCM nodes are
genuinely disjoint: this daemon owns devices 22 and 23, Android's HAL uses the
MultiMedia1 route on devices 0 and 1, so there is no contention for the device
nodes. The mixer is shared, and that is where the two stacks collide.

## Never stop audioserver on LineageOS

This is the one change that looks obviously right and bootloops the device.

The daemon wants Android's audio HAL away from the PCMs, and the biscuit
profile achieves that by stopping Fire OS services. Doing the equivalent here, `stop audioserver`, is fatal. `system_server` makes synchronous binder calls
into `media.audio_policy`, which audioserver provides. With it stopped those
calls block forever:

```
ServiceManager: Waiting for service 'media.audio_policy' on '/dev/binder'
Watchdog: *** WATCHDOG KILLING SYSTEM PROCESS: Blocked in handler on main thread
    at android.media.AudioSystem.setA11yServicesUids(Native Method)
    at com.android.server.audio.AudioService...
```

Android's Watchdog kills `system_server` after 60s, and killing `system_server`
reboots the device. With the daemon started from init, it comes back and does it
again: a bootloop on a roughly 75-second cycle. `console-ramoops` shows a clean
`reboot: Restarting system with command ''`, not a panic, which makes it easy to
misread as an ordinary reboot.

Leaving audioserver alone is safe. The HAL only opens a PCM when something
plays, both PCMs report `subdevices_avail: 1` at idle, and the daemon opens
them exclusively. If Android tries to play afterwards it just fails to open the
device, which is the intended outcome.

If HAL contention ever does become a real problem, neuter the HAL rather than
the service: replace `/vendor/etc/audio_policy_configuration.xml` with one
declaring no primary module. audioserver stays alive and answers binder calls
but never opens a PCM.

## Warning

Do not experiment with the RT5616 mixer controls casually. Setting
`HP Playback Switch` on, or routing `LOUT MIX DAC L1/R1` alongside the already
routed `OUTVOL L/R`, silences Android audio in a way the HAL does not restore, it only manages the controls it knows about. A reboot clears it, since mixer
state is volatile. Snapshot `tinymix -D 0` before changing anything.
