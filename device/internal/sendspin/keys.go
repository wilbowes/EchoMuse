// Package sendspin is a Sendspin player: synchronised multi-room audio from
// Music Assistant, played through the device's music plane (#89, #272).
//
// It speaks the wire of aiosendspin 9.1.1, the server library Music Assistant
// ships, and was tested against that library rather than against the spec
// alone. The two disagree in places (fragment framing, the audio chunk
// header, the shape of supported_pair_methods, static_delay_ms against
// output_delay_ms); where they do, this package sends 9.1.1's form and
// accepts both. Each divergence is marked "9.1.1:" where it is handled.
package sendspin

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base32"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"

	"golang.org/x/crypto/curve25519"
)

const (
	keySize = 32
	pskSize = 32
	idLen   = 43 // base64url of 32 bytes, unpadded
)

// sentinelPSK is the published PSK used when no other applies. Public, so it
// authenticates nothing: a session under it is unpaired.
var sentinelPSK = func() []byte { h := sha256.Sum256([]byte("sendspin-sentinel-psk-v1")); return h[:] }()

func b64u(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func unb64u(s string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "="))
}

// pskID is how a server names a PSK in Noise message 1 without revealing it.
func pskID(psk []byte) string {
	h := sha256.New()
	h.Write([]byte("sendspin-psk-id-v1"))
	h.Write(psk)
	return b64u(h.Sum(nil))
}

// identity is the device's long-term X25519 keypair. Its public half, as
// base64url, is the client_id — so rotating it makes the device a stranger to
// every server it was paired with.
type identity struct {
	priv, pub []byte
}

func newIdentity() (identity, error) {
	priv := make([]byte, keySize)
	if _, err := rand.Read(priv); err != nil {
		return identity{}, err
	}
	return identityFrom(priv)
}

func identityFrom(priv []byte) (identity, error) {
	if len(priv) != keySize {
		return identity{}, fmt.Errorf("sendspin: private key is %d bytes", len(priv))
	}
	pub, err := curve25519.X25519(priv, curve25519.Basepoint)
	if err != nil {
		return identity{}, err
	}
	return identity{priv: append([]byte(nil), priv...), pub: pub}, nil
}

func (id identity) clientID() string { return b64u(id.pub) }

// peerKey decodes a server_id/client_id into the raw public key.
func peerKey(id string) ([]byte, error) {
	if len(id) != idLen {
		return nil, fmt.Errorf("sendspin: peer id is %d chars, want %d", len(id), idLen)
	}
	k, err := unb64u(id)
	if err != nil || len(k) != keySize {
		return nil, errors.New("sendspin: peer id is not a 32-byte base64url key")
	}
	return k, nil
}

func randomPSK() ([]byte, error) {
	p := make([]byte, pskSize)
	_, err := rand.Read(p)
	return p, err
}

// Pairing tokens: "SP:" + version + base32 body, '=' stripped and every '2'
// written as '9', so the token is QR-alphanumeric and survives being typed.
// Version 0 carries client_key || pairing_psk. Music Assistant takes it in
// "pair with token" and checks the key against the connected client.
var b32 = base32.StdEncoding.WithPadding(base32.NoPadding)

func pairingToken(clientKey, pairingPSK []byte) string {
	body := b32.EncodeToString(append(append([]byte(nil), clientKey...), pairingPSK...))
	return "SP:0" + strings.ReplaceAll(body, "2", "9")
}

// decodePairingToken is the lenient decoder the spec asks servers for. The
// device never needs it; the test uses it to prove the encoder round-trips.
func decodePairingToken(s string) (clientKey, psk []byte, err error) {
	s = strings.ToUpper(strings.TrimSpace(s))
	s = strings.TrimPrefix(s, "SP:")
	if s == "" || s[0] != '0' {
		return nil, nil, errors.New("sendspin: not a version-0 pairing token")
	}
	body := strings.ReplaceAll(s[1:], "9", "2")
	raw, err := b32.DecodeString(body)
	if err != nil {
		return nil, nil, err
	}
	if len(raw) < 2*keySize {
		return nil, nil, errors.New("sendspin: pairing token too short")
	}
	return raw[:keySize], raw[keySize : 2*keySize], nil
}
