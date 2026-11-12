package config

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
)

// SSHDir is where emOS's init looks for the SSH flag and keys (init.c,
// ssh_main). The FIRMWARE writes them and INIT acts on them, so SSH works
// when EchoMuse is not running: that is what it is for (Wil, 2026-11-11).
// Ignored on FireOS, which has no tinysshd.
const SSHDir = "/data/local/etc/echomuse/ssh"

var sshDir = SSHDir

// SSHMaxKeys bounds the list; init copies at most 8 KiB of it.
const SSHMaxKeys = 16

// sshKeyComment bounds a key's trailing comment, printable ASCII only.
const sshKeyComment = 100

// ParseSSHKeys reads an authorized_keys text and returns one normalised line
// per distinct Ed25519 key ("ssh-ed25519 <base64> [comment]"), or an error
// naming the first line it refuses.
//
// Only Ed25519, the one type tinysshd accepts, checked against its wire form
// (RFC 8709 §4, RFC 4253 §6.6): string "ssh-ed25519", then string of exactly
// 32 octets, nothing after. Options ("from=…", "command=…") are refused:
// tinysshd would not enforce them, and a restriction the owner believes in
// but nothing applies is worse than none. Blank lines and # comments are
// dropped. The controller validates first, so a refusal here is a bug.
func ParseSSHKeys(text string) ([]string, error) {
	var out []string
	seen := map[string]bool{}
	for i, raw := range strings.Split(text, "\n") {
		line := strings.TrimRight(raw, "\r")
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		f := strings.Fields(line)
		if f[0] != "ssh-ed25519" {
			return nil, fmt.Errorf("line %d: only ssh-ed25519 keys are accepted", i+1)
		}
		if len(f) < 2 {
			return nil, fmt.Errorf("line %d: no key after ssh-ed25519", i+1)
		}
		if err := checkEd25519Blob(f[1]); err != nil {
			return nil, fmt.Errorf("line %d: %v", i+1, err)
		}
		comment := strings.Join(f[2:], " ")
		if len(comment) > sshKeyComment {
			return nil, fmt.Errorf("line %d: comment longer than %d characters", i+1, sshKeyComment)
		}
		for _, c := range comment {
			if c < 0x20 || c > 0x7e {
				return nil, fmt.Errorf("line %d: comment must be printable ASCII", i+1)
			}
		}
		if seen[f[1]] {
			continue
		}
		seen[f[1]] = true
		norm := "ssh-ed25519 " + f[1]
		if comment != "" {
			norm += " " + comment
		}
		out = append(out, norm)
		if len(out) > SSHMaxKeys {
			return nil, fmt.Errorf("more than %d keys", SSHMaxKeys)
		}
	}
	return out, nil
}

func checkEd25519Blob(b64 string) error {
	blob, err := base64.StdEncoding.Strict().DecodeString(b64)
	if err != nil {
		return fmt.Errorf("key is not valid base64")
	}
	name, rest, ok := sshString(blob)
	if !ok || string(name) != "ssh-ed25519" {
		return fmt.Errorf("key data is not an ssh-ed25519 key")
	}
	key, rest, ok := sshString(rest)
	if !ok || len(key) != 32 {
		return fmt.Errorf("an Ed25519 public key is 32 bytes")
	}
	if len(rest) != 0 {
		return fmt.Errorf("trailing bytes after the key")
	}
	return nil
}

// sshString reads one RFC 4251 §5 string: a uint32 length, then that many
// bytes.
func sshString(b []byte) (s, rest []byte, ok bool) {
	if len(b) < 4 {
		return nil, nil, false
	}
	n := binary.BigEndian.Uint32(b)
	if uint64(n) > uint64(len(b)-4) {
		return nil, nil, false
	}
	return b[4 : 4+n], b[4+n:], true
}

// WriteSSHKeys stores the owner's keys for init, normalised, or removes the
// file when there are none. Refuses the whole list on any bad line, writing
// nothing. Written only when the content changes (eMMC wear); temp file and
// rename so init never reads half a list.
func WriteSSHKeys(text string) (changed bool, err error) {
	keys, err := ParseSSHKeys(text)
	if err != nil {
		return false, err
	}
	path := filepath.Join(sshDir, "authorized_keys")
	if len(keys) == 0 {
		return removeIfPresent(path)
	}
	want := []byte(strings.Join(keys, "\n") + "\n")
	if cur, err := os.ReadFile(path); err == nil && bytes.Equal(cur, want) {
		return false, nil
	}
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		return false, err
	}
	if err := atomicfile.Write(path, want, 0o600); err != nil {
		return false, err
	}
	return true, nil
}

// WriteSSHEnabled sets or clears the flag init reads. Off is a removed file,
// so a flag can never be half-written.
func WriteSSHEnabled(on bool) (changed bool, err error) {
	path := filepath.Join(sshDir, "enabled")
	if !on {
		return removeIfPresent(path)
	}
	if _, err := os.Stat(path); err == nil {
		return false, nil
	}
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		return false, err
	}
	if err := atomicfile.Write(path, nil, 0o600); err != nil {
		return false, err
	}
	return true, nil
}

func removeIfPresent(path string) (bool, error) {
	if err := os.Remove(path); err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}
