// Package config provides a shared, concurrency-safe device configuration
// that can be updated at runtime when the controller pushes a config message.
//
// Both the control client (OWW threshold) and the data client (VAD params)
// read from this struct so changes take effect immediately without a restart.
package config

import (
	"encoding/json"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
)

// Device holds all runtime-tunable parameters for this device.
// Zero values are replaced by defaults on first access via Get().
type Device struct {
	mu sync.RWMutex

	// Microphone / VAD
	VadChannel   int
	VadThreshold float64
	VadSpeechMs  int
	VadSilenceMs int

	// Speaker
	StartupVolume int

	// Wake word
	OwwThreshold float64
	OwwModel     string
	// BargeInEnabled / BargeInThreshold mirror the controller's barge-in
	// settings. The device needs them for on-device scoring: while the speaker
	// is streaming, the controller lowers its wake bar to BargeInThreshold
	// (echo at the mic is ~25dB louder than the person, so speech-over-TTS
	// scores are depressed). A device scoring against the normal threshold
	// during playback is not answering the same question, which made every
	// barge-in look like an on-device miss.
	BargeInEnabled   bool
	BargeInThreshold float64
	// DuckDb is how far MUSIC is attenuated while a voice turn plays over
	// it, in dB (negative = quieter). Config rather than a constant because
	// it is a taste parameter that needs iterating in a real room, the same
	// reasoning as the LED meter response curve — not something to discover
	// via a firmware OTA per attempt.
	DuckDb float64
	// OwwOnDevice selects on-device wake word scoring: "off", "shadow" or
	// "on".
	//
	// Shadow scores the wake stream locally and reports what it would have
	// detected, without acting on it, so device and controller can be
	// compared on the same audio. "on" additionally lets the device TRIGGER
	// the turn: the crossing is sent as an oww_wake message and the
	// controller starts the turn on the device's word rather than its own.
	//
	// The controller keeps scoring in "on" mode — its detections no longer
	// trigger, but they still record whether it agreed, so the comparison
	// that justified shipping this keeps running with the roles inverted.
	// It is also what keeps barge-in working unchanged, since that is
	// scored controller-side over the turn's own audio.
	OwwOnDevice string

	// ADC gain — applied via tinymix when config is pushed
	AdcDigitalGain int
	AdcMicpga      int

	// MicGainDb is a fixed digital gain (dB) applied to the full 24-bit
	// capture before quantising to the 16-bit stream (see beamformer
	// extractChannel). Measured speech at normal levels sits at 0.0001–
	// 0.0006 FS RMS — only ~3–20 LSB in 16-bit terms — so gain must be
	// applied pre-truncation to recover real captured resolution rather
	// than amplify 16-bit quantisation noise. Fixed by design: this is
	// the "fixed gain" stage of the dumb-transducer architecture — all
	// adaptation lives controller-side as measurement. 0 = unity.
	MicGainDb int

	// BeamAngle fixes the beamformer steering direction in degrees
	// (0–360, clockwise from 12 o'clock). -1 = auto (track loudest source).
	BeamAngle          float64
	BeamformingEnabled bool

	// AGC toggle — pointer typed so false is expressible over the wire.
	// Defaults true; applies to bounded lockMic turn streams only (forced
	// off on the always-on wake stream). RNNoise NS was removed 2026-07-12 —
	// noise suppression lives controller-side (em_ns.py) on the ASR path.
	AgcEnabled *bool

	// Acoustic echo cancellation (speexdsp, internal/aec). Applies to the
	// whole mic path (wake stream included) — defaults off until validated
	// per deployment. AecDelayMs is the bulk write-to-ear latency the
	// reference stream is shifted by; measured on hardware (2026-07-08)
	// the right value is 0 — the mic side reads whole 160ms ALSA batches
	// (see GetAudioStream), which eats most of the speaker's ≈340ms output
	// buffering, and the filter tail absorbs the remainder. Values ≥100
	// made the echo arrive before its reference (non-causal → zero
	// cancellation). AecTailMs is the adaptive filter length, which must
	// cover residual delay error plus room reverb. Device clamps: delay
	// 0–1000ms, tail 50–500ms.
	AecEnabled *bool
	AecDelayMs int
	AecTailMs  int

	// AecRefSource picks where the far-end reference comes from: "auto",
	// "hw" or "sw".
	//
	// It is an OVERRIDE for the detection, not a statement about the board
	// — the same shape as OwwOnDevice, and config rather than an env var
	// for the same reason. "auto" detects the hardware loopback and falls
	// back to the software tap on a board without one, which is right
	// almost always; "hw" and "sw" pin it, so the two paths can be
	// A/B'd from the dashboard.
	//
	// This started as EM_AEC_HW_REF, on the argument that the reference is
	// a property of the board rather than a user preference. That was
	// wrong: the board property is already DETECTED, and what a person
	// needs to set is which answer to trust — which cannot be a device
	// env var, because changing one means an edit to start_server.sh on
	// the device and a server restart. Making the measurement expensive is
	// how it stays unmeasured.
	AecRefSource string

	// BLE proxy (passive scan over /dev/stpbt, internal/bluetooth) —
	// pointer typed so false is expressible over the wire. Default off.
	BleProxyEnabled *bool

	// ListeningAnim carries the controller's current listening-ring
	// animation spec, raw JSON in the led_anim shape, so the device can
	// light it locally at its OWN wake crossing (#263) instead of waiting
	// a controller round trip for the authoritative frame. Nil until the
	// controller sends one; a device that has never received it simply
	// keeps the old behaviour.
	ListeningAnim json.RawMessage

	initialised bool
}

