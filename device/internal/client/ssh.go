package client

import (
	"bufio"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/config"
)

// Variables so the test can point them at fixtures.
var (
	sshBinary  = "/sbin/tinysshd"
	sshHostKey = filepath.Join(config.SSHDir, "hostkeys", "ed25519.pk")
	procTCP    = []string{"/proc/net/tcp", "/proc/net/tcp6"}
)

const sshPortHex = "0016" // 22

// sshAvailable decides the "ssh" capability: this image carries emOS's SSH
// server. A static boot property, so it rides registration.
func sshAvailable() bool {
	st, err := os.Stat(sshBinary)
	return err == nil && st.Mode().IsRegular() && st.Mode().Perm()&0o111 != 0
}

// SSHStatus is what the Echo reports about SSH: whether something is
// listening on port 22 now, and the host key's fingerprint so the owner can
// check it on first connect. Read from the kernel and the key file, never
// from the setting, because the dashboard's statement about SSH must be the
// Echo's own (the listening rule, applied to remote access).
type SSHStatus struct {
	Listening bool   `json:"listening"`
	HostKey   string `json:"hostKey,omitempty"`
}

// ReadSSHStatus returns nil where the image has no SSH server, so "not
// available" never reads as "off".
func ReadSSHStatus() *SSHStatus {
	if !sshAvailable() {
		return nil
	}
	return &SSHStatus{Listening: sshListening(), HostKey: sshFingerprint()}
}

// sshListening looks for a LISTEN socket (state 0A) on port 22 in the
// kernel's tables, the same thing netstat reads.
func sshListening() bool {
	for _, path := range procTCP {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			fl := strings.Fields(sc.Text())
			if len(fl) < 4 || fl[3] != "0A" {
				continue
			}
			if i := strings.LastIndexByte(fl[1], ':'); i >= 0 && strings.EqualFold(fl[1][i+1:], sshPortHex) {
				f.Close()
				return true
			}
		}
		f.Close()
	}
	return false
}

// sshFingerprint is OpenSSH's "SHA256:<base64, no padding>" of the host key's
// wire blob (RFC 4253 §6.6), from tinysshd's raw 32-byte ed25519.pk. Empty
// until init has made the key.
func sshFingerprint() string {
	pk, err := os.ReadFile(sshHostKey)
	if err != nil || len(pk) != 32 {
		return ""
	}
	var b []byte
	for _, s := range [][]byte{[]byte("ssh-ed25519"), pk} {
		var n [4]byte
		binary.BigEndian.PutUint32(n[:], uint32(len(s)))
		b = append(append(b, n[:]...), s...)
	}
	sum := sha256.Sum256(b)
	return "SHA256:" + base64.RawStdEncoding.EncodeToString(sum[:])
}
