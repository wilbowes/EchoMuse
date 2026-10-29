// The WiFi change on a board whose profile states a DHCP service, which is the
// Echo Dot 3rd gen under FireOS: no framework, Amazon's supplicant and DHCP
// service, and the guest-network finding that a lease carried across networks
// leaves the device without IPv4 (2026-10-22).
package wifi

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/platform"
)

// onDHCPBoard puts the package on a board that states the Dot 3's DHCP
// service, under the given base, with a conf, a DHCP directory and every
// command recorded in order rather than run.
func onDHCPBoard(t *testing.T, base string, wpaErr error) (calls *[]string, dhcp string) {
	t.Helper()
	onBase(t, base, "ctrl_interface="+androidDir+"\nupdate_config=1\nnetwork={\n\tssid=\"Valinor\"\n}\n", "")

	oldRun, oldWpa, oldDhcp, oldChown := runCommand, wpaCliExec, dhcpDirP, chownFile
	t.Cleanup(func() { runCommand, wpaCliExec, dhcpDirP, chownFile = oldRun, oldWpa, oldDhcp, oldChown })

	var log []string
	dhcpServiceOf = func() string { return "dhcpcd_wlan0" }
	chownFile = func(string, int, int) error { return nil }
	runCommand = func(name string, args ...string) error {
		log = append(log, name+" "+strings.Join(args, " "))
		return nil
	}
	wpaCliExec = func(args ...string) (string, error) {
		log = append(log, "wpa_cli "+args[len(args)-1])
		if wpaErr != nil {
			return "Failed to connect to non-global ctrl_ifname", wpaErr
		}
		return "OK\n", nil
	}
	dhcpDirP = t.TempDir()
	return &log, dhcpDirP
}