var global = &Device{}

// Get returns the global device config, initialised from environment
// variables on first call.
func Get() *Device {
	global.mu.Lock()
	defer global.mu.Unlock()
	if !global.initialised {
		global.loadDefaults()
		global.initialised = true
	}
	return global
}

// loadDefaults populates from environment variables, falling back to
// hard-coded defaults. Must be called with mu held.
func (d *Device) loadDefaults() {
	d.VadChannel = envInt("VAD_CHANNEL", 0)
	d.VadThreshold = envFloat("VAD_THRESHOLD", 0.004)
	d.VadSpeechMs = envInt("VAD_SPEECH_MS", 80)
	d.VadSilenceMs = envInt("VAD_SILENCE_MS", 600)
	d.StartupVolume = envInt("STARTUP_VOLUME", 85)
	d.OwwThreshold = envFloat("OWW_THRESHOLD", 0.5)
	d.OwwModel = envStr("OWW_MODEL", "hey_jarvis_v0.1")
	d.OwwOnDevice = normaliseOnDevice(envStr("OWW_ON_DEVICE", OnDeviceOff))
	d.BargeInThreshold = envFloat("BARGE_IN_THRESHOLD", 0.05)
	d.DuckDb = envFloat("DUCK_DB", -18)
	d.AdcDigitalGain = envInt("ADC_DIGITAL_GAIN", 88)
	d.AdcMicpga = envInt("ADC_MICPGA", 40)
	d.MicGainDb = clampMicGainDb(envInt("MIC_GAIN_DB", 24))
	d.BeamAngle = envFloat("BEAM_ANGLE", -1)
	d.BeamformingEnabled = envBool("BEAMFORMING_ENABLED", true)
	agcEnabled := envBool("AGC_ENABLED", true)
	d.AgcEnabled = &agcEnabled
	// true to match em_db.DEFAULT_DEVICE_CONFIG, which now defaults AEC on
	// because barge-in does. The controller's value reaches us on the first
	// config push either way; this only governs the window before it.
	aecEnabled := envBool("AEC_ENABLED", true)
	d.AecEnabled = &aecEnabled
	d.AecDelayMs = envInt("AEC_DELAY_MS", 0)
	d.AecTailMs = envInt("AEC_TAIL_MS", 300)
	// EM_AEC_HW_REF keeps working as the boot default for a device with no
	// controller to push config, and is superseded the moment one does.
	d.AecRefSource = normaliseAecRef(envStr("EM_AEC_HW_REF", AecRefAuto))
	bleProxyEnabled := envBool("BLE_PROXY_ENABLED", false)
	d.BleProxyEnabled = &bleProxyEnabled
}

