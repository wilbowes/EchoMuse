# Radar (Amazon Echo 2) — analysis notes

Captured 2026-09-17 against a rooted FireOS 6 device
(`G2A0P308816702JC`, board_id `0120 0014 0004 0017`).

These notes are the offline read of the device's hardware
enumeration plus a small set of mixer experiments. **Nothing
here was flashed; the device is back on stock FireOS.**

## What was checked

| Property | Where | Value |
|---|---|---|
| SoC | `uname -a` | MT8163 (armv7l), kernel 3.18.19 |
| Boot slot | `ro.boot.slot_suffix` | `_a` (boot_a at p10) |
| Cache partition | `/dev/block/by-name/` | p15, ext4, 756 MB |
| Audio card | `/proc/asound/cards` | card 0 = `mtsndcard` |
| Wake-word capable | `/sys/class/wmtWifi/` | connsys powered, `wlan0` up |
| Buttons | `/proc/bus/input/devices` | gpio-keys on event2 = `KEY_VOLUMEUP/DOWN` |
| ALS | `/sys/bus/i2c/devices/` | tsl2540@0x39 + tsl2584tsv@0x29 |
| LED ring | `/sys/bus/i2c/devices/0-003f/` | is31fl3236 (driver bound, 12 LEDs) |
| LP55231 chips | `/sys/bus/i2c/devices/0-0032/..0035/` | driver unbound — no enable-gpio |

## LED ring

This is the key surprise. Radar's LED ring is **identical to
biscuit's**: an `is31fl3236` at i2c-0 0x3F, with the same
`frame` (72-hex-char RGB triplet sequence for 12 LEDs),
`led_current`, and `boot_animation` sysfs attributes. The
firmware's `device/internal/bindings/led/i2c_controller.go`
already hardcodes the same path.

What I had wrong earlier: the **four LP55231 chips at i2c-0
0x32-0x35 are NOT the LED ring**. They are present in DT, the
`lp5523x` driver directory exists in `/sys/bus/i2c/drivers/`,
but `bind` fails with:

```
lp5523x 0-0033: could not acquire enable gpio (err=-517)
i2c 0-0033: Driver lp5523x requests probe deferral
```

