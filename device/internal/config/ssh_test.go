package config

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// blob builds an RFC 4253 §6.6 public key blob: string name, string key.
func blob(name string, key []byte, trailing ...byte) string {
	var b []byte
	put := func(s []byte) {
		var n [4]byte
		binary.BigEndian.PutUint32(n[:], uint32(len(s)))
		b = append(append(b, n[:]...), s...)
	}
	put([]byte(name))
	put(key)
	return base64.StdEncoding.EncodeToString(append(b, trailing...))
}

func key32(seed byte) []byte {
	pub := ed25519.NewKeyFromSeed(make([]byte, 32)).Public().(ed25519.PublicKey)
	k := append([]byte(nil), pub...)
	k[0] ^= seed
	return k
}

func TestParseSSHKeysAcceptsWhatTheSpecAllows(t *testing.T) {
	a := blob("ssh-ed25519", key32(0))
	b := blob("ssh-ed25519", key32(1))
	got, err := ParseSSHKeys(
		"# my keys\r\n\n  ssh-ed25519 " + a + "   wil@laptop  \r\n" +
			"ssh-ed25519\t" + b + "\n" +
			"ssh-ed25519 " + a + " duplicate\n")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ssh-ed25519 " + a + " wil@laptop", "ssh-ed25519 " + b}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("got %q, want %q", got, want)
	}
	if got, err := ParseSSHKeys(""); err != nil || len(got) != 0 {
		t.Fatalf("empty: %q %v", got, err)
	}
	// Every printable ASCII character is a legal comment character.
	var printable strings.Builder
	for c := 0x21; c <= 0x7e && printable.Len() < sshKeyComment; c++ {
		printable.WriteByte(byte(c))
	}
	if _, err := ParseSSHKeys("ssh-ed25519 " + a + " " + printable.String()); err != nil {
		t.Fatalf("printable comment refused: %v", err)
	}
}

func TestParseSSHKeysRefusesTheSpecsForbiddenForms(t *testing.T) {
	good := blob("ssh-ed25519", key32(0))
	cases := map[string]string{
		"rsa":                "ssh-rsa " + good,
		"ecdsa":              "ecdsa-sha2-nistp256 " + good,
		"options":            `from="10.0.0.1" ssh-ed25519 ` + good,
		"command option":     `command="x" ssh-ed25519 ` + good,
		"no key":             "ssh-ed25519",
		"bad base64":         "ssh-ed25519 !!!!",
		"unpadded base64":    "ssh-ed25519 " + strings.TrimRight(blob("ssh-ed25519", key32(0)[:31]), "="),
		"31-byte key":        "ssh-ed25519 " + blob("ssh-ed25519", key32(0)[:31]),
		"33-byte key":        "ssh-ed25519 " + blob("ssh-ed25519", append(key32(0), 0)),
		"inner type rsa":     "ssh-ed25519 " + blob("ssh-rsa", key32(0)),
		"trailing bytes":     "ssh-ed25519 " + blob("ssh-ed25519", key32(0), 0),
		"truncated length":   "ssh-ed25519 " + base64.StdEncoding.EncodeToString([]byte{0, 0, 0, 11, 's'}),
		"control in comment": "ssh-ed25519 " + good + " a\x01b",
		"non-ASCII comment":  "ssh-ed25519 " + good + " café",
		"long comment":       "ssh-ed25519 " + good + " " + strings.Repeat("x", sshKeyComment+1),
	}
	for name, line := range cases {
		if _, err := ParseSSHKeys("ssh-ed25519 " + good + "\n" + line); err == nil {
			t.Errorf("%s: accepted %q", name, line)
		} else if !strings.HasPrefix(err.Error(), "line 2:") {
			t.Errorf("%s: error does not name the line: %v", name, err)
		}
	}
	var many strings.Builder
	for i := 0; i <= SSHMaxKeys; i++ {
		many.WriteString("ssh-ed25519 " + blob("ssh-ed25519", key32(byte(i))) + "\n")
	}
	if _, err := ParseSSHKeys(many.String()); err == nil {
		t.Errorf("accepted %d keys", SSHMaxKeys+1)
	}
}