// Apply updates the config from a controller-pushed config message.
// Only non-zero / non-empty values from the message are applied so that
// a partial config push doesn't zero out unmentioned fields.
func (d *Device) Apply(msg ConfigMessage) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if !d.initialised {
		d.loadDefaults()
		d.initialised = true
	}

	if msg.VadThreshold > 0 {
		d.VadThreshold = msg.VadThreshold
	}
	if msg.VadSpeechMs > 0 {
		d.VadSpeechMs = msg.VadSpeechMs
	}
	if msg.VadSilenceMs > 0 {
		d.VadSilenceMs = msg.VadSilenceMs
	}
	if msg.OwwThreshold > 0 {
		d.OwwThreshold = msg.OwwThreshold
	}
	if msg.OwwModel != "" {
		d.OwwModel = msg.OwwModel
	}
	if msg.OwwOnDevice != "" {
		d.OwwOnDevice = normaliseOnDevice(msg.OwwOnDevice)
	}
	if msg.BargeInEnabled != nil {
		d.BargeInEnabled = *msg.BargeInEnabled
	}
	if msg.BargeInThreshold > 0 {
		d.BargeInThreshold = msg.BargeInThreshold
	}
	// Negative-going, so the usual "non-zero means set" rule is inverted:
	// a duck of 0dB is a legitimate setting ("do not duck at all") and must
	// be distinguishable from an absent field, hence the pointer.
	if msg.DuckDb != nil {
		d.DuckDb = *msg.DuckDb
	}
	if msg.StartupVolume > 0 {
		d.StartupVolume = msg.StartupVolume
	}
	if msg.AdcDigitalGain > 0 {
		d.AdcDigitalGain = msg.AdcDigitalGain
	}
	if msg.AdcMicpga > 0 {
		d.AdcMicpga = msg.AdcMicpga
	}
	if msg.MicGainDb != nil {
		d.MicGainDb = clampMicGainDb(*msg.MicGainDb)
	}
	if msg.BeamAngle != nil {
		d.BeamAngle = *msg.BeamAngle
	}
	if msg.BeamformingEnabled != nil {
		d.BeamformingEnabled = *msg.BeamformingEnabled
	}
	if msg.AgcEnabled != nil {
		d.AgcEnabled = msg.AgcEnabled
	}
	if msg.AecEnabled != nil {
		d.AecEnabled = msg.AecEnabled
	}
	if msg.AecDelayMs != nil {
		d.AecDelayMs = *msg.AecDelayMs
	}
	if msg.AecTailMs > 0 {
		d.AecTailMs = msg.AecTailMs
	}
	if msg.AecRefSource != "" {
		d.AecRefSource = normaliseAecRef(msg.AecRefSource)
	}
	if msg.BleProxyEnabled != nil {
		d.BleProxyEnabled = msg.BleProxyEnabled
	}
	if msg.ListeningAnim != nil {
		d.ListeningAnim = msg.ListeningAnim
	}
}

// Snapshot returns a consistent copy of all config values.
func (d *Device) Snapshot() ConfigMessage {
	d.mu.RLock()
	defer d.mu.RUnlock()
	beamAngle := d.BeamAngle
	// C4 fix (2026-07-05 review): previously &d.BeamformingEnabled leaked a
	// pointer into the live mutex-guarded struct — the caller (streamMic,
	// every period) dereferences it after RUnlock, racing with Apply()
	// writing the same bool on a config push. Copy to a local like
	// beamAngle/agcEnabled above.
	beamformingEnabled := d.BeamformingEnabled
	// Same reason as beamformingEnabled above: copy, never point into the
	// mutex-guarded struct.
	bargeInEnabled := d.BargeInEnabled
	agcEnabled := true
	if d.AgcEnabled != nil {
		agcEnabled = *d.AgcEnabled
	}
	micGainDb := d.MicGainDb
	aecEnabled := false
	if d.AecEnabled != nil {
		aecEnabled = *d.AecEnabled
	}
	aecDelayMs := d.AecDelayMs
	bleProxyEnabled := false
	if d.BleProxyEnabled != nil {
		bleProxyEnabled = *d.BleProxyEnabled
	}
	return ConfigMessage{
		VadThreshold:       d.VadThreshold,
		VadSpeechMs:        d.VadSpeechMs,
		VadSilenceMs:       d.VadSilenceMs,
		OwwThreshold:       d.OwwThreshold,
		OwwModel:           d.OwwModel,
		OwwOnDevice:        d.OwwOnDevice,
		BargeInEnabled:     &bargeInEnabled,
		BargeInThreshold:   d.BargeInThreshold,
		StartupVolume:      d.StartupVolume,
		AdcDigitalGain:     d.AdcDigitalGain,
		AdcMicpga:          d.AdcMicpga,
		MicGainDb:          &micGainDb,
		BeamAngle:          &beamAngle,
		BeamformingEnabled: &beamformingEnabled,
		AgcEnabled:         &agcEnabled,
		AecEnabled:         &aecEnabled,
		AecDelayMs:         &aecDelayMs,
		AecTailMs:          d.AecTailMs,
		AecRefSource:       d.AecRefSource,
		BleProxyEnabled:    &bleProxyEnabled,
		ListeningAnim:      d.ListeningAnim,
	}
}

