package config

import (
	"bytes"
	"os"
	"path/filepath"
)

// ConsolePasswordPath is where emOS's init looks for the console password
// record. It sits beside the TLS credentials because it is the same kind of
// thing — a per-device secret pushed by the controller — and because that
// directory already survives an OTA.
//
// The FIRMWARE writes it and INIT reads it, which is the whole reason it is a
// file rather than something held in memory: the console has to work when
// EchoMuse is not running, since that is exactly when someone needs it.
const ConsolePasswordPath = "/data/local/etc/echomuse/console.pw"

// WriteConsolePassword stores the record emOS's init checks against, or
// removes it when the record is empty.
//
// The record is already hashed — the controller hashes before it is stored or
// pushed, so no plaintext passes through here. Empty means no password, which
// is why the caller must distinguish an empty record from an ABSENT field: the
// config push repeats every setting on every connect, so "" has to mean
// "remove" rather than "nothing was said".
//
// Written only when the content actually changes. The push arrives on every
// reconnect, and this device runs for years on eMMC that cannot be replaced,
// so an unconditional write would spend a flash write per reconnect to store
// bytes that were already there.
func WriteConsolePassword(record string) (changed bool, err error) {
	want := []byte(record)

	current, readErr := os.ReadFile(ConsolePasswordPath)
	if readErr == nil && bytes.Equal(bytes.TrimSpace(current), bytes.TrimSpace(want)) {
		return false, nil
	}

	if len(bytes.TrimSpace(want)) == 0 {
		if readErr != nil && os.IsNotExist(readErr) {
			return false, nil
		}
		if err := os.Remove(ConsolePasswordPath); err != nil && !os.IsNotExist(err) {
			return false, err
		}
		return true, nil
	}

	if err := os.MkdirAll(filepath.Dir(ConsolePasswordPath), 0o755); err != nil {
		return false, err
	}
	// Written to a temp file and renamed, so init can never read a half-written
	// record: a truncated one parses as unusable, which is read as NO password
	// and would leave the console open exactly while it looked configured.
	tmp := ConsolePasswordPath + ".tmp"
	if err := os.WriteFile(tmp, append(want, '\n'), 0o600); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, ConsolePasswordPath); err != nil {
		os.Remove(tmp)
		return false, err
	}
	return true, nil
}
