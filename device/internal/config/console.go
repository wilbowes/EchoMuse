package config

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
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

// ConsoleTimeoutPath is where emOS's init looks for the console idle timeout,
// beside the password record and for the same reasons: the FIRMWARE writes it
// and INIT reads it, because the console has to work when EchoMuse is not
// running.
const ConsoleTimeoutPath = "/data/local/etc/echomuse/console.timeout"

// The path actually written, so tests can point it somewhere harmless. A var
// rather than passing the path in: every caller in the firmware wants the one
// real location, and threading it through would let a future caller write the
// record somewhere init does not read.
var consoleTimeoutPath = ConsoleTimeoutPath

// ConsoleTimeoutMaxMin is the ceiling the controller validates against and
// init clamps to. 90 minutes is long enough that a real debugging session is
// never the thing being cut off.
const ConsoleTimeoutMaxMin = 90

// WriteConsoleTimeout stores the console idle timeout in MINUTES, or removes
// the record when the timeout is zero.
//
// Minutes rather than seconds because that is the unit the setting is chosen
// in (0, then 1-90). `TMOUT` is seconds, so init multiplies when it builds the
// shell's environment — one conversion, at the point of use, rather than a
// factor of sixty between the stored value, the pushed value and the number on
// screen.
//
// Zero means no timeout and is a CHOICE, not an absence: a device whose owner
// deliberately turned this off must not be moved by a later change of default.
// Absent is handled by the caller, which passes a pointer for the same reason
// ConsolePassword is one.
//
// Out of range is refused rather than clamped. The controller validates first,
// so a bad value reaching here is a bug rather than a user, and silently
// accepting it would hide the bug behind a plausible timeout.
//
// Written only when the content changes, for the eMMC reason above.
func WriteConsoleTimeout(minutes int) (changed bool, err error) {
	if minutes < 0 || minutes > ConsoleTimeoutMaxMin {
		return false, fmt.Errorf("console timeout %d is outside 0-%d minutes",
			minutes, ConsoleTimeoutMaxMin)
	}

	if minutes == 0 {
		if err := os.Remove(consoleTimeoutPath); err != nil {
			if os.IsNotExist(err) {
				return false, nil // already no timeout; nothing written
			}
			return false, err
		}
		return true, nil
	}

	want := []byte(strconv.Itoa(minutes))
	current, readErr := os.ReadFile(consoleTimeoutPath)
	if readErr == nil && bytes.Equal(bytes.TrimSpace(current), want) {
		return false, nil
	}

	if err := os.MkdirAll(filepath.Dir(consoleTimeoutPath), 0o755); err != nil {
		return false, err
	}
	// Temp file and rename, so init can never read a half-written value. A
	// truncated number would parse as a SHORTER timeout, which is the
	// dangerous direction: a console that expires mid-command looks like the
	// device dropping the link.
	tmp := consoleTimeoutPath + ".tmp"
	if err := os.WriteFile(tmp, append(want, '\n'), 0o600); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, consoleTimeoutPath); err != nil {
		os.Remove(tmp)
		return false, err
	}
	return true, nil
}