// ConfigMessage mirrors the JSON shape of the config control message
// sent by the controller. JSON tags must match em_controller.py exactly.
type ConfigMessage struct {
	Type               string   `json:"type,omitempty"`
	AdcDigitalGain     int      `json:"adcDigitalGain,omitempty"`
	AdcMicpga          int      `json:"adcMicpga,omitempty"`
	MicGainDb          *int     `json:"micGainDb,omitempty"`
	StartupVolume      int      `json:"startupVolume,omitempty"`
	VadThreshold       float64  `json:"vadThreshold,omitempty"`
	VadSpeechMs        int      `json:"vadSpeechMs,omitempty"`
	VadSilenceMs       int      `json:"vadSilenceMs,omitempty"`
	OwwThreshold       float64  `json:"owwThreshold,omitempty"`
	OwwModel           string   `json:"owwModel,omitempty"`
	OwwOnDevice        string   `json:"owwOnDevice,omitempty"`
	BargeInEnabled     *bool    `json:"bargeInEnabled,omitempty"`
	BargeInThreshold   float64  `json:"bargeInThreshold,omitempty"`
	DuckDb             *float64 `json:"duckDb,omitempty"`
	BeamAngle          *float64 `json:"beamAngle,omitempty"`
	BeamformingEnabled *bool    `json:"beamformingEnabled,omitempty"`
	HasBeamforming     bool     `json:"hasBeamforming,omitempty"`
	AgcEnabled         *bool    `json:"agcEnabled,omitempty"`
	AecEnabled         *bool    `json:"aecEnabled,omitempty"`
	AecDelayMs         *int     `json:"aecDelayMs,omitempty"`
	AecTailMs          int      `json:"aecTailMs,omitempty"`
	AecRefSource       string   `json:"aecRefSource,omitempty"`
	BleProxyEnabled    *bool    `json:"bleProxyEnabled,omitempty"`

	// ListeningAnim: raw led_anim spec for the listening ring (#263).
	// Carried as raw JSON so this package does not depend on the
	// animation renderer's types.
	ListeningAnim json.RawMessage `json:"listeningAnim,omitempty"`
}

// clampMicGainDb bounds the fixed mic gain to a sane range: 0dB (unity —
// the pre-gain behaviour, bit-exact) up to +42dB. The 24-bit capture holds
// 8 bits (48dB) below the old 16-bit truncation point; beyond +42dB the
// gain is amplifying the capture's own noise floor with no headroom left.
func clampMicGainDb(db int) int {
	if db < 0 {
		return 0
	}
	if db > 42 {
		return 42
	}
	return db
}

// On-device wake word modes.
const (
	OnDeviceOff    = "off"
	OnDeviceShadow = "shadow"
	OnDeviceOn     = "on"

	// AecRefSource values. "auto" detects the hardware loopback and falls
	// back to the software tap; the other two pin it for an A/B.
	AecRefAuto = "auto"
	AecRefHW   = "hw"
	AecRefSW   = "sw"
)

// normaliseOnDevice maps a pushed value onto a known mode. Anything
// unrecognised becomes "off": a device receiving a mode it cannot honour must
// not guess, because the two plausible guesses are "score but do nothing" and
// "start triggering turns", and one of those is a live behaviour change on a
// device that cannot deliver it.
//
// That rule is why firmware predating "on" is safe to leave in the field: it
// normalises the value away and keeps scoring in shadow. The controller does
// not rely on that — it gates the setting on the oww_trigger capability — but
// the device must not depend on the controller being careful.
func normaliseOnDevice(v string) string {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case OnDeviceShadow:
		return OnDeviceShadow
	case OnDeviceOn:
		return OnDeviceOn
	case "", OnDeviceOff:
		return OnDeviceOff
	default:
		log.Printf("[config] unknown owwOnDevice %q — treating as %q", v, OnDeviceOff)
		return OnDeviceOff
	}
}

// normaliseAecRef keeps an unknown value on the DETECTING path rather than
// pinning one. A typo that pinned "sw" would silently disable the hardware
// reference on every device it reached, and read as the feature not working.
func normaliseAecRef(v string) string {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case AecRefHW, "on", "true", "1":
		return AecRefHW
	case AecRefSW, "off", "false", "0":
		return AecRefSW
	case "", AecRefAuto:
		return AecRefAuto
	default:
		log.Printf("[config] unknown aecRefSource %q — detecting", v)
		return AecRefAuto
	}
}

// ─── env helpers ──────────────────────────────────────────────────────────────

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

func envBool(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		return v == "1" || v == "true" || v == "True"
	}
	return def
}

func envStr(key string, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
