// Package profile describes the audio hardware of a supported device.
//
// Before this existed, card/device numbers, channel counts and the mixer init
// sequence were compile-time constants scattered across the bindings and
// start_server.sh, all of them specific to the Echo Dot gen 2 ("biscuit").
// Supporting a second device means naming those values and selecting them at
// runtime.
package profile

import (
	"log"
	"os/exec"
	"strings"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/alsa"
)

// Mic describes the capture stream.
type Mic struct {
	Card, Device int
	Channels     int
	Format       alsa.Format
	SampleRate   int
	PeriodSize   int
	Periods      int

	// MicChannels are the channel indices carrying microphone audio.
	MicChannels []int
	// RefChannels carry a hardware loopback of the playback signal, suitable
	// as an AEC reference. Empty when the device provides no such feed.
	RefChannels []int
}

// FrameBytes is the size of one interleaved frame across all channels.
func (m Mic) FrameBytes() int { return m.Channels * m.Format.Bytes() }

// Speaker describes the playback stream.
type Speaker struct {
	Card, Device int
	Channels     int
	Format       alsa.Format
	SampleRate   int
	PeriodSize   int
	Periods      int
}

// FrameBytes is the size of one interleaved output frame.
func (s Speaker) FrameBytes() int { return s.Channels * s.Format.Bytes() }

// Buttons describes the evdev nodes carrying physical button events.
//
// Paths differ per device and some buttons simply do not exist: the Echo Dot
// has an action ("dot") button, the Echo Show 5 has only volume keys plus a
// mic-mute switch, so DotDevice is empty there and the subscriber skips it.
type Buttons struct {
	// DotDevice carries the action button. Empty when the device has none.
	DotDevice string
	// VolumeDevice carries volume up/down. Empty disables volume handling.
	VolumeDevice string
}

// Volume describes the mixer control that carries playback volume.
//
// biscuit drives control 61 ("PCM Playback Volume"); on checkers that index is
// Pmic_Anc_Switch, and the equivalent is the RT5616's DAC1 digital volume.
type Volume struct {
	// Selector is passed to tinymix: a control index or a name.
	Selector string
	// DisplayName is how tinymix prints the control, used to parse the value
	// back out of its output.
	DisplayName string
}

// WakeCue describes how the device acknowledges wake-word detection on
// hardware with no LED ring to turn green.
type WakeCue struct {
	// Enabled turns the whole thing on. False where the ring already is the
	// acknowledgement.
	Enabled bool
	// ChimeMs is the total length of the two-note tone; 0 disables the chime.
	ChimeMs int
	// ChimeHz is the first note; the second is a major third above it.
	ChimeHz float64
	// ChimeAmplitude is 0..1 of full scale.
	ChimeAmplitude float64
	// BacklightPath is the sysfs brightness file to blip; "" disables it.
	BacklightPath string
	// BacklightMax is the value written while listening. The screen is held
	// at this level for the whole turn and restored afterwards, rather than
	// blipped: a flash says "heard you" but not "still listening".
	BacklightMax int
}

