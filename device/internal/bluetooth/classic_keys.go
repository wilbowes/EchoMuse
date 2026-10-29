package bluetooth

// The speakers this Echo has paired with, and their link keys.
//
// A link key is the whole of a BR/EDR pairing: without it the speaker has to
// be put back into pairing mode. So it is kept on /data, written only when a
// pairing creates or replaces one (eMMC wear), and never logged.

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"sort"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
)

// KeyPath is where paired speakers are kept, beside the link's credentials.
const KeyPath = "/data/local/etc/echomuse/bt_paired.json"

// maxPaired bounds the file; the least recently used pairing goes first.
const maxPaired = 8

// Paired describes one paired device, without its key.
type Paired struct {
	Addr     string `json:"addr"`
	Name     string `json:"name,omitempty"`
	LastUsed int64  `json:"lastUsed"` // unix seconds
}

type pairedRecord struct {
	Paired
	Key     string `json:"key"`
	KeyType byte   `json:"keyType"`
}

type keyStore struct {
	path string
	mu   sync.Mutex
	recs map[string]pairedRecord
}

// openKeyStore loads the file. Missing is an empty store. Corrupt is also
// empty, logged by the caller: the only cost is pairing again, where refusing
// to start would cost the speaker feature outright.
func openKeyStore(path string) (*keyStore, error) {
	s := &keyStore{path: path, recs: map[string]pairedRecord{}}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return s, err
	}
	var recs []pairedRecord
	if err := json.Unmarshal(b, &recs); err != nil {
		return s, err
	}
	for _, r := range recs {
		addr, ok := normaliseAddr(r.Addr)
		if k, err := hex.DecodeString(r.Key); !ok || err != nil || len(k) != 16 {
			continue
		}
		r.Addr = addr
		s.recs[addr] = r
	}
	return s, nil
}

func (s *keyStore) key(addr string) (key [16]byte, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.recs[addr]
	if !ok {
		return key, false
	}
	k, _ := hex.DecodeString(r.Key)
	copy(key[:], k)
	return key, true
}

func (s *keyStore) known(addr string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.recs[addr]
	return ok
}

// put stores a key, writing only when it is new or changed.
func (s *keyStore) put(addr string, key [16]byte, keyType byte, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := hex.EncodeToString(key[:])
	r, had := s.recs[addr]
	if had && r.Key == k && r.KeyType == keyType && (name == "" || r.Name == name) {
		return nil
	}
	r.Addr, r.Key, r.KeyType = addr, k, keyType
	if name != "" {
		r.Name = name
	}
	r.LastUsed = time.Now().Unix()
	s.recs[addr] = r
	for len(s.recs) > maxPaired {
		oldest := ""
		for a, o := range s.recs {
			if a != addr && (oldest == "" || o.LastUsed < s.recs[oldest].LastUsed) {
				oldest = a
			}
		}
		delete(s.recs, oldest)
	}
	return s.saveLocked()
}

// setName records a name learnt after pairing, if it changed.
func (s *keyStore) setName(addr, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.recs[addr]
	if !ok || name == "" || r.Name == name {
		return nil
	}
	r.Name = name
	s.recs[addr] = r
	return s.saveLocked()
}

func (s *keyStore) forget(addr string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.recs[addr]; !ok {
		return nil
	}
	delete(s.recs, addr)
	return s.saveLocked()
}

// list returns the paired devices, most recently used first. LastUsed is the
// pairing time: refreshing it on every connect would write the file each
// time the speaker reconnects.
func (s *keyStore) list() []Paired {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Paired, 0, len(s.recs))
	for _, r := range s.recs {
		out = append(out, r.Paired)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].LastUsed != out[j].LastUsed {
			return out[i].LastUsed > out[j].LastUsed
		}
		return out[i].Addr < out[j].Addr
	})
	return out
}

func (s *keyStore) saveLocked() error {
	recs := make([]pairedRecord, 0, len(s.recs))
	for _, r := range s.recs {
		recs = append(recs, r)
	}
	sort.Slice(recs, func(i, j int) bool { return recs[i].Addr < recs[j].Addr })
	b, err := json.Marshal(recs)
	if err != nil {
		return err
	}
	return atomicfile.Write(s.path, b, 0600)
}
