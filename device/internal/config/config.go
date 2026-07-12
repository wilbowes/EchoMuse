// Package config provides a shared, concurrency-safe device configuration
// that can be updated at runtime when the controller pushes a config message.
//
// Both the control client (OWW threshold) and the data client (VAD params)
// read from this struct so changes take effect immediately without a restart.
package config

import (
	"os"
	"strconv"
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

	// BLE proxy (passive scan over /dev/stpbt, internal/bluetooth) —
	// pointer typed so false is expressible over the wire. Default off.
	BleProxyEnabled *bool

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
	d.OwwThreshold = envFloat("OWW_THRESHOLD", 0.3)
	d.OwwModel = envStr("OWW_MODEL", "hey_jarvis_v0.1")
	d.AdcDigitalGain = envInt("ADC_DIGITAL_GAIN", 88)
	d.AdcMicpga = envInt("ADC_MICPGA", 40)
	d.MicGainDb = clampMicGainDb(envInt("MIC_GAIN_DB", 24))
	d.BeamAngle = envFloat("BEAM_ANGLE", -1)
	d.BeamformingEnabled = envBool("BEAMFORMING_ENABLED", true)
	agcEnabled := envBool("AGC_ENABLED", true)
	d.AgcEnabled = &agcEnabled
	aecEnabled := envBool("AEC_ENABLED", false)
	d.AecEnabled = &aecEnabled
	d.AecDelayMs = envInt("AEC_DELAY_MS", 0)
	d.AecTailMs = envInt("AEC_TAIL_MS", 300)
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
	if msg.BleProxyEnabled != nil {
		d.BleProxyEnabled = msg.BleProxyEnabled
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
		BleProxyEnabled:    &bleProxyEnabled,
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
	BeamAngle          *float64 `json:"beamAngle,omitempty"`
	BeamformingEnabled *bool    `json:"beamformingEnabled,omitempty"`
	HasBeamforming     bool     `json:"hasBeamforming,omitempty"`
	AgcEnabled         *bool    `json:"agcEnabled,omitempty"`
	AecEnabled         *bool    `json:"aecEnabled,omitempty"`
	AecDelayMs         *int     `json:"aecDelayMs,omitempty"`
	AecTailMs          int      `json:"aecTailMs,omitempty"`
	BleProxyEnabled    *bool    `json:"bleProxyEnabled,omitempty"`
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