// Profile is the full hardware description for one device.
type Profile struct {
	// Name matches ro.product.device.
	Name string

	Mic     Mic
	Speaker Speaker

	// MixerInit is applied once at startup, before any PCM is opened.
	MixerInit []alsa.Control
	// AmpOn puts the output stage into its audible state. Re-applied at the
	// start of every playback stream, not just once at startup: on checkers
	// Android's audio HAL rewrites the same control whenever it opens or
	// closes an output of its own ("ApplyDeviceTurnoffSequenceByName ...
	// cltname = Ext_Speaker_Amp_Switch cltvalue = On"), which silences this
	// daemon with nothing logged.
	AmpOn []alsa.Control
	// AmpOff silences the output stage. Applied when the server shuts down or
	// when playback is idle, so an enabled amp on an idle DAC does not hiss.
	AmpOff []alsa.Control

	// Buttons maps the physical controls to evdev nodes.
	Buttons Buttons
	// WakeCue is the acknowledgement shown when a voice turn starts.
	WakeCue WakeCue
	// Volume is the playback volume control.
	Volume Volume
	// MicMuteCtls are the mixer controls that mute the capture path. Another
	// device's indices address unrelated parts of its codec and would leave
	// the microphone live, so these are per-device like every other mixer
	// value here.
	MicMuteCtls []alsa.Control
	// HasMuteButtonLED is true where a discrete LED sits under the mic-off
	// button. The Show 5 has no such LED and no gpio444 to export.
	HasMuteButtonLED bool

	// SupportsBLEProxy gates the ESPHome bluetooth_proxy feature. Enabling it
	// is not free: the scanner needs exclusive ownership of /dev/stpbt, so it
	// permanently `pm disable`s the Android Bluetooth stack to take it. Only
	// turn this on where the HCI transport is known to work: on checkers the
	// scan fails immediately ("read during cmd 0c03: EOF"), so disabling
	// Bluedroid would cost the device its Bluetooth for nothing.
	SupportsBLEProxy bool

	// HasLEDRing is false on devices with a screen and no LED ring; the null
	// LED controller is used instead.
	HasLEDRing bool
	// LEDCount is the size of the ring, 0 where there is none. Callers that
	// paint a whole-ring colour use this rather than assuming 12.
	LEDCount int

	// AdcDigitalGainCtls and AdcMicpgaCtls are the mixer controls a config
	// push retunes when it carries adc_digital_gain / adc_micpga. biscuit has
	// four ADCs to keep in step; checkers has one.
	AdcDigitalGainCtls []alsa.Control
	AdcMicpgaCtls      []alsa.Control
	// Beamforming is only meaningful with enough mics to steer. With two mics
	// there is no array to speak of and the beamformer is bypassed.
	Beamforming bool

	// StopServices are init services stopped at startup so the daemon owns the
	// audio hardware outright.
	StopServices []string

	// UseALSABackend selects the dependency-free ALSA bindings over the
	// original tinyalsa ones. biscuit deliberately stays on tinyalsa: it is
	// the configuration those deployments have been running, and there is no
	// reason to move a working fleet onto a newer code path as a side effect
	// of adding a second device.
	UseALSABackend bool
}

// Capabilities is what the device tells the controller it can do, so the
// controller can skip work the hardware cannot use.
//
// "leds" stays on even without a ring: the device still consumes ring *state*
// : the controller's explicit "this frame is the listening ring" hint is what
// drives the chime and backlight cue: it just renders it differently. What it
// cannot use is "led_anim", the local animation engine, which would otherwise
// spin a ticker rendering frames for zero LEDs.
//
// Nothing is advertised for beamforming. A capability no controller reads
// would change the registration payload of every existing device for no
// effect; the controller side has to come first.
func (p Profile) Capabilities() []string {
	caps := []string{"mic", "speaker", "leds", "buttons"}
	if p.HasLEDRing {
		caps = append(caps, "led_anim")
	}
	return caps
}

// HasAECReference reports whether the capture stream carries a hardware
// loopback of the playback signal.
func (p Profile) HasAECReference() bool { return len(p.Mic.RefChannels) > 0 }

// biscuit is the Echo Dot gen 2: 4x TLV320ADC3101 feeding an FPGA that
// presents 9 channels over SPI (7 mics used, 2 unconnected), 12-LED ring,
// mono speaker. Values carried over from the original bindings.
var biscuit = Profile{
	Name: "biscuit",
	Mic: Mic{
		Card: 0, Device: 24,
		Channels:   9,
		Format:     alsa.FormatS24_3LE,
		SampleRate: 16000,
		PeriodSize: 512,
		Periods:    5,
		// 6 perimeter mics plus a centre omni; ch7/ch8 are unconnected.
		MicChannels: []int{0, 1, 2, 3, 4, 5, 6},
		RefChannels: nil,
	},
	Speaker: Speaker{
		Card: 0, Device: 23,
		Channels:   2,
		Format:     alsa.FormatS16LE,
		SampleRate: 48000,
		PeriodSize: 2048,
		Periods:    4,
	},
	MixerInit: []alsa.Control{
		{Index: 56, Values: []string{"On"}},
		{Index: 64, Values: []string{"1", "1"}},
		{Index: 88, Values: []string{"On"}},
		{Index: 61, Values: []string{"100", "100"}},
		// Mic gain, equalised across all four ADCs (A/B/C/D).
		{Index: 89, Values: []string{"88", "88"}},
		{Index: 92, Values: []string{"40", "40"}},
		{Index: 107, Values: []string{"88", "88"}},
		{Index: 110, Values: []string{"40", "40"}},
		{Index: 125, Values: []string{"88", "88"}},
		{Index: 128, Values: []string{"40", "40"}},
		{Index: 143, Values: []string{"88", "88"}},
		{Index: 146, Values: []string{"40", "40"}},
	},
	AmpOn: []alsa.Control{
		{Index: 61, Values: []string{"100", "100"}, Optional: true},
		{Index: 5, Values: []string{"On"}, Optional: true},
	},
	AmpOff: []alsa.Control{
		{Index: 61, Values: []string{"0", "0"}, Optional: true},
		{Index: 5, Values: []string{"Off"}, Optional: true},
	},
	Buttons: Buttons{
		DotDevice:    "/dev/input/event1",
		VolumeDevice: "/dev/input/event2",
	},
	// Per-chip ADC mute pairs across all four codecs (A: ch0/ch1 through D:
	// ch6 plus unused). Muting only chip A left the perimeter mics and ch6,
	// which the wake word and speech-to-text use, physically live.
	MicMuteCtls: []alsa.Control{
		{Index: 105}, {Index: 106}, // ADC_A
		{Index: 123}, {Index: 124}, // ADC_B
		{Index: 141}, {Index: 142}, // ADC_C
		{Index: 159}, {Index: 160}, // ADC_D
	},
	// The 12-LED ring turning green is already the acknowledgement.
	WakeCue: WakeCue{Enabled: false},
	Volume: Volume{
		Selector:    "61",
		DisplayName: "PCM Playback Volume",
	},
	HasMuteButtonLED: true,
	SupportsBLEProxy: true,
	HasLEDRing:       true,
	LEDCount:         12,
	AdcDigitalGainCtls: []alsa.Control{
		{Index: 89}, {Index: 107}, {Index: 125}, {Index: 143},
	},
	AdcMicpgaCtls: []alsa.Control{
		{Index: 92}, {Index: 110}, {Index: 128}, {Index: 146},
	},
	Beamforming:    true,
	StopServices:   []string{"mixer", "ledcontroller", "acebutton"},
	UseALSABackend: false,
}

