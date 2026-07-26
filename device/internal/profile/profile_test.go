package profile

import (
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/alsa"
)

// all returns every registered profile, so invariants are checked against any
// device added later rather than just the two that exist today.
func all(t *testing.T) []*Profile {
	t.Helper()
	out := make([]*Profile, 0, len(profiles))
	for _, p := range profiles {
		out = append(out, p)
	}
	if len(out) == 0 {
		t.Fatal("no profiles registered")
	}
	return out
}

// TestNeverStopAudioserver is a regression guard for a bug that bootlooped a
// device.
//
// checkers originally listed audioserver and vendor.audio-hal in StopServices,
// mirroring what the biscuit profile does with its Fire OS equivalents, to keep
// Android's audio HAL off the PCMs. On LineageOS that is fatal: system_server
// makes synchronous binder calls into media.audio_policy, which audioserver
// provides, so with it stopped those calls block forever, Android's Watchdog
// kills system_server after 60s, and killing system_server reboots the device.
// Started from init, the daemon then repeats it roughly every 75 seconds.
//
// Stopping any Android audio service is therefore never correct here.
func TestNeverStopAudioserver(t *testing.T) {
	banned := []string{"audioserver", "audio-hal", "audio_hal", "media.audio_policy"}
	for _, p := range all(t) {
		for _, svc := range p.StopServices {
			for _, b := range banned {
				if strings.Contains(svc, b) {
					t.Errorf("profile %q stops %q, see the comment on this test; "+
						"this bootloops the device on LineageOS", p.Name, svc)
				}
			}
		}
	}
}

// TestChannelRolesInRange catches a channel index pointing past the end of the
// capture stream, which would read another channel's samples or run off the
// period buffer.
func TestChannelRolesInRange(t *testing.T) {
	for _, p := range all(t) {
		for _, ch := range p.Mic.MicChannels {
			if ch < 0 || ch >= p.Mic.Channels {
				t.Errorf("%s: mic channel %d outside 0..%d", p.Name, ch, p.Mic.Channels-1)
			}
		}
		for _, ch := range p.Mic.RefChannels {
			if ch < 0 || ch >= p.Mic.Channels {
				t.Errorf("%s: AEC ref channel %d outside 0..%d", p.Name, ch, p.Mic.Channels-1)
			}
		}
		// Mic and reference channels must be disjoint.
		for _, m := range p.Mic.MicChannels {
			for _, r := range p.Mic.RefChannels {
				if m == r {
					t.Errorf("%s: channel %d listed as both mic and AEC reference", p.Name, m)
				}
			}
		}
	}
}

func TestFrameBytes(t *testing.T) {
	for _, p := range all(t) {
		if got, want := p.Mic.FrameBytes(), p.Mic.Channels*p.Mic.Format.Bytes(); got != want {
			t.Errorf("%s: mic FrameBytes = %d, want %d", p.Name, got, want)
		}
		if p.Mic.Format.Bytes() == 0 {
			t.Errorf("%s: mic format %v has no known sample width", p.Name, p.Mic.Format)
		}
		if p.Speaker.Format.Bytes() == 0 {
			t.Errorf("%s: speaker format %v has no known sample width", p.Name, p.Speaker.Format)
		}
	}
}

// TestPeriodSizeIsUsable catches a period size of zero, which would make the
// read loop spin on empty buffers.
func TestPeriodSizeIsUsable(t *testing.T) {
	for _, p := range all(t) {
		if p.Mic.PeriodSize <= 0 || p.Mic.Periods <= 0 {
			t.Errorf("%s: mic period %d x %d", p.Name, p.Mic.PeriodSize, p.Mic.Periods)
		}
		if p.Speaker.PeriodSize <= 0 || p.Speaker.Periods <= 0 {
			t.Errorf("%s: speaker period %d x %d", p.Name, p.Speaker.PeriodSize, p.Speaker.Periods)
		}
		if p.Mic.SampleRate <= 0 || p.Speaker.SampleRate <= 0 {
			t.Errorf("%s: zero sample rate", p.Name)
		}
	}
}

// TestMicMuteControlsConfigured guards a privacy-relevant failure: writing
// another device's mute indices reconfigures unrelated codec state and leaves
// the microphone live.
func TestMicMuteControlsConfigured(t *testing.T) {
	for _, p := range all(t) {
		if len(p.MicMuteCtls) == 0 {
			t.Errorf("%s: no mic mute controls; muting would silently do nothing", p.Name)
		}
		for _, c := range p.MicMuteCtls {
			if c.Name == "" && c.Index == 0 {
				t.Errorf("%s: a mic mute control identifies nothing", p.Name)
			}
		}
	}
}

func TestVolumeControlConfigured(t *testing.T) {
	for _, p := range all(t) {
		if p.Volume.Selector == "" {
			t.Errorf("%s: no volume control selector", p.Name)
		}
		if p.Volume.DisplayName == "" {
			t.Errorf("%s: no volume display name, readFromDevice parses tinymix "+
				"output by this prefix and would silently fall back to half scale", p.Name)
		}
	}
}

