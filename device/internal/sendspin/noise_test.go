package sendspin

import (
	"bytes"
	"encoding/json"
	"errors"
	"testing"

	"github.com/flynn/noise"
)

// initiator plays the server's half, for tests. The interop harness
// (tools/sendspin_interop) checks the same exchange against aiosendspin.
func initiator(t *testing.T, server identity, clientPub, prologue, psk []byte) *noise.HandshakeState {
	t.Helper()
	hs, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:           cipherSuite,
		Pattern:               noise.HandshakeKK,
		Initiator:             true,
		Prologue:              prologue,
		StaticKeypair:         noise.DHKey{Private: server.priv, Public: server.pub},
		PeerStatic:            clientPub,
		PresharedKey:          psk,
		PresharedKeyPlacement: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	return hs
}

func TestResponderCompletesUnderThePSKTheServerNamed(t *testing.T) {
	client, _ := newIdentity()
	server, _ := newIdentity()
	prologue := []byte(`{"type":"client/init"}{"type":"server/init"}`)
	psk, _ := randomPSK()

	ini := initiator(t, server, client.pub, prologue, psk)
	p1, _ := json.Marshal(msg1Payload{PskID: pskID(psk)})
	m1, _, _, err := ini.WriteMessage(nil, p1)
	if err != nil {
		t.Fatal(err)
	}

	r, _ := newResponder(client, server.pub, prologue)
	got, err := r.readMsg1(m1)
	if err != nil || got.PskID != pskID(psk) {
		t.Fatalf("message 1: %+v %v", got, err)
	}
	m2, tr, h, err := r.writeMsg2(psk)
	if err != nil {
		t.Fatal(err)
	}
	pl, c1, c2, err := ini.ReadMessage(nil, m2)
	if err != nil {
		t.Fatalf("server rejected message 2: %v", err)
	}
	if string(pl) != "{}" {
		t.Errorf("message 2 payload %q, want the literal {}", pl)
	}
	if !bytes.Equal(h, ini.ChannelBinding()) {
		t.Error("handshake hash differs between the two sides")
	}

	// Transport both ways.
	ct, _ := c1.Encrypt(nil, nil, []byte("to client"))
	if pt, err := tr.decrypt(ct); err != nil || string(pt) != "to client" {
		t.Errorf("server→client: %q %v", pt, err)
	}
	ct, _ = tr.encrypt([]byte("to server"))
	if pt, err := c2.Decrypt(nil, nil, ct); err != nil || string(pt) != "to server" {
		t.Errorf("client→server: %q %v", pt, err)
	}
}

// The wrong PSK is caught by the SERVER, on message 2. Message 1 carries no
// PSK, so the device cannot tell — which is why a lookup miss falls back to
// the Sentinel rather than failing: the server then learns the credential
// did not match.
func TestAWrongPSKFailsAtTheServer(t *testing.T) {
	client, _ := newIdentity()
	server, _ := newIdentity()
	real, _ := randomPSK()
	ini := initiator(t, server, client.pub, nil, real)
	p1, _ := json.Marshal(msg1Payload{PskID: pskID(real)})
	m1, _, _, _ := ini.WriteMessage(nil, p1)

	r, _ := newResponder(client, server.pub, nil)
	if _, err := r.readMsg1(m1); err != nil {
		t.Fatal(err)
	}
	m2, _, _, err := r.writeMsg2(sentinelPSK)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := ini.ReadMessage(nil, m2); err == nil {
		t.Error("server accepted message 2 under a different PSK")
	}
}

// Message 1 is authenticated by the static keys: a server that is not the
// one named in server/init fails there.
func TestMessage1FromAnImpostorFails(t *testing.T) {
	client, _ := newIdentity()
	server, _ := newIdentity()
	impostor, _ := newIdentity()
	psk, _ := randomPSK()
	ini := initiator(t, impostor, client.pub, nil, psk)
	p1, _ := json.Marshal(msg1Payload{PskID: pskID(psk)})
	m1, _, _, _ := ini.WriteMessage(nil, p1)
	r, _ := newResponder(client, server.pub, nil)
	if _, err := r.readMsg1(m1); err == nil {
		t.Error("accepted message 1 from a key other than the server's")
	}
}

