// The peer link is a plain TCP connection carrying raw 48 kHz mono
// S16LE after a short handshake. No WebSocket, no HTTP: there is no
// browser here, and the handshake is the only framing the link needs —
// after it, both directions are an unbroken PCM stream until either
// side closes, and close is end of call.
//
// Handshake (dialler → listener):
//
//	magic "EMIC" | version 1 | sessionLen | session | tokenLen | token
//	| callerLen | caller
//
// Response (listener → dialler), one byte:
//
//	0 ok | 1 bad magic/version | 2 unknown session | 3 bad token |
//	4 busy (this end is not waiting for a call)
//
// The token is the controller-issued per-call token from the offer.
// The listener accepts only the session it was offered and only the
// token that came with it, so a stray connection — or a device from
// another call — cannot inject audio into a call it was not invited to.
// The token is a secret: it is never logged, and the dialler's
// handshake is the only place it travels after the offer.
package intercom

import (
	"errors"
	"fmt"
	"io"
	"net"
	"time"
)

// DefaultPort is the intercom listener port. Sendspin holds 8928; the
// next port up is the intercom's. The firewall rule opens only while a
// call is being attempted or is live, and closes when it ends.
const DefaultPort = 8929

// HandshakeTimeout bounds the handshake on both ends: a listener must
// not let a half-open connection hold the call hostage, and a dialler
// must not wait forever on a peer that never answers.
const HandshakeTimeout = 10 * time.Second

const (
	handshakeMagic   = "EMIC"
	handshakeVersion = 1
)

const (
	acceptOK = iota
	acceptBadHandshake
	acceptUnknownSession
	acceptBadToken
	acceptBusy
)

var (
	// ErrHandshake is a handshake that failed on the wire: wrong magic,
	// wrong version, or a field that does not parse.
	ErrHandshake = errors.New("intercom: bad handshake")
	// ErrUnknownSession is a dial for a session this end was not offered.
	ErrUnknownSession = errors.New("intercom: unknown session")
	// ErrBadToken is a dial with the wrong per-call token.
	ErrBadToken = errors.New("intercom: bad token")
	// ErrBusy is a dial while this end is not waiting for a call.
	ErrBusy = errors.New("intercom: peer is busy")
)

// handshake is the dialler's opening message.
type handshake struct {
	Session string
	Token   string
	Caller  string
}

func writeHandshake(w io.Writer, h handshake) error {
	if len(h.Session) > 255 || len(h.Token) > 255 || len(h.Caller) > 255 {
		return fmt.Errorf("intercom: handshake field too long")
	}
	buf := []byte(handshakeMagic)
	buf = append(buf, handshakeVersion)
	buf = append(buf, byte(len(h.Session)))
	buf = append(buf, h.Session...)
	buf = append(buf, byte(len(h.Token)))
	buf = append(buf, h.Token...)
	buf = append(buf, byte(len(h.Caller)))
	buf = append(buf, h.Caller...)
	_, err := w.Write(buf)
	return err
}

func readHandshake(r io.Reader) (handshake, error) {
	var h handshake
	head := make([]byte, 5)
	if _, err := io.ReadFull(r, head); err != nil {
		return h, ErrHandshake
	}
	if string(head[:4]) != handshakeMagic || head[4] != handshakeVersion {
		return h, ErrHandshake
	}
	readField := func() (string, error) {
		var lb [1]byte
		if _, err := io.ReadFull(r, lb[:]); err != nil {
			return "", ErrHandshake
		}
		b := make([]byte, lb[0])
		if _, err := io.ReadFull(r, b); err != nil {
			return "", ErrHandshake
		}
		return string(b), nil
	}
	var err error
	if h.Session, err = readField(); err != nil {
		return h, err
	}
	if h.Token, err = readField(); err != nil {
		return h, err
	}
	if h.Caller, err = readField(); err != nil {
		return h, err
	}
	return h, nil
}

// dial opens the peer link: connect, handshake, wait for the accept
// byte. The caller is responsible for closing the returned conn.
func dial(addr string, h handshake) (net.Conn, error) {
	d := net.Dialer{Timeout: HandshakeTimeout}
	conn, err := d.Dial("tcp", addr)
	if err != nil {
		return nil, err
	}
	if err := conn.SetDeadline(time.Now().Add(HandshakeTimeout)); err != nil {
		conn.Close()
		return nil, err
	}
	if err := writeHandshake(conn, h); err != nil {
		conn.Close()
		return nil, err
	}
	var resp [1]byte
	if _, err := io.ReadFull(conn, resp[:]); err != nil {
		conn.Close()
		return nil, err
	}
	switch resp[0] {
	case acceptOK:
	case acceptBadHandshake:
		conn.Close()
		return nil, ErrHandshake
	case acceptUnknownSession:
		conn.Close()
		return nil, ErrUnknownSession
	case acceptBadToken:
		conn.Close()
		return nil, ErrBadToken
	case acceptBusy:
		conn.Close()
		return nil, ErrBusy
	default:
		conn.Close()
		return nil, fmt.Errorf("intercom: unknown accept code %d", resp[0])
	}
	// The handshake deadline served its purpose; the audio stream
	// runs on its own read/write deadlines instead.
	if err := conn.SetDeadline(time.Time{}); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}

// acceptHandshake reads one handshake off an accepted connection and
// checks it against the session this end was offered. It writes the
// accept byte either way, so a rejected dialler learns it was refused
// rather than hanging until its own deadline.
func acceptHandshake(conn net.Conn, sess Session, waiting bool) (handshake, error) {
	if err := conn.SetDeadline(time.Now().Add(HandshakeTimeout)); err != nil {
		return handshake{}, err
	}
	h, err := readHandshake(conn)
	code := byte(acceptOK)
	switch {
	case err != nil:
		code = acceptBadHandshake
	case !waiting:
		code = acceptBusy
	case h.Session != sess.ID:
		code = acceptUnknownSession
		err = ErrUnknownSession
	case h.Token != sess.Token:
		code = acceptBadToken
		err = ErrBadToken
	}
	if _, werr := conn.Write([]byte{code}); werr != nil {
		return h, werr
	}
	if code != acceptOK {
		if err == nil {
			err = ErrBusy
		}
		return h, err
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return h, err
	}
	return h, nil
}