// TestLEDConsistency keeps HasLEDRing and LEDCount from disagreeing: callers
// use one to decide whether to paint and the other to size the frame.
func TestLEDConsistency(t *testing.T) {
	for _, p := range all(t) {
		if p.HasLEDRing && p.LEDCount == 0 {
			t.Errorf("%s: HasLEDRing but LEDCount 0", p.Name)
		}
		if !p.HasLEDRing && p.LEDCount != 0 {
			t.Errorf("%s: no LED ring but LEDCount %d", p.Name, p.LEDCount)
		}
		// A device with a ring does not need the chime/backlight cue, and one
		// without it does, otherwise it gives no wake feedback at all.
		if p.HasLEDRing == p.WakeCue.Enabled {
			t.Errorf("%s: HasLEDRing=%v and WakeCue.Enabled=%v, exactly one form "+
				"of wake feedback should be active", p.Name, p.HasLEDRing, p.WakeCue.Enabled)
		}
	}
}

func TestHasAECReference(t *testing.T) {
	if !checkers.HasAECReference() {
		t.Error("checkers should report a hardware AEC reference (ch2/ch3)")
	}
	if biscuit.HasAECReference() {
		t.Error("biscuit has no loopback channels and should report none")
	}
}

func TestByNameAndNames(t *testing.T) {
	for _, name := range Names() {
		if ByName(name) == nil {
			t.Errorf("Names() lists %q but ByName returns nil", name)
		}
	}
	if ByName("no-such-device") != nil {
		t.Error("ByName on an unknown device should return nil")
	}
	// The map key must match the profile's own Name, since Detect looks up by
	// ro.product.device and callers print p.Name.
	for name, p := range profiles {
		if p.Name != name {
			t.Errorf("profile registered as %q but Name is %q", name, p.Name)
		}
	}
}

// TestUnknownDeviceFallsBackToBiscuit keeps existing deployments working: a
// device this build has never heard of must behave exactly as before.
func TestUnknownDeviceFallsBackToBiscuit(t *testing.T) {
	if got := detectFor("some-unreleased-echo"); got.Name != biscuit.Name {
		t.Errorf("unknown device selected %q, want fallback to %q", got.Name, biscuit.Name)
	}
	if got := detectFor(""); got.Name != biscuit.Name {
		t.Errorf("empty ro.product.device selected %q, want %q", got.Name, biscuit.Name)
	}
	if got := detectFor("checkers"); got.Name != "checkers" {
		t.Errorf("known device selected %q, want checkers", got.Name)
	}
}

// TestCheckersMeasuredValues pins the constants that were read off the
// hardware. They are not guesses and should not be "tidied" without a device
// in hand, see docs/checkers-port.md.
func TestCheckersMeasuredValues(t *testing.T) {
	m := checkers.Mic
	if m.Card != 0 || m.Device != 22 {
		t.Errorf("mic PCM = card %d device %d, want card 0 device 22", m.Card, m.Device)
	}
	if m.Format != alsa.FormatS24_3LE {
		t.Errorf("mic format = %v, want S24_3LE (the only format the driver accepts)", m.Format)
	}
	if m.Channels != 4 {
		t.Errorf("mic channels = %d, want 4 (channels_min == channels_max)", m.Channels)
	}
	// 257 frames x 12 bytes = 3084, the driver's minimum period.
	if m.PeriodSize%257 != 0 {
		t.Errorf("period size %d is not a multiple of the driver minimum 257", m.PeriodSize)
	}
	if m.Periods > 10 {
		t.Errorf("periods = %d, driver maximum is 10", m.Periods)
	}
	// 4 periods (~64 ms) logged arrival gaps of 33 to 51 ms during playback,
	// close enough to the ring depth that a scheduling hiccup drops whole
	// periods at the hardware with nothing surfaced.
	if m.Periods < 8 {
		t.Errorf("periods = %d, want at least 8 (~128 ms of ring); 4 was "+
			"measured as too shallow during playback", m.Periods)
	}

	// Ext_Speaker_Amp_Switch must be driven Off to get audio out. Its boot
	// default is On, which silences the speaker entirely.
	var found bool
	for _, c := range checkers.MixerInit {
		if c.Name == "Ext_Speaker_Amp_Switch" {
			found = true
			if len(c.Values) != 1 || c.Values[0] != "Off" {
				t.Errorf("Ext_Speaker_Amp_Switch set to %v, want [Off]: On silences output", c.Values)
			}
		}
	}
	if !found {
		t.Error("MixerInit does not set Ext_Speaker_Amp_Switch; the speaker will be silent")
	}
}

// TestCapabilitiesMatchHardware keeps the device honest with the controller.
// The controller gates work on these: led_anim_capable decides whether it
// hands the ring to a local animation engine, and a device with no ring would
// otherwise spin a ticker rendering frames for zero LEDs.
func TestCapabilitiesMatchHardware(t *testing.T) {
	for _, p := range all(t) {
		caps := p.Capabilities()
		has := func(c string) bool {
			for _, v := range caps {
				if v == c {
					return true
				}
			}
			return false
		}

		// Always true of both devices.
		for _, c := range []string{"mic", "speaker", "buttons"} {
			if !has(c) {
				t.Errorf("%s: missing capability %q", p.Name, c)
			}
		}

		// "leds" stays on even without a ring: the device consumes the
		// listening hint to drive its wake cue. Dropping it would take the
		// cue's trigger away.
		if !has("leds") {
			t.Errorf("%s: must advertise \"leds\": the listening hint drives the wake cue", p.Name)
		}

		if got := has("led_anim"); got != p.HasLEDRing {
			t.Errorf("%s: led_anim=%v but HasLEDRing=%v", p.Name, got, p.HasLEDRing)
		}
		// Nothing advertises beamforming: no controller reads it, and adding it
		// would change the registration payload of existing devices.
		if has("beamforming") {
			t.Errorf("%s: advertises beamforming, which no controller consumes", p.Name)
		}
	}
}
