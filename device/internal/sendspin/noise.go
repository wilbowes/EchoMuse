package sendspin

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/flynn/noise"
)

// The device is always the Noise RESPONDER: the server initiates the
// handshake whichever side opened the WebSocket. Pattern KKpsk2 — both static
// keys known in advance (they are the ids), PSK mixed at the end of message 2.
//
// ChaChaPoly rather than AESGCM: Go has no AES acceleration on 32-bit ARM,
// and ChaCha costs 0.40% of a core at FLAC rate (measured, PR #271).
const suiteName = "25519_ChaChaPoly_SHA256"

var cipherSuite = noise.NewCipherSuite(noise.DH25519, noise.CipherChaChaPoly, noise.HashSHA256)

// placeholderPSK is what the handshake is built with before message 1 says
// which PSK the server is using. psk2 mixes the PSK only when message 2 is
// written, so reading message 1 under a placeholder is exact.
var placeholderPSK = make([]byte, pskSize)

// maxTransportPlaintext is Noise's 65535-byte limit less the 16-byte tag.
const maxTransportPlaintext = 65535 - 16

// transport is one direction pair of Noise cipher states.
type transport struct {
	send, recv *noise.CipherState
}

func (t *transport) encrypt(pt []byte) ([]byte, error) {
	return t.send.Encrypt(nil, nil, pt)
}

func (t *transport) decrypt(ct []byte) ([]byte, error) {
	return t.recv.Decrypt(nil, nil, ct)
}

// responder runs the device's half of one handshake. It is split in two
// because the PSK is chosen between the messages.
type responder struct {
	hs *noise.HandshakeState
}

func newResponder(id identity, serverPub, prologue []byte) (*responder, error) {
	hs, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:           cipherSuite,
		Pattern:               noise.HandshakeKK,
		Initiator:             false,
		Prologue:              prologue,
		StaticKeypair:         noise.DHKey{Private: id.priv, Public: id.pub},
		PeerStatic:            serverPub,
		PresharedKey:          placeholderPSK,
		PresharedKeyPlacement: 2,
	})
	if err != nil {
		return nil, err
	}
	return &responder{hs: hs}, nil
}

// msg1Payload is what the server says inside Noise message 1: which PSK it is
// using. psk_category is the spec's addition; 9.1.1 does not send it.
type msg1Payload struct {
	PskID       string `json:"psk_id"`
	PskCategory string `json:"psk_category,omitempty"`
}

// readMsg1 decrypts message 1. It is authenticated by the static keys alone,
// so a wrong server key fails here.
func (r *responder) readMsg1(msg []byte) (msg1Payload, error) {
	pt, _, _, err := r.hs.ReadMessage(nil, msg)
	if err != nil {
		return msg1Payload{}, fmt.Errorf("noise message 1: %w", err)
	}
	var p msg1Payload
	if err := json.Unmarshal(pt, &p); err != nil || len(p.PskID) != idLen {
		return msg1Payload{}, errors.New("noise message 1: malformed payload")
	}
	return p, nil
}

// writeMsg2 mixes the chosen PSK and completes the handshake. The payload is
// the literal two bytes "{}", not an empty payload.
func (r *responder) writeMsg2(psk []byte) ([]byte, *transport, []byte, error) {
	if err := r.hs.SetPresharedKey(psk); err != nil {
		return nil, nil, nil, err
	}
	msg, c1, c2, err := r.hs.WriteMessage(nil, []byte("{}"))
	if err != nil {
		return nil, nil, nil, err
	}
	if c1 == nil || c2 == nil {
		return nil, nil, nil, errors.New("noise: handshake did not complete")
	}
	// c1 carries initiator→responder, c2 responder→initiator.
	return msg, &transport{send: c2, recv: c1}, r.hs.ChannelBinding(), nil
}

// Binary message types after decryption. 0 is a JSON body; 4 is an audio
// chunk. Fragments come in two dialects:
//
//   - spec: type 1, then a flags byte (bit 1 first, bit 0 last), with the
//     original type after the flags on the first fragment;
//   - 9.1.1: type 2 for "more" and 3 for "end", with the original type after
//     the first "more" byte.
//
// Types 2 and 3 are reserved in the spec, so the two cannot be confused.
const (
	msgJSON         = 0
	msgFragment     = 1
	msgFragMore911  = 2
	msgFragEnd911   = 3
	msgAudioChunk   = 4
	maxReassembled  = 16 << 20
	fragFlagFirst   = 1 << 1
	fragFlagLast    = 1 << 0
	fragFlagsUnused = 0xFC
)

// reassembler turns decrypted transport messages back into whole ones.
type reassembler struct {
	buf    []byte
	typ    byte
	active bool
}

var errFragment = errors.New("sendspin: malformed fragment sequence")

// push takes one decrypted message and returns a complete one, or nil while a
// fragmented message is still being collected. A malformed sequence is a
// protocol error: the caller closes the connection.
func (r *reassembler) push(pt []byte) ([]byte, error) {
	if len(pt) == 0 {
		return nil, errFragment
	}
	switch pt[0] {
	case msgFragment:
		if len(pt) < 2 || pt[1]&fragFlagsUnused != 0 {
			return nil, errFragment
		}
		flags, data := pt[1], pt[2:]
		if flags&fragFlagFirst != 0 {
			if r.active || len(data) < 1 || data[0] == msgFragment {
				return nil, errFragment
			}
			r.typ, r.buf, r.active = data[0], append(r.buf[:0], data[1:]...), true
		} else {
			if !r.active {
				return nil, errFragment
			}
			r.buf = append(r.buf, data...)
		}
		return r.finish(flags&fragFlagLast != 0)
	case msgFragMore911:
		if !r.active {
			if len(pt) < 2 {
				return nil, errFragment
			}
			r.typ, r.buf, r.active = pt[1], append(r.buf[:0], pt[2:]...), true
			return nil, r.check()
		}
		r.buf = append(r.buf, pt[1:]...)
		return nil, r.check()
	case msgFragEnd911:
		if !r.active {
			return nil, errFragment
		}
		r.buf = append(r.buf, pt[1:]...)
		return r.finish(true)
	default:
		if r.active {
			return nil, errFragment
		}
		return pt, nil
	}
}

func (r *reassembler) check() error {
	if len(r.buf) > maxReassembled {
		return errFragment
	}
	return nil
}

func (r *reassembler) finish(last bool) ([]byte, error) {
	if err := r.check(); err != nil {
		return nil, err
	}
	if !last {
		return nil, nil
	}
	out := make([]byte, 1+len(r.buf))
	out[0] = r.typ
	copy(out[1:], r.buf)
	r.active, r.buf = false, r.buf[:0]
	return out, nil
}
