package wifi

import (
	"encoding/hex"
	"fmt"
	"strings"
	"unicode/utf8"
)

// An SSID is 0–32 arbitrary octets (IEEE 802.11): spaces, quotes, backslashes,
// UTF-8 and bytes that are not text at all are all valid. So it travels as
// bytes, and ssidLine writes it to the conf in whichever of wpa_supplicant's
// two forms can hold it (see there).
//
// wpa_cli prints SSIDs through hostap's printf_encode: `"` and `\` are
// backslash-escaped, ESC/LF/CR/TAB become \e \n \r \t, and every other byte
// outside 0x20–0x7e becomes \xNN. UnescapeSSID reverses exactly that, which is
// what makes the bytes recoverable from scan_results and status.

// UnescapeSSID turns wpa_cli's printf_encode output back into the SSID bytes.
func UnescapeSSID(s string) []byte {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c != '\\' || i+1 >= len(s) {
			out = append(out, c)
			continue
		}
		switch n := s[i+1]; n {
		case '\\', '"':
			out = append(out, n)
			i++
		case 'e':
			out = append(out, 0x1b)
			i++
		case 'n':
			out = append(out, '\n')
			i++
		case 'r':
			out = append(out, '\r')
			i++
		case 't':
			out = append(out, '\t')
			i++
		case 'x':
			if i+3 < len(s) {
				if b, err := hex.DecodeString(s[i+2 : i+4]); err == nil {
					out = append(out, b[0])
					i += 3
					continue
				}
			}
			out = append(out, c)
		default:
			out = append(out, c)
		}
	}
	return out
}

// SSIDText is an SSID for display: its bytes as UTF-8, with anything that is
// not valid UTF-8 shown as U+FFFD. Never used to address a network — that is
// what the bytes (and SSIDHex on the wire) are for.
func SSIDText(b []byte) string {
	return strings.ToValidUTF8(string(b), "�")
}

// hiddenOrEmpty is true for an SSID nobody can join by name: empty, or the
// all-zero bytes a hidden AP advertises.
func hiddenOrEmpty(b []byte) bool {
	for _, c := range b {
		if c != 0 {
			return false
		}
	}
	return true
}

// validate checks a network against what WPA2-Personal allows: an SSID of 1–32
// bytes, and a passphrase that is empty (an open network), 64 hex digits (a raw
// PSK), or 8–63 printable ASCII characters. Anything else is refused here,
// with the reason, rather than written and failing to associate.
func validate(ssid []byte, psk string) error {
	if len(ssid) == 0 || hiddenOrEmpty(ssid) {
		return fmt.Errorf("empty SSID")
	}
	if len(ssid) > 32 {
		return fmt.Errorf("SSID is %d bytes; the limit is 32", len(ssid))
	}
	if psk == "" || isHexPSK(psk) {
		return nil
	}
	if len(psk) < 8 || len(psk) > 63 {
		return fmt.Errorf("WPA passphrase must be 8–63 characters (got %d)", len(psk))
	}
	for i := 0; i < len(psk); i++ {
		if psk[i] < 0x20 || psk[i] > 0x7e {
			return fmt.Errorf("WPA passphrase may only contain printable ASCII characters")
		}
	}
	return nil
}

func isHexPSK(psk string) bool {
	if len(psk) != 64 {
		return false
	}
	_, err := hex.DecodeString(psk)
	return err == nil
}

// ssidLine is the conf line for an SSID. Quoted — the form FireOS's own
// framework has always been given — whenever that can hold it: wpa_supplicant
// ends a quoted string at its LAST double quote (wpa_config_parse_string), so
// `"` and `\` inside are literal, and valid UTF-8 passes through byte for
// byte. Hex otherwise, for control bytes and anything not UTF-8, which the
// line-based conf cannot carry quoted. Both forms verified 2026-09-19 against
// the emOS wpa_supplicant 2.10 build parsing a real conf.
func ssidLine(b []byte) string {
	quotable := utf8.Valid(b)
	for _, c := range b {
		if c < 0x20 || c == 0x7f {
			quotable = false
		}
	}
	if quotable {
		return "\tssid=\"" + string(b) + "\""
	}
	return "\tssid=" + hex.EncodeToString(b)
}

// pskLine is the conf line for a validated passphrase. A raw 64-hex PSK is
// written bare. A passphrase is written quoted, which needs no escaping:
// wpa_supplicant ends a quoted passphrase at its LAST double quote
// (wpa_config_parse_psk, os_strrchr), so `"` and `\` inside it are literal,
// and validate has already refused control characters and newlines.
func pskLine(psk string) string {
	if isHexPSK(psk) {
		return "\tpsk=" + strings.ToLower(psk)
	}
	return "\tpsk=\"" + psk + "\""
}
