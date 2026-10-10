# Radar speaker startup

Echo 2nd gen (2017, Radar) can run PCM23 normally while its internal speaker
stays silent. Without the stock audio service, the codec's 117-byte speaker
filter may contain zeros and `MFP Gpio Mute` may remain asserted. See
[issue #535](https://github.com/wilbowes/EchoMuse/issues/535).

The device application owns this setup. Radar is identified by the exact
`/proc/idme/device_type_id` value `A7WXQPH584YP`. Other boards keep their existing
startup timing and mixer writes; identifying Radar does not apply Biscuit's
thermal tuning.

## Listening tests

When judging Radar's sound, compare with the bass guard disabled and the EQ
flat; leave the limiter enabled. The guard is tuned for the Dot's smaller
driver and may remove bass the Echo 2 woofer can reproduce. On one Echo 2
running build [#687](https://github.com/wilbowes/EchoMuse/pull/687), ordinary
TTS drove guard reduction to 30dB, and quieter replies reached 13.6dB. Turning
the guard off made an immediate audible improvement. The dashboard's music
EQ preset also sounded worse than flat on that unit. These are tester
observations from one device, not Radar-wide tuning measurements; no Radar
defaults are changed here.

On Radar, the application:

1. Asserts physical mute before stopping the stock media services.
2. Reads `ext_speaker_output` / `turnon` from the device's own
   `/system/etc/audio_device.xml`. Only the `biquad coefficients` control is
   imported. Missing, duplicate, incorrectly sized, out-of-range or all-zero
   profiles fail startup while muted.
3. Writes the complete profile in one tinyalsa byte-array operation, then
   prepares the output routes with DAC volume zero.
4. Starts silence, checks that PCM23 reaches `RUNNING`, waits three seconds,
   releases physical mute at zero DAC gain, waits four seconds, then ramps the
   DAC to unity. User volume is still applied in software. This adds about
   eight seconds to Radar's application startup.
5. Reasserts physical mute before normal shutdown and on playback failure.
   The supervisor also mutes Radar after abnormal process exits.

No stock configuration file, filter values, firmware image or proprietary
processing library is distributed by this change. The existing stock system
partition supplies the profile. The byte-array symbol is resolved only when
used, so its absence on another device does not prevent that device's binary
from loading.

Application OTA delivers the setup; the controller's existing startup-script
synchronization delivers supervisor changes. No kernel or emOS flash is needed.

## Playback tuning for Radar testers

The default bass guard and the dashboard's Music EQ preset were tuned for the
Dot 2 driver. A Radar tester reported the guard reaching its full 30dB depth
on ordinary TTS, making speech sound thin, and preferred flat EQ to the Music
preset. This is one device's listening report, not a measured Radar-wide
profile. For an initial Radar listening test, set **Config → Playback → EQ**
to **Flat** and turn **Speaker protection** off. Leave the limiter enabled;
it protects against peaks independently of the bass guard. These are playback
settings, not the Radar startup calibration above. Biscuit keeps the existing
guard-enabled default.

## Validation and limits

The settling sequence was validated on two Radars using the NS6572/6436 stock
system base under emOS: clear spoken output without observed pops/crackle,
application updates, and full reboots. These are listening observations, not
electrical transient measurements. Headphone output, other Radar stock builds
and FireOS service interactions still need hardware validation. Other boards'
unchanged shell writes are covered by regression tests, not new hardware tests.

Host tests cover profile selection/validation, byte-array bounds, startup
failure paths, PCM readiness and board identity. Reproduce with:

```sh
cd device
go test -race ./internal/bindings/speaker ./internal/bindings/mixer ./pkg/board
go vet ./internal/bindings/speaker ./internal/bindings/mixer ./pkg/board
cd ../controller
python -m pytest tests/test_deploy.py tests/test_mixer_names.py
```

Build the ARM application with the pinned compiler described in
[CONTRIBUTING.md](../CONTRIBUTING.md). A host test run does not validate the
native library ABI.
