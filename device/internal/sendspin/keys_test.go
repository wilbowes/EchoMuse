package sendspin

import (
	"bytes"
	"encoding/hex"
	"path/filepath"
	"testing"
)

// Published vectors from the spec (pairing.md, connection.md).
func TestPairingTokenMatchesTheSpecVector(t *testing.T) {
	key := make([]byte, 32)
	psk := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
		psk[i] = byte(0xe0 + i)
	}
	const want = "SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP6BYPC4PSOLZXH5DU6V97M5XXO74HR6LZ7J5PW674PT6X37T6757Y"
	if got := pairingToken(key, psk); got != want {
		t.Fatalf("token\n got %s\nwant %s", got, want)
	}
	// And back, through the lenient decoding a server applies to typed input.
	k, p, err := decodePairingToken("  sp:" + want[3:] + " ")
	if err != nil || !bytes.Equal(k, key) || !bytes.Equal(p, psk) {
		t.Fatalf("round trip failed: %v", err)
	}
}

func TestSentinelPSKAndItsIDMatchTheSpec(t *testing.T) {
	if got := hex.EncodeToString(sentinelPSK); got != "1b5e24dbc1aed95fc2a5a338a90c05df44bd10f5ec1f4cd66cbf86272767b9d3" {
		t.Errorf("sentinel PSK %s", got)
	}
	if got := pskID(sentinelPSK); got != "GFsV9tLaSQm9HcFWpKsgYQOr7wFTvNUtkmFwuVz3zoo" {
		t.Errorf("sentinel psk_id %s", got)
	}
}

func TestPeerKeyRefusesAnythingButA43CharKey(t *testing.T) {
	good := b64u(make([]byte, 32))
	if _, err := peerKey(good); err != nil {
		t.Errorf("valid id refused: %v", err)
	}
	for _, bad := range []string{"", good[:42], good + "A", "!" + good[1:]} {
		if _, err := peerKey(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

// The identity IS the client_id, so it must survive a restart, and the
// secrets must be per device rather than defaults.
func TestStoreKeepsItsIdentityAndMintsFreshSecrets(t *testing.T) {
	dir := t.TempDir()
	a, err := openStore(filepath.Join(dir, "a.json"))
	if err != nil {
		t.Fatal(err)
	}
	a2, err := openStore(filepath.Join(dir, "a.json"))
	if err != nil {
		t.Fatal(err)
	}
	if a.id.clientID() != a2.id.clientID() || a.token() != a2.token() {
		t.Error("identity or pairing PSK changed across a reopen")
	}
	b, _ := openStore(filepath.Join(dir, "b.json"))
	if a.id.clientID() == b.id.clientID() || bytes.Equal(a.pp, b.pp) {
		t.Error("two stores share an identity or pairing PSK")
	}
	if s := a.settings(); s.Volume != 100 || s.Muted || s.StaticDelayMs != 0 {
		t.Errorf("fresh settings %+v", s)
	}
}

func TestResolveFindsEachKindOfPSKAndFallsBackToTheSentinel(t *testing.T) {
	s, _ := openStore(filepath.Join(t.TempDir(), "s.json"))
	lt, _ := randomPSK()
	srv := b64u(bytes.Repeat([]byte{7}, 32))
	if err := s.addRecord(srv, lt, func(string) bool { return false }); err != nil {
		t.Fatal(err)
	}
	if r := s.resolve(pskID(lt)); r.cat != catLongTerm || r.serverID != srv {
		t.Errorf("long-term: %+v", r)
	}
	if r := s.resolve(pskID(s.pp)); r.cat != catPairing {
		t.Errorf("pairing: %+v", r)
	}
	if r := s.resolve(pskID([]byte("nope"))); r.cat != catSentinel || !bytes.Equal(r.psk, sentinelPSK) {
		t.Errorf("miss: %+v", r)
	}
}

func TestPairingRecordsEvictTheLeastRecentlyUsedButNeverOneInUse(t *testing.T) {
	s, _ := openStore(filepath.Join(t.TempDir(), "s.json"))
	ids := make([]string, maxRecords+1)
	for i := range ids {
		ids[i] = b64u(bytes.Repeat([]byte{byte(i + 1)}, 32))
		psk, _ := randomPSK()
		s.addRecord(ids[i], psk, func(id string) bool { return id == ids[0] })
		s.f.Records[len(s.f.Records)-1].LastUsed = int64(i) // deterministic order
	}
	got := s.paired()
	if len(got) != maxRecords {
		t.Fatalf("%d records, want %d", len(got), maxRecords)
	}
	has := func(id string) bool {
		for _, g := range got {
			if g == id {
				return true
			}
		}
		return false
	}
	if !has(ids[0]) {
		t.Error("evicted a record backing a live connection")
	}
	if has(ids[1]) {
		t.Error("kept the least recently used evictable record")
	}
	if !has(ids[maxRecords]) {
		t.Error("the new pairing did not persist")
	}
}