The same error for 0-0034, 0-0035. For 0-0032 the DT has an
`enable-gpio` property (phandle #9, pin 0x1d) but the kernel
still probes defer because the GPIO controller that phandle
points at does not exist in this build — only `gpiochip357` is
on the running system. So even the one chip that has the
property bound, cannot be brought up without a kernel rebuild
or DT overlay.

**Conclusion**: the LP55231 cluster is for something the
Amazon userspace controls through libagl, not for the LED
ring, and the LED ring is exactly the same as biscuit.

### Experiment: stop `ledcontroller` and drive the ring directly

`ledcontroller` (PID 207 at boot, restarted as PID 3787 after
this test) holds the LED ring. Stopping it via `stop
ledcontroller` frees `/sys/devices/soc/11007000.i2c/i2c-0/0-003f/frame`
for direct writes — verified:

```
$ adb shell 'echo "ff0000000000000000000000000000000000000000000000000000000000000000000000" > \
              /sys/devices/soc/11007000.i2c/i2c-0/0-003f/frame'
$ adb shell 'cat /sys/devices/soc/11007000.i2c/i2c-0/0-003f/frame'
ff0000000000000000000000000000000000000000000000000000000000000000000000
```

LED 0 turns full red; the others stay off. The 12-LED chase
around the ring works the same way (one full revolution
per ~4.8 s at 0.4 s per LED), confirming the physical layout
is the same shape as biscuit's.

### Layout numbers (LED_BOTTOM, LED_DIR, ORBIT_STEP_MS)

These were not measured here because the apparent kernel
orbit did not resume after `ledcontroller` was restarted —
the framework takes a few seconds to re-bind to AIPC, and we
did not wait for it. The board header carries the **biscuit
defaults** as placeholders. They need to be measured off a
fresh radar kernel, the same way the comments in
`emos/init/boards/biscuit.h` describe, before the radar port
can ship.

## Audio: 8-mic array via 4× TLV320AIC3101

The radar's microphone array is the most interesting piece of
this hardware. It is **8 channels in a 4-corner-mic square**
(per `coefs_FBFV2_4micsSq_8beams_*.cfg`):

```
redesign 4mics square 8beams beams usual mics 1,2,4,5 (1-indexing) and IDNR -15
```

The 4 corner mics × 2 "look directions" (FF + NF for the EVD
MVDR beams) = 8 beams. The MTK HAL reports
`MTK_DUAL_MIC_SUPPORT=no` — the 8-beam beamforming is run by
**Amazon's custom DSP library**, not the MTK audio HAL.

### Hardware topology

```
i2c-0:    0-0018 = tlv320aic3101 (ADC_A)  -- mics 1, 2
          0-0019 = tlv320aic3101 (ADC_B)  -- mics 3, 4
          0-001a = tlv320aic3101 (ADC_C)  -- mics 5, 6
          0-001b = tlv320aic3101 (ADC_D)  -- mics 7, 8
          0-0039 = tsl2540 (ALS, primary)
          0-0029 = tsl2584 (ALS, secondary)

i2c-1:    1-0060 = sym827 (regulator)

i2c-2:    2-0018 = tlv320aic32x4 (playback DAC)
          2-006b = bq24297 (battery charger)
```

All four ADC chips share the same I2S bus via
`mediatek,mt8163-soc-pcm-dl1@11220000` and appear as a single
ALSA device to userspace:

```
pcmC0D24c   "TLV320AIC3101 Capture tlv320aic3101-codec-24"
            subdevices_avail: 0  (when nobody has it open)
```

The four chips' I2S lanes carry 8 channels at 16 kHz (the
capture sample rate Amazon's voice pipeline uses).

### Calibration: `miccal.0` – `miccal.6`

`/proc/idme/miccal.N` carries 7 calibration values, not 8
(matching the 7 entries in `device/CLAUDE.md`):

```
miccal.0 = 16358
miccal.1 = 15428
miccal.2 = 16023
miccal.3 = 16916
miccal.4 = 17464
miccal.5 = 17405
miccal.6 = 15487
```

5-digit decimal numbers; range 15k-17k. These have **never
been read by anything in EchoMuse** — the comment in
`device/CLAUDE.md` calls them out as a future capability.

The natural reading is one of:

- 7 of 8 mics are calibrated, the 8th is the AFE echo
  reference (used as the speaker-playback calibration
  source, no per-unit correction needed)
- 4 corner mics × 2 axis (FF + NF) = 8 axes, minus the
  axis that gets its calibration from the AFE ref = 7
  per-unit values

Either way, the factory writes 7 values, init should expect
7.

### Audio HAL config (`AudioParamOptions.xml`)

```
MTK_DUAL_MIC_SUPPORT              no
MTK_AUDIO_HD_REC_SUPPORT          no
MTK_AUDIO_BLOUD_CUSTOMPARAMETER_REV  MTK_AUDIO_BLOUD_CUSTOMPARAMETER_V5
MTK_AUDIO_SPEAKER_PATH            "" (empty -- no specific speaker path)
MTK_FM_SUPPORT                    no
MTK_VOIP_ENHANCEMENT_SUPPORT      no
MTK_VOICE_UI_SUPPORT              yes
MTK_VOICE_UNLOCK_SUPPORT          yes
MTK_ASR_SUPPORT                   no
MTK_HANDSFREE_DMNR_SUPPORT        no
MTK_MAGICONFERENCE_SUPPORT        no
VIR_WIFI_ONLY_SUPPORT             yes
VIR_3G_DATA_ONLY_SUPPORT          no
```

The relevant flags for voice:

- `MTK_VOICE_UI_SUPPORT=yes` — wake-word is enabled at the
  HAL level (no MTK ASR engine though)
- `MTK_VOIP_ENHANCEMENT_SUPPORT=no` — no VoIP-specific AEC
- `MTK_HANDSFREE_DMNR_SUPPORT=no` — no MTK dual-mic NR (so
  the 8-mic NR is all Amazon's)

### Beamforming coefficients

`/vendor/etc/audio-algorithms/`:

| File | Size | Purpose |
|---|---|---|
| `coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg` | 540 KB | 4-corner square, 8 beams, IDNR -15 dB, 2 ms latency |
| `coefs_FBFV2_EVD_FF_MVDR_8beams.cfg` | 540 KB | MVDR beamforming, far-field (FF) |
| `coefs_FBFV2_EVD_NF_MVDR_8beams.cfg` | 540 KB | MVDR beamforming, near-field (NF) |
| `coefs_FilterBank_AnalysisSynthesis_1024.cfg` | 20 KB | filter-bank, LFFT=8192, M=256, D=128, Lh=1024 |
| `EdgeflowModelConfig.json` | — | AEC echo-likelihood model |
| `MBCL.cfg` / `MBCL_VOIP.cfg` | — | microphone boost / level calibration |

These are the **OEM's beamforming coefficients**. If
EchoMuse's firmware-side mic path ever needs to do its own
beamforming instead of relying on the kernel's MTK AFE
pipeline, these are the inputs. They are flat arrays of
floats — no headers, just comma-separated IEEE-754 little
endian (the comment in the FBF file points to an internal
Amazon repo).

### Mixer paths

`/vendor/etc/audio_device.xml` (202 lines) defines the
analog audio paths: speaker, headphone, receiver, 2-in-1
speaker, headset mic, etc. It writes to
`Ext_Speaker_Amp_Switch`, `Speaker_Amp_Switch`,
`Audio_Amp_R/L_Switch`, etc. — these are the controls in
the `tinypcminfo` output's ENUM section.

`/vendor/etc/mixer_paths.xml` (119 lines) is the ASoC board
mixer: routes I2S lanes, ADC inputs, PGA gains. Most
playback paths are stubbed empty. The interesting entries
are the ADC A/B/C/D routing (`ADC_A Left Ip Select ADC_A
DIF1_L switch` etc.) which is set to **DIF1** for the active
mic capture path — meaning each ADC chip's left and right
inputs come through the digital mic interface #1, dropped
on the I2S bus.

### Audio warning

`tinypcminfo -D 0 -d 24` (probing the 8-channel mic pcm)
**hangs** the device for 10+ seconds before being killed.
The TLV320AIC3101 driver probably needs the I2S clocks
explicitly enabled via ASoC DAPM before the device responds.
EchoMuse's firmware-side mic.c will need to be careful
about clock-gating order if it opens this device cold.

## What this means for the radar port

1. **LED**: `BOARD_LED_NODE` is `0-003f` (already fixed in
   this PR). `BOARD_LED_COUNT` is 12 (fixed). The layout
   numbers (`LED_BOTTOM`, `LED_DIR`, `ORBIT_STEP_MS`) are
   still biscuit placeholders and need measurement.

2. **Audio**: `pcmC0D24c` is the 8-channel mic capture. The
   ALSA major/minor is 116,38 (already in `radar_nodes[]`).

3. **Wifi**: same bring-up as biscuit; same chip, same
   patches in `/vendor/firmware/`. The boards_radar.c copy
   works as-is.

4. **Beamforming**: EchoMuse's controller-side beamformer
   on biscuit takes the 9-channel mic array and forms a
   beam. On radar it would take the 8-channel array and
   pick one of the 8 pre-computed beams from the
   `coefs_FBFV2_*.cfg` files. The firmware-side mic.c
   needs to read those files off the device and feed them
   to the controller's beamformer — out of scope for the
   board-port PR, but the coefficients are captured here.

## Files in this dump

| File | Bytes | Source on device |
|---|---|---|
| `tinymix.txt` | 13 KB | `tinymix` (all 241 controls) |
| `audio_device.xml` | 9 KB | `/vendor/etc/audio_device.xml` |
| `mixer_paths.xml` | 3.5 KB | `/vendor/etc/mixer_paths.xml` |
| `AudioParamOptions.xml` | 2.4 KB | `/vendor/etc/audio_param/AudioParamOptions.xml` |
| `coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg` | 540 KB | `/vendor/etc/audio-algorithms/` |
| `coefs_FBFV2_EVD_FF_MVDR_8beams.cfg` | 540 KB | `/vendor/etc/audio-algorithms/` |
| `coefs_FBFV2_EVD_NF_MVDR_8beams.cfg` | 540 KB | `/vendor/etc/audio-algorithms/` |
| `coefs_FilterBank_AnalysisSynthesis_1024.cfg` | 20 KB | `/vendor/etc/audio-algorithms/` |
