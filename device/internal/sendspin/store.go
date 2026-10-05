package sendspin

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// maxRecords is how many servers the device stays paired with at once. The
// spec's floor is 5, and a pairing never fails for want of room: the least
// recently used record goes.
const maxRecords = 8

// The persisted state: identity, pairing secrets, and the player settings the
// spec asks to survive a reboot (static delay, volume, mute). Secrets live
// here and nowhere else — never in logs, stats or the support bundle.
type storeFile struct {
	PrivateKey string       `json:"privateKey"`
	PairingPSK string       `json:"pairingPsk"`
	Records    []pairRecord `json:"records,omitempty"`
	// LastPlayback is the server that last held a playback connection; it
	// wins a tie between two connections that declare nothing.
	LastPlayback  string `json:"lastPlayback,omitempty"`
	StaticDelayMs int    `json:"staticDelayMs"`
	Volume        int    `json:"volume"`
	Muted         bool   `json:"muted"`
}

type pairRecord struct {
	ServerID string `json:"serverId"`
	PSK      string `json:"psk"`
	LastUsed int64  `json:"lastUsed"`
}

// store is the device's Sendspin state on disk.
type store struct {
	path string
	mu   sync.Mutex
	f    storeFile
	id   identity
	pp   []byte // pairing PSK
}

// openStore loads the state, creating a fresh identity and pairing PSK the
// first time. A corrupt file is an error rather than a silent reset: resetting
// would change the client_id and unpair the device from every server.
func openStore(path string) (*store, error) {
	s := &store{path: path}
	b, err := os.ReadFile(path)
	switch {
	case err == nil:
		if err := json.Unmarshal(b, &s.f); err != nil {
			return nil, err
		}
	case errors.Is(err, os.ErrNotExist):
		s.f.Volume = 100
	default:
		return nil, err
	}

	dirty := false
	if s.f.PrivateKey == "" {
		id, err := newIdentity()
		if err != nil {
			return nil, err
		}
		s.f.PrivateKey, dirty = b64u(id.priv), true
	}
	if s.f.PairingPSK == "" {
		p, err := randomPSK()
		if err != nil {
			return nil, err
		}
		s.f.PairingPSK, dirty = b64u(p), true
	}
	priv, err := unb64u(s.f.PrivateKey)
	if err != nil {
		return nil, err
	}
	if s.id, err = identityFrom(priv); err != nil {
		return nil, err
	}
	if s.pp, err = unb64u(s.f.PairingPSK); err != nil || len(s.pp) != pskSize {
		return nil, errors.New("sendspin: stored pairing PSK is malformed")
	}
	if dirty {
		if err := s.saveLocked(); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *store) saveLocked() error {
	b, err := json.MarshalIndent(&s.f, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// pskCategory is which kind of PSK a handshake matched, which decides what
// the server may do on the connection.
type pskCategory int

const (
	catSentinel pskCategory = iota
	catPairing
	catLongTerm
)

// resolvedPSK is the answer to "which PSK does this psk_id name".
type resolvedPSK struct {
	psk      []byte
	cat      pskCategory
	serverID string // bound server, long-term only
}

// resolve finds the PSK a server named in Noise message 1. A miss falls back
// to the Sentinel: the server then sees a credential mismatch it can offer to
// re-pair, which is better than a connection that silently never completes.
func (s *store) resolve(id string) resolvedPSK {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, r := range s.f.Records {
		if p, err := unb64u(r.PSK); err == nil && pskID(p) == id {
			return resolvedPSK{psk: p, cat: catLongTerm, serverID: r.ServerID}
		}
	}
	if pskID(s.pp) == id {
		return resolvedPSK{psk: s.pp, cat: catPairing}
	}
	return resolvedPSK{psk: sentinelPSK, cat: catSentinel}
}

// addRecord persists a new long-term PSK for a server, replacing any it
// already had and evicting the least recently used record past capacity —
// never one a live connection is using.
func (s *store) addRecord(serverID string, psk []byte, inUse func(string) bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.f.Records[:0]
	for _, r := range s.f.Records {
		if r.ServerID != serverID {
			out = append(out, r)
		}
	}
	out = append(out, pairRecord{ServerID: serverID, PSK: b64u(psk), LastUsed: time.Now().Unix()})
	if len(out) > maxRecords {
		sort.SliceStable(out, func(i, j int) bool { return out[i].LastUsed < out[j].LastUsed })
		for i := range out {
			if !inUse(out[i].ServerID) && out[i].ServerID != serverID {
				out = append(out[:i], out[i+1:]...)
				break
			}
		}
	}
	s.f.Records = out
	return s.saveLocked()
}

func (s *store) removeRecord(serverID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.f.Records[:0]
	for _, r := range s.f.Records {
		if r.ServerID != serverID {
			out = append(out, r)
		}
	}
	s.f.Records = out
	return s.saveLocked()
}

func (s *store) touch(serverID string, playback bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.f.Records {
		if s.f.Records[i].ServerID == serverID {
			s.f.Records[i].LastUsed = time.Now().Unix()
		}
	}
	if playback && s.f.LastPlayback != serverID {
		s.f.LastPlayback = serverID
		s.saveLocked()
	}
}

func (s *store) lastPlayback() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.f.LastPlayback
}

func (s *store) paired() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := make([]string, 0, len(s.f.Records))
	for _, r := range s.f.Records {
		ids = append(ids, r.ServerID)
	}
	return ids
}

// playerSettings are the three values the spec asks a player to persist.
type playerSettings struct {
	StaticDelayMs int
	Volume        int
	Muted         bool
}

func (s *store) settings() playerSettings {
	s.mu.Lock()
	defer s.mu.Unlock()
	return playerSettings{s.f.StaticDelayMs, s.f.Volume, s.f.Muted}
}

func (s *store) setSettings(p playerSettings) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if p == (playerSettings{s.f.StaticDelayMs, s.f.Volume, s.f.Muted}) {
		return
	}
	s.f.StaticDelayMs, s.f.Volume, s.f.Muted = p.StaticDelayMs, p.Volume, p.Muted
	s.saveLocked()
}

func (s *store) token() string { return pairingToken(s.id.pub, s.pp) }
