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
	VadChannel    int
	VadThreshold  float64
	VadSpeechMs   int
	VadSilenceMs  int

	// Speaker
	StartupVolume int

	// Wake word
	OwwThreshold float64
	OwwModel     string

	// ADC gain — applied via tinymix when config is pushed
	AdcDigitalGain int
	AdcMicpga      int

	// BeamAngle fixes the beamformer steering direction in degrees
	// (0–360, clockwise from 12 o'clock). -1 = auto (track loudest source).
	BeamAngle          float64
	BeamformingEnabled bool

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
	d.VadChannel    = envInt("VAD_CHANNEL", 0)
	d.VadThreshold  = envFloat("VAD_THRESHOLD", 0.004)
	d.VadSpeechMs   = envInt("VAD_SPEECH_MS", 80)
	d.VadSilenceMs  = envInt("VAD_SILENCE_MS", 600)
	d.StartupVolume = envInt("STARTUP_VOLUME", 85)
	d.OwwThreshold  = envFloat("OWW_THRESHOLD", 0.3)
	d.OwwModel      = envStr("OWW_MODEL", "hey_jarvis_v0.1")
	d.AdcDigitalGain    = envInt("ADC_DIGITAL_GAIN", 88)
	d.AdcMicpga         = envInt("ADC_MICPGA", 40)
	d.BeamAngle         = envFloat("BEAM_ANGLE", -1)
	d.BeamformingEnabled = envBool("BEAMFORMING_ENABLED", true)
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
	if msg.BeamAngle != 0 {
		d.BeamAngle = msg.BeamAngle
	}
	if msg.BeamformingEnabled != nil {
		d.BeamformingEnabled = *msg.BeamformingEnabled
	}
}

// Snapshot returns a consistent copy of all config values.
func (d *Device) Snapshot() ConfigMessage {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return ConfigMessage{
		VadThreshold:        d.VadThreshold,
		VadSpeechMs:         d.VadSpeechMs,
		VadSilenceMs:        d.VadSilenceMs,
		OwwThreshold:        d.OwwThreshold,
		OwwModel:            d.OwwModel,
		StartupVolume:       d.StartupVolume,
		AdcDigitalGain:      d.AdcDigitalGain,
		AdcMicpga:           d.AdcMicpga,
		BeamAngle:           d.BeamAngle,
		BeamformingEnabled:  &d.BeamformingEnabled,
	}
}

// ConfigMessage mirrors the JSON shape of the config control message
// sent by the controller. JSON tags must match em_controller.py exactly.
type ConfigMessage struct {
	Type               string  `json:"type,omitempty"`
	AdcDigitalGain     int     `json:"adcDigitalGain,omitempty"`
	AdcMicpga          int     `json:"adcMicpga,omitempty"`
	StartupVolume      int     `json:"startupVolume,omitempty"`
	VadThreshold       float64 `json:"vadThreshold,omitempty"`
	VadSpeechMs        int     `json:"vadSpeechMs,omitempty"`
	VadSilenceMs       int     `json:"vadSilenceMs,omitempty"`
	OwwThreshold       float64 `json:"owwThreshold,omitempty"`
	OwwModel           string  `json:"owwModel,omitempty"`
	BeamAngle           float64  `json:"beamAngle,omitempty"`
	BeamformingEnabled  *bool    `json:"beamformingEnabled,omitempty"`
	HasBeamforming      bool     `json:"hasBeamforming,omitempty"`
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
