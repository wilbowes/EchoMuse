package client

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func sshFixture(t *testing.T) string {
	dir := t.TempDir()
	savedBin, savedKey, savedTCP := sshBinary, sshHostKey, procTCP
	t.Cleanup(func() { sshBinary, sshHostKey, procTCP = savedBin, savedKey, savedTCP })
	sshBinary = filepath.Join(dir, "tinysshd")
	sshHostKey = filepath.Join(dir, "ed25519.pk")
	procTCP = []string{filepath.Join(dir, "tcp"), filepath.Join(dir, "tcp6")}
	return dir
}

const tcpHeader = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

func TestSSHStatusIsWhatTheEchoIsDoing(t *testing.T) {
	sshFixture(t)
	if ReadSSHStatus() != nil || sshAvailable() {
		t.Fatal("no tinysshd: no capability and no status, never \"off\"")
	}
	os.WriteFile(sshBinary, []byte("x"), 0o644)
	if sshAvailable() {
		t.Fatal("a non-executable file is not an SSH server")
	}
	os.Chmod(sshBinary, 0o755)
	if !sshAvailable() {
		t.Fatal("tinysshd present: capability")
	}

	// An established connection FROM port 22 elsewhere, and a listener on
	// 2222 (0x08AE): neither is SSH listening here.
	os.WriteFile(procTCP[0], []byte(tcpHeader+
		"   0: 0100000A:9C40 0200000A:0016 01 00000000:00000000 00:00000000 00000000 0 0 1\n"+
		"   1: 00000000:08AE 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 2\n"), 0o644)
	if st := ReadSSHStatus(); st == nil || st.Listening || st.HostKey != "" {
		t.Fatalf("not listening yet: %+v", st)
	}
	os.WriteFile(procTCP[0], []byte(tcpHeader+
		"   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 3\n"), 0o644)
	if !ReadSSHStatus().Listening {
		t.Fatal("LISTEN on 0x0016 is SSH listening")
	}
	os.Remove(procTCP[0])
	os.WriteFile(procTCP[1], []byte(tcpHeader+
		"   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A 0 0 0 0 0 4\n"), 0o644)
	if !ReadSSHStatus().Listening {
		t.Fatal("an IPv6 listener counts too")
	}
}

// The fingerprint is the one OpenSSH prints for the same key.
func TestSSHFingerprintMatchesOpenSSH(t *testing.T) {
	dir := sshFixture(t)
	pub := ed25519.NewKeyFromSeed(make([]byte, 32)).Public().(ed25519.PublicKey)
	os.WriteFile(sshHostKey, pub, 0o600)
	fp := sshFingerprint()
	if !strings.HasPrefix(fp, "SHA256:") || strings.HasSuffix(fp, "=") {
		t.Fatalf("not OpenSSH's form: %q", fp)
	}
	os.WriteFile(sshHostKey, pub[:31], 0o600)
	if sshFingerprint() != "" {
		t.Fatal("a short key file has no fingerprint")
	}
	os.WriteFile(sshHostKey, pub, 0o600)

	kg, err := exec.LookPath("ssh-keygen")
	if err != nil {
		t.Skip("no ssh-keygen")
	}
	// The same key in authorized_keys form, built independently.
	line := "ssh-ed25519 " + wireB64(pub)
	f := filepath.Join(dir, "k.pub")
	os.WriteFile(f, []byte(line+"\n"), 0o600)
	out, err := exec.Command(kg, "-l", "-E", "sha256", "-f", f).CombinedOutput()
	if err != nil {
		t.Fatalf("ssh-keygen: %v %s", err, out)
	}
	if !strings.Contains(string(out), fp) {
		t.Fatalf("ssh-keygen says %q, we say %q", out, fp)
	}
}

// wireB64 is the RFC 4253 §6.6 blob, written out byte by byte here rather
// than shared with the code under test.
func wireB64(pub []byte) string {
	b := []byte{0, 0, 0, 11}
	b = append(b, "ssh-ed25519"...)
	b = append(b, 0, 0, 0, 32)
	b = append(b, pub...)
	return base64.StdEncoding.EncodeToString(b)
}