func TestDonutReloadWritesRereadsThenClearsTheAddress(t *testing.T) {
	calls, dhcp := onDHCPBoard(t, platform.FireOS, nil)
	for _, name := range []string{
		"dhcpcd-wlan0.lease", "dhcpcd-wlan0-Valinor.lease", "dhcpcd-wlan0.lease6", // ours
		"dhcpcd-wlan1.lease", "dhcpcd-wlan0.pid", "dhcpcd.duid", // not leases of wlan0
	} {
		if err := os.WriteFile(filepath.Join(dhcp, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}

	conf := composeConf([]byte("Holodeck 3"), "pa$$w@rd")
	if err := reloadConf(conf); err != nil {
		t.Fatalf("reloadConf: %v", err)
	}

	got, _ := os.ReadFile(androidConfP)
	if string(got) != conf {
		t.Fatalf("conf on disk is not the one written:\n%s", got)
	}
	want := []string{"wpa_cli reconfigure", "stop dhcpcd_wlan0", "ifconfig wlan0 0.0.0.0"}
	if strings.Join(*calls, "|") != strings.Join(want, "|") {
		t.Fatalf("commands = %q, want %q (reconfigure first, so nothing can start DHCP on the old network after the lease is gone)", *calls, want)
	}
	for _, kept := range []string{"dhcpcd-wlan1.lease", "dhcpcd-wlan0.pid", "dhcpcd.duid"} {
		if _, err := os.Stat(filepath.Join(dhcp, kept)); err != nil {
			t.Errorf("%s was removed; only wlan0's leases should be", kept)
		}
	}
	for _, gone := range []string{"dhcpcd-wlan0.lease", "dhcpcd-wlan0-Valinor.lease", "dhcpcd-wlan0.lease6"} {
		if _, err := os.Stat(filepath.Join(dhcp, gone)); err == nil {
			t.Errorf("%s survived: the new network would be offered the old lease", gone)
		}
	}
}

func TestDonutReloadStandsWhenTheSupplicantIsDown(t *testing.T) {
	// Startup recovery can run before Amazon's supplicant is up. The restored
	// conf on disk is what it will read, so this must not report failure and
	// leave the marker to retry forever.
	calls, _ := onDHCPBoard(t, platform.FireOS, errors.New("exit status 255"))
	if err := reloadConf("ctrl_interface=" + androidDir + "\n"); err != nil {
		t.Fatalf("reloadConf with no supplicant: %v", err)
	}
	if len(*calls) != 3 {
		t.Fatalf("the address must still be cleared: %q", *calls)
	}
}

func TestDonutOnEmosTakesTheEmosPath(t *testing.T) {
	// Upstream runs emOS on the Dot 3 (#1036), where em-wifi and init own the
	// supplicant. The stated service must not pull it onto the FireOS road: the conf
	// goes to our file, and no Dot 3 command runs. reloadConf's own result is
	// not asserted — retireSupplicant walks /proc, which a host may not have.
	calls, _ := onDHCPBoard(t, platform.EmOS, nil)
	conf := composeConf([]byte("Holodeck 3"), "pa$$w@rd")
	_ = reloadConf(conf)
	got, err := os.ReadFile(emosConfP)
	if err != nil || string(got) != conf {
		t.Fatalf("emOS conf not written to %s: %v\n%s", emosConfP, err, got)
	}
	if len(*calls) != 0 {
		t.Fatalf("an emOS Dot 3 ran FireOS Dot 3 commands: %q", *calls)
	}
	if strings.Contains(conf, "ieee80211w") {
		t.Fatalf("the emOS conf changed, and nothing was measured there:\n%s", conf)
	}
	if ipWait() != ipTimeout {
		t.Fatalf("emOS Dot 3 ipWait = %s", ipWait())
	}
	startDHCP()
	if len(*calls) != 0 {
		t.Fatalf("on emOS DHCP is init's to run: %q", *calls)
	}
}

func TestDonutStartsDHCPOnlyOnTheDot3(t *testing.T) {
	calls, _ := onDHCPBoard(t, platform.FireOS, nil)
	startDHCP()
	if strings.Join(*calls, "|") != "start dhcpcd_wlan0" {
		t.Fatalf("on the Dot 3: %q", *calls)
	}
	*calls = nil
	dhcpServiceOf = func() string { return "" }
	startDHCP()
	if len(*calls) != 0 {
		t.Fatalf("off the Dot 3 DHCP is not ours to start: %q", *calls)
	}
}

func TestDonutGivesDHCPLonger(t *testing.T) {
	onDHCPBoard(t, platform.FireOS, nil)
	if ipWait() != ownDHCPIPTimeout {
		t.Fatalf("Dot 3 ipWait = %s", ipWait())
	}
	dhcpServiceOf = func() string { return "" }
	if ipWait() != ipTimeout {
		t.Fatalf("other boards ipWait = %s", ipWait())
	}
}

func TestDonutConfAsksForOptionalPMF(t *testing.T) {
	onDHCPBoard(t, platform.FireOS, nil)
	if c := composeConf([]byte("Holodeck 3"), "secretpass"); !strings.Contains(c, "\tieee80211w=1\n") {
		t.Fatalf("Dot 3 conf lacks optional PMF:\n%s", c)
	}
	if c := composeConf([]byte("Open"), ""); strings.Contains(c, "ieee80211w") {
		t.Fatalf("an open network has no PMF to negotiate:\n%s", c)
	}

	dhcpServiceOf = func() string { return "" }
	if c := composeConf([]byte("Holodeck 3"), "secretpass"); strings.Contains(c, "ieee80211w") {
		t.Fatalf("the Dot 2's conf changed, and nothing was measured there:\n%s", c)
	}
}

func TestOffTheDot3ReloadNeverTouchesDHCP(t *testing.T) {
	// A board that states no DHCP service: the Dot 2, and any board nothing
	// identified. FireOS 5's path runs svc, which a host cannot; the point
	// is that it errors out on its own road, not down the Dot 3's.
	calls, _ := onDHCPBoard(t, platform.FireOS, nil)
	dhcpServiceOf = func() string { return "" }
	_ = reloadConf("x")
	for _, c := range *calls {
		if strings.Contains(c, "dhcpcd") || strings.HasPrefix(c, "ifconfig") {
			t.Fatalf("the Dot 2 path ran a Dot 3 command: %q", *calls)
		}
	}
}
