package sendspin

import "encoding/json"

// envelope is every JSON message: a type and a payload.
type envelope struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

func marshalMsg(typ string, payload any) ([]byte, error) {
	p, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return json.Marshal(envelope{Type: typ, Payload: p})
}

// ── cleartext handshake ─────────────────────────────────────────────────────

type clientInit struct {
	ClientID string `json:"client_id"`
	Version  int    `json:"version"`
	Suite    string `json:"suite"`
}

type serverInit struct {
	ServerID string `json:"server_id"`
	Version  int    `json:"version"`
}

type serverError struct {
	Reason string `json:"reason"`
}

type noiseHandshake struct {
	Data string `json:"data"`
}

// ── client → server ─────────────────────────────────────────────────────────

type deviceInfo struct {
	ProductName     string `json:"product_name,omitempty"`
	Manufacturer    string `json:"manufacturer,omitempty"`
	SoftwareVersion string `json:"software_version,omitempty"`
}

type audioFormat struct {
	Codec      string `json:"codec"`
	Channels   int    `json:"channels"`
	SampleRate int    `json:"sample_rate"`
	BitDepth   int    `json:"bit_depth"`
}

type playerSupport struct {
	SupportedFormats []audioFormat `json:"supported_formats"`
	BufferCapacity   int           `json:"buffer_capacity"`
	// 9.1.1: required here, and only "volume"/"mute" are valid. The current
	// spec moved it into client/state.
	SupportedCommands []string `json:"supported_commands"`
}

// pairMethod is one entry of supported_pair_methods. 9.1.1: a LIST of
// {method, ...}; the current spec has an object keyed by method. 9.1.1
// rejects the object form, so the list is what gets sent.
type pairMethod struct {
	Method string `json:"method"`
}

type unpairedAccess struct {
	Enabled bool `json:"enabled"`
}

type clientHello struct {
	Name                 string         `json:"name"`
	SupportedRoles       []string       `json:"supported_roles"`
	DeviceInfo           *deviceInfo    `json:"device_info,omitempty"`
	PlayerSupport        *playerSupport `json:"player@v1_support,omitempty"`
	SupportedPairMethods []pairMethod   `json:"supported_pair_methods"`
	UnpairedAccess       unpairedAccess `json:"unpaired_access"`
}

type playerState struct {
	Volume            int      `json:"volume"`
	Muted             bool     `json:"muted"`
	StaticDelayMs     int      `json:"static_delay_ms"`
	RequiredLeadMs    int      `json:"required_lead_time_ms"`
	MinBufferMs       int      `json:"min_buffer_ms"`
	SupportedCommands []string `json:"supported_commands"`
}

type clientState struct {
	Available bool         `json:"available"`
	Player    *playerState `json:"player,omitempty"`
}

// streamRequestFormat asks the server to change the stream's format. Every
// field of the player request is optional in the spec; all four are sent, so
// the request names one entry of supported_formats exactly.
type streamRequestFormat struct {
	Player *audioFormat `json:"player"`
}

type clientTime struct {
	ClientTransmitted int64 `json:"client_transmitted"`
}

type clientGoodbye struct {
	Reason string `json:"reason"`
}

type pairFinalize struct {
	LongTermPSK string `json:"long_term_psk"`
}

type pairAbort struct {
	Reason string `json:"reason"`
}

// ── server → client ─────────────────────────────────────────────────────────

type serverHello struct {
	Name string `json:"name"`
}

type activatePairing struct {
	Method string `json:"method"`
}

type serverActivate struct {
	Activities  []string         `json:"activities"`
	ActiveRoles *[]string        `json:"active_roles"`
	Pairing     *activatePairing `json:"pairing"`
}

type serverTime struct {
	ClientTransmitted int64 `json:"client_transmitted"`
	ServerReceived    int64 `json:"server_received"`
	ServerTransmitted int64 `json:"server_transmitted"`
}

type streamStart struct {
	Player *streamFormat `json:"player"`
}

type streamRoles struct {
	Roles []string `json:"roles"`
}

// covers reports whether a stream/clear or stream/end applies to the player.
// Omitted roles means every stream.
func (r streamRoles) covers(role string) bool {
	if len(r.Roles) == 0 {
		return true
	}
	for _, x := range r.Roles {
		if x == role {
			return true
		}
	}
	return false
}

type groupUpdate struct {
	PlaybackState string `json:"playback_state"`
	GroupID       string `json:"group_id"`
	GroupName     string `json:"group_name"`
}

type playerCommand struct {
	Command string `json:"command"`
	Volume  *int   `json:"volume"`
	Mute    *bool  `json:"mute"`
	// 9.1.1 names it static_delay_ms with command set_static_delay; the
	// current spec output_delay_ms with set_output_delay. Both are taken.
	StaticDelayMs *int `json:"static_delay_ms"`
	OutputDelayMs *int `json:"output_delay_ms"`
}

type serverCommand struct {
	Player *playerCommand `json:"player"`
}
