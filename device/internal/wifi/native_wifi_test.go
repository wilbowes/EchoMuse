package wifi

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeProfileFromConf(t *testing.T) {
	first := "network={\nssid=6669727374\npsk=" + strings.Repeat("a", 64) + "\nkey_mgmt=WPA-PSK\n}\n"
	second := "network={\nssid=\"second\\\"net\"\nkey_mgmt=NONE\n}\n"
	p, err := nativeProfileFromConf(first, nil)
	if err != nil || !bytes.Equal(p.ssid, []byte("first")) || p.auth != "wpa2" || p.psk != strings.Repeat("a", 64) {
		t.Fatalf("hex personal profile: %#v %v", p, err)
	}
	p, err = nativeProfileFromConf(first+second, []byte("second\\\"net"))
	if err != nil || p.auth != "open" || string(p.ssid) != "second\\\"net" {
		t.Fatalf("selected literal SSID: %#v %v", p, err)
	}
	for _, bad := range []string{
		first + second, first + first, "network={\nssid=6100\nkey_mgmt=NONE\n}\n",
		"network={\nssid=zz\nkey_mgmt=NONE\n}\n",
		"network={\nssid=\"x\"\nkey_mgmt=WPA-EAP\n}\n",
		"network={\nssid=\"x\"\nssid=\"y\"\nkey_mgmt=NONE\n}\n",
		"network={\nssid=\"x\"\nkey_mgmt=WPA-PSK\n}\n", "network={\nssid=\"x\"\n"} {
		if _, err := nativeProfileFromConf(bad, nil); err == nil {
			t.Fatalf("unsafe/ambiguous profile accepted: %q", bad)
		}
	}
	if _, err := nativeProfileFromConf(first, []byte("missing")); err == nil {
		t.Fatal("missing preferred profile accepted")
	}
}

func TestNativeCommandReceipt(t *testing.T) {
	for _, tc := range []struct {
		output string
		want   bool
	}{
		{"Ret: 0 Duration : 1 ms\n", true},
		{" auth_mode=WPA2 psk=failed-password\nRet: 0 Duration : 1 ms", true},
		{"error updating network.\nRet: 0 Duration : 1 ms", false},
		{"Failed to set uid/gid\nRet: 0 Duration : 1 ms", false},
		{"Ret: -11 Duration : 1 ms", false},
		{"Ret: 0\nRet: -1", false},
		{"", false},
	} {
		if got := nativeCommandSucceeded(tc.output); got != tc.want {
			t.Fatalf("receipt %q = %v; want %v", tc.output, got, tc.want)
		}
	}
}

func TestFailedCommitPreservesRecovery(t *testing.T) {
	oldMarker, oldLegacy, oldConf, oldBase, oldPending, oldUnconfirmed := markerP, legacyMarkP, androidConfP, baseOS, pending, unconfirmed
	defer func() {
		markerP, legacyMarkP, androidConfP, baseOS, pending, unconfirmed = oldMarker, oldLegacy, oldConf, oldBase, oldPending, oldUnconfirmed
	}()
	dir := t.TempDir()
	markerP, legacyMarkP = filepath.Join(dir, "pending"), filepath.Join(dir, "legacy-pending")
	androidConfP = filepath.Join(dir, "wpa.conf")
	baseOS = func() string { return "fireos" }
	for _, path := range []string{markerP, legacyMarkP, backupPath()} {
		if err := os.WriteFile(path, []byte("recovery"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	pending, unconfirmed = &Result{OK: false}, false
	Commit()
	for _, path := range []string{markerP, legacyMarkP, backupPath()} {
		if data, err := os.ReadFile(path); err != nil || string(data) != "recovery" {
			t.Fatalf("failure acknowledgement discarded %s: %v", path, err)
		}
	}
	// A later rejected request has replaced the success in pending; the ack
	// still commits the change that succeeded.
	pending, unconfirmed = &Result{OK: false}, true
	Commit()
	for _, path := range []string{markerP, legacyMarkP, backupPath()} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("successful commit retained %s: %v", path, err)
		}
	}
}

func TestNativeVendorPersonalKeyManagement(t *testing.T) {
	for _, mode := range []string{"WPA-PSK WPA-PSK-SHA256", "WPA-PSK-SHA256 WPA-PSK"} {
		conf := "network={\nssid=\"test\"\npsk=" + strings.Repeat("a", 64) + "\nkey_mgmt=" + mode + "\n}\n"
		p, err := nativeProfileFromConf(conf, []byte("test"))
		if err != nil || p.auth != "wpa2" || p.psk != strings.Repeat("a", 64) {
			t.Fatalf("vendor WPA2 profile: %v", err)
		}
	}
	if _, err := nativeProfileFromConf("network={\nssid=\"test\"\nkey_mgmt=WPA-PSK WPA-EAP\n}\n", nil); err == nil {
		t.Fatal("enterprise mode accepted as personal")
	}
}

func TestNativeNetworkState(t *testing.T) {
	for _, state := range []string{"CONNECTING", "CONNECTED", "DISCONNECTING", "DISCONNECTED", "L2CONNECTED"} {
		if got := nativeNetworkState("calling SDK\nnetworkState: " + state + "\r\nRet: 0\n"); got != state {
			t.Fatalf("state %s: %s", state, got)
		}
	}
	for _, bad := range []string{"", "networkState: INVALID\n", "psk=DISCONNECTED\n", "networkState: CONNECTED\nnetworkState: DISCONNECTED\n"} {
		if nativeNetworkState(bad) != "UNKNOWN" {
			t.Fatal("ambiguous/invalid native state accepted")
		}
	}
}

func TestNativePlanRefusesBeforeDisconnecting(t *testing.T) {
	conf := "network={\nssid=\"home\"\npsk=\"passphrase\"\nkey_mgmt=WPA-PSK\n}\n" +
		"network={\nssid=\"work\"\nkey_mgmt=WPA-EAP\n}\n"
	target, previous, err := nativePlan([]byte("new"), "", conf, []byte("home"))
	if err != nil || target.auth != "open" || string(previous.ssid) != "home" || previous.psk != "passphrase" {
		t.Fatalf("restorable plan: %#v %#v %v", target, previous, err)
	}
	if target, _, err = nativePlan([]byte("new"), strings.Repeat("A", 64), conf, []byte("home")); err != nil || target.psk != strings.Repeat("a", 64) {
		t.Fatalf("raw PSK target: %v", err)
	}
	for name, current := range map[string][]byte{"unknown": nil, "enterprise": []byte("work"), "absent": []byte("gone")} {
		if _, _, err := nativePlan([]byte("new"), "passphrase", conf, current); err == nil {
			t.Fatalf("change accepted with an unrestorable current network (%s)", name)
		}
	}
	if _, _, err := nativePlan([]byte{'a', 0, 'b'}, "passphrase", conf, []byte("home")); err == nil {
		t.Fatal("NUL SSID accepted")
	}
}