// checkers is the Echo Show 5 gen 1: a single TLV320AIC3101 feeding the same
// amzn-mt-spi-pcm FPGA path as biscuit but built for 4 channels, an RT5616
// driving a mono speaker, a screen instead of an LED ring.
//
// Every value below was read off the hardware: the driver's own HW_REFINE for
// the PCM parameters, and the Android HAL's mixer state captured at the moment
// it opened the output for the playback settings.
var checkers = Profile{
	Name: "checkers",
	Mic: Mic{
		Card: 0, Device: 22,
		Channels:   4, // channels_min == channels_max, fixed by the driver
		Format:     alsa.FormatS24_3LE,
		SampleRate: 16000,
		PeriodSize: 257, // driver minimum: 3084 bytes / 12 bytes per frame
		// 8 periods = 2056 frames = ~128ms of ring. At 4 periods (~64ms) the
		// capture telemetry logged arrival gaps of 33-51ms during playback, // close enough to the ring depth that a scheduling hiccup would drop
		// whole periods at the hardware with no error surfaced. The driver
		// allows up to 10.
		Periods:     8,
		MicChannels: []int{0, 1},
		// ch2/ch3 carry a bit-identical loopback of the playback signal,
		// resampled to 16 kHz by the FPGA and sample-aligned with the mics.
		// Measured echo path: 2.5 ms delay, -13 dB, 0.83 correlation.
		RefChannels: []int{2, 3},
	},
	Speaker: Speaker{
		Card: 0, Device: 23,
		Channels:   2,
		Format:     alsa.FormatS16LE,
		SampleRate: 48000,
		PeriodSize: 1536, // matches what the Amazon HAL negotiates
		Periods:    2,
	},
	MixerInit: []alsa.Control{
		// Counter-intuitive but load-bearing: the external speaker amp switch
		// must be OFF for audio to reach the speaker. Its boot default is On,
		// which silences output entirely. Found by diffing the mixer against
		// the Android HAL at the instant it opened the output PCM.
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"Off"}},
		// Mic gain, mirroring biscuit's per-ADC settings for the single ADC.
		{Name: "ADC_A Digital Volume Control", Values: []string{"88", "88"}},
		{Name: "ADC_A MICPGA Volume Ctrl", Values: []string{"40", "40"}},
	},
	// Off is the audible state. The name reads backwards: On silences output.
	AmpOn: []alsa.Control{
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"Off"}, Optional: true},
	},
	AmpOff: []alsa.Control{
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"On"}, Optional: true},
	},
	Buttons: Buttons{
		// No action button on a Show 5. /proc/bus/input/devices lists
		// event6 as "gpio-keys", which is volumeup/volumedown on GPIO
		// 393/394 (see checkers.dtsi); event2 is the touchscreen.
		DotDevice:    "",
		VolumeDevice: "/dev/input/event6",
	},
	// The single TLV320AIC3101's left and right mute, which tinymix reports
	// as "ADC_A Left Mute" and "ADC_A Right Mute".
	MicMuteCtls: []alsa.Control{
		{Name: "ADC_A Left Mute"},
		{Name: "ADC_A Right Mute"},
	},
	// No ring on a Show 5, so acknowledge with a chime and a blip of the
	// screen backlight. Without either, you cannot tell the device heard the
	// wake word until it answers, and you end up talking over it.
	WakeCue: WakeCue{
		Enabled:        true,
		ChimeMs:        120,
		ChimeHz:        880,
		ChimeAmplitude: 0.18,
		BacklightPath:  "/sys/class/leds/lcd-backlight/brightness",
		BacklightMax:   255,
	},
	Volume: Volume{
		// The RT5616 DAC1 digital volume, 0..175 like biscuit's control.
		// "OUT Playback Volume" (0..39) is the analogue LOUT gain and is
		// left at its default; attenuating digitally keeps the output
		// stage in the state the HAL uses.
		Selector:    "DAC1 Playback Volume",
		DisplayName: "DAC1 Playback Volume",
	},
	HasMuteButtonLED: false,
	SupportsBLEProxy: false,
	HasLEDRing:       false,
	LEDCount:         0,
	AdcDigitalGainCtls: []alsa.Control{
		{Name: "ADC_A Digital Volume Control"},
	},
	AdcMicpgaCtls: []alsa.Control{
		{Name: "ADC_A MICPGA Volume Ctrl"},
	},
	// Two mics is not an array worth steering.
	Beamforming: false,
	// Nothing. This list must stay empty on LineageOS, and the reason is
	// worth spelling out because the obvious change is catastrophic.
	//
	// Stopping audioserver looks right: it keeps the Android HAL off the
	// PCMs: and it is what the Fire OS profile does with its own services.
	// On LineageOS it bootloops the device. system_server's AudioService
	// makes synchronous binder calls to media.audio_policy, which audioserver
	// provides; with audioserver stopped those calls block forever, Android's
	// Watchdog kills system_server after 60s, and killing system_server
	// reboots the device. The daemon then starts again at boot and repeats,
	// roughly every 75 seconds:
	//
	//   ServiceManager: Waiting for service 'media.audio_policy' on '/dev/binder'
	//   Watchdog: *** WATCHDOG KILLING SYSTEM PROCESS: Blocked in handler on main thread
	//       at android.media.AudioSystem.setA11yServicesUids(Native Method)
	//       at com.android.server.audio.AudioService...
	//
	// Not stopping it is safe: the HAL only opens a PCM when something plays,
	// both PCMs report subdevices_avail: 1 at idle, and this daemon opens them
	// exclusively. If Android does try to play afterwards it simply fails to
	// open the device, which is the intended outcome anyway.
	//
	// If HAL contention ever does become a problem, the fix is to neuter the
	// HAL rather than the service: replace /vendor/etc/audio_policy_configuration.xml
	// with one declaring no primary module, so audioserver stays alive and
	// answers binder calls but never opens a PCM.
	StopServices:   nil,
	UseALSABackend: true,
}

