package client

import (
	"crypto/tls"
	"crypto/x509"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// Device-link TLS credentials — pushed by the controller (provisioning
// wizard over adb, or the dashboard "Secure link" action over the shell
// plane). Paths are coupled with controller/em_api.py DEVICE_TLS_DIR.
//
//	ca.pem — the controller CA the device pins (chain verification uses
//	         ONLY this pool; system roots are irrelevant)
//	token  — shared secret sent as X-EM-Token on all three WS dials
//
// Credentials are re-read on every dial attempt, so a push takes effect
// on the next reconnect without a process restart.
const (
	credCAPath    = "/data/local/etc/echomuse/ca.pem"
	credTokenPath = "/data/local/etc/echomuse/token"

	// Must match the DNS SAN in the controller's server cert
	// (controller/em_pki.py TLS_SERVER_NAME). It is an identity label,
	// not a resolvable name — mDNS supplies the actual address.
	tlsServerName = "echomuse-controller"
)

// BuildUnix is the firmware build timestamp (seconds since epoch), set at
// build time via ldflags — see device/compile.sh. It floors the TLS
// verification clock: an Echo fresh off a reboot can sit at a bogus date
// until Android's NTP syncs, and a strict NotBefore check would then
// brick the connection that time sync itself may depend on.
var BuildUnix = ""

type linkCreds struct {
	tlsConf *tls.Config // nil — no CA on disk, plain ws
	token   string      // "" — no token on disk
}

// loadLinkCreds reads the credential files. Absent files are the normal
// pre-rollout state, not an error.
func loadLinkCreds() linkCreds {
	var creds linkCreds

	if pem, err := os.ReadFile(credCAPath); err == nil {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			log.Printf("[tls] %s exists but contains no valid PEM certificate — ignoring", credCAPath)
		} else {
			creds.tlsConf = &tls.Config{
				RootCAs:    pool,
				ServerName: tlsServerName,
				MinVersion: tls.VersionTLS12,
				Time:       tlsNow,
			}
		}
	}

	if tok, err := os.ReadFile(credTokenPath); err == nil {
		creds.token = strings.TrimSpace(string(tok))
	}

	return creds
}

// header returns the WS handshake headers for a dial: the link token if
// one is installed, empty otherwise.
func (c linkCreds) header() http.Header {
	h := http.Header{}
	if c.token != "" {
		h.Set("X-EM-Token", c.token)
	}
	return h
}

// dialer returns a websocket dialer carrying the pinned-CA TLS config
// (nil TLSClientConfig is fine for plain ws dials).
func (c linkCreds) dialer() websocket.Dialer {
	return websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		TLSClientConfig:  c.tlsConf,
	}
}

// tlsNow is the verification clock for cert validity: never earlier than
// the firmware build time. The controller additionally backdates its certs
// 10 years (see em_pki.py), so between the two, a wrong device clock in
// either direction cannot strand the device off the network.
func tlsNow() time.Time {
	now := time.Now()
	if BuildUnix != "" {
		if sec, err := strconv.ParseInt(BuildUnix, 10, 64); err == nil {
			if bt := time.Unix(sec, 0); now.Before(bt) {
				return bt
			}
		}
	}
	return now
}