func TestReassemblerTakesBothFragmentDialects(t *testing.T) {
	var r reassembler
	// spec: type 1, flags (first=2, last=1), orig type on the first.
	for _, step := range [][]byte{{1, 2, 4, 'a', 'b'}, {1, 0, 'c'}} {
		if out, err := r.push(step); out != nil || err != nil {
			t.Fatalf("mid-sequence: %v %v", out, err)
		}
	}
	out, err := r.push([]byte{1, 1, 'd'})
	if err != nil || !bytes.Equal(out, []byte{4, 'a', 'b', 'c', 'd'}) {
		t.Fatalf("spec dialect: % x %v", out, err)
	}
	// 9.1.1: type 2 "more" (orig type on the first), 3 "end".
	r.push([]byte{2, 0, '{'})
	r.push([]byte{2, '"'})
	out, err = r.push([]byte{3, '}'})
	if err != nil || !bytes.Equal(out, []byte{0, '{', '"', '}'}) {
		t.Fatalf("9.1.1 dialect: % x %v", out, err)
	}
	if out, err := r.push([]byte{0, 'x'}); err != nil || !bytes.Equal(out, []byte{0, 'x'}) {
		t.Fatalf("plain message: % x %v", out, err)
	}
}

func TestReassemblerRefusesMalformedSequences(t *testing.T) {
	cases := map[string][][]byte{
		"continuation with none in flight": {{1, 0, 'x'}},
		"second first fragment":            {{1, 2, 4, 'x'}, {1, 2, 4, 'y'}},
		"plain message mid-sequence":       {{1, 2, 4, 'x'}, {0, 'y'}},
		"reserved flag bit":                {{1, 0x06, 4, 'x'}},
		"orig type of 1":                   {{1, 3, 1, 'x'}},
		"9.1.1 end with none in flight":    {{3, 'x'}},
		"empty":                            {{}},
	}
	for name, steps := range cases {
		var r reassembler
		var err error
		for _, s := range steps {
			if _, err = r.push(s); err != nil {
				break
			}
		}
		if !errors.Is(err, errFragment) {
			t.Errorf("%s: want errFragment, got %v", name, err)
		}
	}
}

// The activity sets each PSK permits (messaging.md, server/activate). A
// paired server may declare playback; an unpaired one only with unpaired
// access on; pairing only before the device holds a long-term record.
func TestActivitySetsAllowedPerPSK(t *testing.T) {
	set := func(xs ...string) map[string]bool {
		m := map[string]bool{}
		for _, x := range xs {
			m[x] = true
		}
		return m
	}
	cases := []struct {
		cat      pskCategory
		acts     map[string]bool
		unpaired bool
		want     bool
	}{
		{catLongTerm, set(), false, true},
		{catLongTerm, set(actPlayback), false, true},
		{catLongTerm, set(actPairing), false, false},
		{catSentinel, set(), false, true},
		{catSentinel, set(actPairing), false, true},
		{catSentinel, set(actPlayback), false, false},
		{catSentinel, set(actPlayback), true, true},
		{catPairing, set(actPlayback, actPairing), false, false},
		{catPairing, set(actPlayback, actPairing), true, true},
	}
	for _, c := range cases {
		if got := allowed(c.cat, c.acts, c.unpaired); got != c.want {
			t.Errorf("%s %v unpaired=%v: got %v", c.cat, actKey(c.acts), c.unpaired, got)
		}
	}
	if rank(set(actPlayback, actPairing)) <= rank(set(actPairing)) || rank(set(actPairing)) <= rank(set()) {
		t.Error("admission must rank playback over pairing over nothing")
	}
}
