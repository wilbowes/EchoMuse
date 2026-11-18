package led

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestPrivacyDriverInterfaces(t *testing.T) {
	defer func() { privacyOnce = sync.Once{}; privacyPath = "" }()
	for _, gpio := range []bool{false, true} {
		name, state, trigger := "amz_privacy", "privacy_state", "privacy_trigger"
		if gpio {
			name, state, trigger = "gpio-privacy", "state", "enable"
		}
		privacyPath = filepath.Join(t.TempDir(), name)
		if err := os.Mkdir(privacyPath, 0700); err != nil {
			t.Fatal(err)
		}
		privacyOnce = sync.Once{}
		privacyOnce.Do(func() {})
		if !PrivacyDriver() || GPIOPrivacyDriver() != gpio {
			t.Fatal("wrong privacy interface")
		}
		for _, value := range []string{"0\n", "1\n", "invalid"} {
			if err := os.WriteFile(filepath.Join(privacyPath, state), []byte(value), 0600); err != nil {
				t.Fatal(err)
			}
			muted, err := PrivacyMuted()
			if value == "invalid" {
				if err == nil {
					t.Fatal("invalid state accepted as unmuted")
				}
			} else if err != nil || muted != (value == "1\n") {
				t.Fatalf("state %q: %v %v", value, muted, err)
			}
		}
		if !gpio {
			if err := os.WriteFile(filepath.Join(privacyPath, "privacy_timer_on"), []byte("1\n"), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if entering, err := PrivacyEntering(); err != nil || entering == gpio {
			t.Fatalf("entering: %v %v", entering, err)
		}
		if err := EnterPrivacy(); err != nil {
			t.Fatal(err)
		}
		if value, err := os.ReadFile(filepath.Join(privacyPath, trigger)); err != nil || string(value) != "1" {
			t.Fatalf("wrong entry command: %q %v", value, err)
		}
		if err := os.Remove(filepath.Join(privacyPath, state)); err != nil {
			t.Fatal(err)
		}
		if _, err := PrivacyMuted(); err == nil {
			t.Fatal("missing state accepted as unmuted")
		}
	}
}