// The real implementation agrees: ssh-keygen reads every line we accept.
func TestAcceptedKeysReadByOpenSSH(t *testing.T) {
	kg, err := exec.LookPath("ssh-keygen")
	if err != nil {
		t.Skip("no ssh-keygen")
	}
	dir := t.TempDir()
	if out, err := exec.Command(kg, "-q", "-t", "ed25519", "-N", "", "-C", "test@host",
		"-f", filepath.Join(dir, "k")).CombinedOutput(); err != nil {
		t.Fatalf("ssh-keygen: %v %s", err, out)
	}
	pub, _ := os.ReadFile(filepath.Join(dir, "k.pub"))
	got, err := ParseSSHKeys(string(pub))
	if err != nil || len(got) != 1 {
		t.Fatalf("a key ssh-keygen made was refused: %v", err)
	}
	if got[0] != strings.TrimSpace(string(pub)) {
		t.Fatalf("normalised %q to %q", pub, got[0])
	}
	f := filepath.Join(dir, "ours.pub")
	os.WriteFile(f, []byte(got[0]+"\n"), 0o600)
	if out, err := exec.Command(kg, "-l", "-f", f).CombinedOutput(); err != nil {
		t.Fatalf("ssh-keygen cannot read our line: %v %s", err, out)
	}
	// And our own built blob is what OpenSSH would make of it.
	b := blob("ssh-ed25519", key32(7))
	os.WriteFile(f, []byte("ssh-ed25519 "+b+"\n"), 0o600)
	if out, err := exec.Command(kg, "-l", "-f", f).CombinedOutput(); err != nil {
		t.Fatalf("ssh-keygen refuses a blob the test builds: %v %s", err, out)
	}
}

func TestWriteSSH(t *testing.T) {
	sshDir = t.TempDir() + "/ssh"
	t.Cleanup(func() { sshDir = SSHDir })
	keys := filepath.Join(sshDir, "authorized_keys")
	flag := filepath.Join(sshDir, "enabled")
	a := "ssh-ed25519 " + blob("ssh-ed25519", key32(0)) + " me"

	if ch, err := WriteSSHKeys(a + "\r\n"); err != nil || !ch {
		t.Fatalf("first write: %v %v", ch, err)
	}
	if b, _ := os.ReadFile(keys); string(b) != a+"\n" {
		t.Fatalf("stored %q", b)
	}
	if st, _ := os.Stat(keys); st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode())
	}
	if ch, _ := WriteSSHKeys(a); ch {
		t.Fatal("the same keys again were rewritten")
	}
	if ch, err := WriteSSHKeys(a + "\nssh-rsa AAAA"); err == nil || ch {
		t.Fatal("a bad list was written")
	}
	if b, _ := os.ReadFile(keys); string(b) != a+"\n" {
		t.Fatal("a refused list changed the stored one")
	}
	if ch, _ := WriteSSHKeys(""); !ch {
		t.Fatal("clearing the keys changed nothing")
	}
	if _, err := os.Stat(keys); !os.IsNotExist(err) {
		t.Fatal("no keys must mean no file")
	}

	if ch, _ := WriteSSHEnabled(true); !ch {
		t.Fatal("enable changed nothing")
	}
	if ch, _ := WriteSSHEnabled(true); ch {
		t.Fatal("enable twice wrote twice")
	}
	if _, err := os.Stat(flag); err != nil {
		t.Fatal("no flag")
	}
	if ch, _ := WriteSSHEnabled(false); !ch {
		t.Fatal("disable changed nothing")
	}
	if ch, _ := WriteSSHEnabled(false); ch {
		t.Fatal("disable twice reported a change")
	}
	if _, err := os.Stat(flag); !os.IsNotExist(err) {
		t.Fatal("flag still there")
	}
}

// The vectors the controller's em_ssh_keys.py runs too, so the two agree.
func TestSSHKeysSharedVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/ssh_keys.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Accept []struct {
			Name, Text string
			Keys       []string
		}
		Refuse []struct {
			Name, Text string
			Line       int
		}
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	for _, c := range v.Accept {
		got, err := ParseSSHKeys(c.Text)
		if err != nil || strings.Join(got, "|") != strings.Join(c.Keys, "|") {
			t.Errorf("%s: got %q, %v; want %q", c.Name, got, err, c.Keys)
		}
	}
	for _, c := range v.Refuse {
		_, err := ParseSSHKeys(c.Text)
		want := fmt.Sprintf("line %d:", c.Line)
		switch {
		case err == nil:
			t.Errorf("%s: accepted", c.Name)
		case c.Line > 0 && !strings.HasPrefix(err.Error(), want):
			t.Errorf("%s: %v, want it to name line %d", c.Name, err, c.Line)
		case c.Line == 0 && strings.HasPrefix(err.Error(), "line "):
			t.Errorf("%s: %v, want a whole-list error", c.Name, err)
		}
	}
}