var profiles = map[string]*Profile{
	biscuit.Name:  &biscuit,
	checkers.Name: &checkers,
}

var (
	detectOnce sync.Once
	detected   *Profile
)

// Detect selects a profile from ro.product.device, falling back to biscuit so
// existing deployments behave exactly as before.
//
// Memoised: it shells out to getprop, and several packages ask for the active
// profile independently.
func Detect() *Profile {
	detectOnce.Do(func() { detected = detect() })
	return detected
}

func detect() *Profile { return detectFor(prop("ro.product.device")) }

// detectFor maps a ro.product.device value to a profile. Split out from detect
// so the mapping is testable without a device.
func detectFor(name string) *Profile {
	if p, ok := profiles[name]; ok {
		log.Printf("profile: detected %q", p.Name)
		return p
	}
	if name != "" {
		log.Printf("profile: unknown device %q, falling back to %q", name, biscuit.Name)
	}
	return &biscuit
}

// ByName returns a named profile, or nil if unknown. Used by the -profile
// override so a device can be forced during bring-up.
func ByName(name string) *Profile { return profiles[name] }

// Names lists the supported profiles.
func Names() []string {
	out := make([]string, 0, len(profiles))
	for n := range profiles {
		out = append(out, n)
	}
	return out
}

func prop(key string) string {
	out, err := exec.Command("getprop", key).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
