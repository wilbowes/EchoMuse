// Both bases are exercised from a host that is neither, which is the reason
// the paths and the platform lookup are vars rather than constants.
//
// The parsing half of this has a twin in C: wpa_ctrl_dir in emos/init/init.c,
// checked by emos/init/wpacheck.c. They read the SAME file on the same device
// and must agree — init uses the answer for the reassociate nudge, this package
// uses it for scan and status, and each is silent about the other being wrong.
// Keep the two case lists in step.
package wifi

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/platform"
)

// onBase points the package at a temp directory and a chosen base OS, writing
// whichever confs the case needs. An empty string means that conf is absent.
func onBase(t *testing.T, base, androidBody, emosBody string) {
	t.Helper()
	dir := t.TempDir()

	oldBase, oldA, oldE := baseOS, androidConfP, emosConfP
	t.Cleanup(func() { baseOS, androidConfP, emosConfP = oldBase, oldA, oldE })

	baseOS = func() string { return base }
	androidConfP = filepath.Join(dir, "wpa_supplicant.conf")
	emosConfP = filepath.Join(dir, "wpa.conf")

	for path, body := range map[string]string{androidConfP: androidBody, emosConfP: emosBody} {
		if body == "" {
			continue
		}
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatalf("writing %s: %v", path, err)
		}
	}
}

const (
	androidDir = "/data/misc/wifi/sockets"
	emosDir    = "/data/emos/sockets"
)

func TestSockDirReadsTheConfInUse(t *testing.T) {
	cases := []struct {
		name    string
		base    string
		android string
		emos    string
		want    string
	}{
		// The two confs that exist on devices today.
		{"fireos, the wizard's skeleton", platform.FireOS,
			"ctrl_interface=" + androidDir + "\nupdate_config=1\n", "", androidDir},
		{"fireos, Amazon's DIR= form", platform.FireOS,
			"ctrl_interface=DIR=" + androidDir + " GROUP=wifi\n", "", androidDir},
		{"emos, em-wifi's own conf", platform.EmOS,
			"", "ctrl_interface=" + emosDir + "\nupdate_config=1\n", emosDir},

		// The migration case: a device crossed over from FireOS carrying
		// Android's conf and never told its network here. It must read the
		// file the supplicant was actually started with, which is Android's.
		{"emos, crossed over from fireos", platform.EmOS,
			"ctrl_interface=" + androidDir + "\n", "", androidDir},

		// ...and once it HAS been told, emOS's own file wins, matching
		// wpa_conf() in init.c. Both present, and they disagree — which is
		// precisely the state a migrated device that later ran em-wifi is in.
		{"emos, both present, ours wins", platform.EmOS,
			"ctrl_interface=" + androidDir + "\n", "ctrl_interface=" + emosDir + "\n", emosDir},

		// Position and surroundings must not matter: composeConf puts globals
		// above it on FireOS, and nothing guarantees an ordering.
		{"not the first line", platform.EmOS, "",
			"update_config=1\nap_scan=1\nctrl_interface=" + emosDir + "\n", emosDir},
		{"leading whitespace", platform.EmOS, "",
			"  \tctrl_interface=" + emosDir + "\n", emosDir},
		{"trailing whitespace", platform.EmOS, "",
			"ctrl_interface=" + emosDir + "   \n", emosDir},
		{"no trailing newline", platform.EmOS, "",
			"ctrl_interface=" + emosDir, emosDir},

		// Last wins, as in hostap, which processes globals line by line and
		// overwrites. wpacheck.c pins the same rule on the C side.
		{"two declarations, last wins", platform.EmOS, "",
			"ctrl_interface=" + androidDir + "\nctrl_interface=" + emosDir + "\n", emosDir},

		// Everything else falls back to Android's directory — what every conf
		// written before this declared, so the fallback is the old behaviour.
		{"no conf at all", platform.EmOS, "", "", androidDir},
		{"empty conf", platform.EmOS, "", " ", androidDir},
		{"no ctrl_interface", platform.EmOS, "",
			"update_config=1\nnetwork={\n\tssid=\"x\"\n}\n", androidDir},
		{"empty value", platform.EmOS, "", "ctrl_interface=\n", androidDir},

		// A relative value is hostap's abstract socket namespace, which
		// `wpa_cli -p` cannot address — the default beats an argument that
		// looks plausible and can never connect.
		{"relative value", platform.EmOS, "", "ctrl_interface=wpa_ctrl\n", androidDir},

		// A prefix match on a different key would read somebody else's value.
		{"a longer key starting the same", platform.EmOS, "",
			"ctrl_interface_group=wifi\n", androidDir},
		{"the key commented out", platform.EmOS, "",
			"#ctrl_interface=" + emosDir + "\n", androidDir},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			onBase(t, c.base, c.android, c.emos)
			if got := sockDir(); got != c.want {
				t.Errorf("sockDir() = %q, want %q", got, c.want)
			}
		})
	}
}

func TestConfPathsPrefersOurNamespaceForWrites(t *testing.T) {
	// FireOS reads and writes Android's file, and nothing about that changes.
	onBase(t, platform.FireOS, "ctrl_interface=x\n", "")
	if read, write := confPaths(); read != androidConfP || write != androidConfP {
		t.Errorf("fireos: read %q write %q, want both %q", read, write, androidConfP)
	}

	// An emOS device that has been told its network here uses our file for
	// both.
	onBase(t, platform.EmOS, "", "ctrl_interface=x\n")
	if read, write := confPaths(); read != emosConfP || write != emosConfP {
		t.Errorf("emos: read %q write %q, want both %q", read, write, emosConfP)
	}

	// The migration case, and the only one where the two differ: read the
	// conf the supplicant is running from, write to the namespace FireOS 6
	// cannot clobber.
	onBase(t, platform.EmOS, "ctrl_interface=x\n", "")
	read, write := confPaths()
	if read != androidConfP {
		t.Errorf("migrated device: read %q, want %q — a change must back up "+
			"the conf actually in use", read, androidConfP)
	}
	if write != emosConfP {
		t.Errorf("migrated device: write %q, want %q", write, emosConfP)
	}

	// Neither present: an emOS device nobody has configured. Write ours.
	onBase(t, platform.EmOS, "", "")
	if _, write := confPaths(); write != emosConfP {
		t.Errorf("unconfigured emos: write %q, want %q", write, emosConfP)
	}
}

func TestBackupSitsBesideTheConfItBacksUp(t *testing.T) {
	// On FireOS the current and legacy locations are the same file, so an
	// upgrade has nothing to find in two places.
	onBase(t, platform.FireOS, "ctrl_interface=x\n", "")
	if backupPath() != legacyBackupPath() {
		t.Errorf("fireos: backup %q and legacy %q should be the same path",
			backupPath(), legacyBackupPath())
	}

	// On emOS it moves into our namespace, and the legacy path still points
	// where pre-upgrade firmware wrote it — RecoverIfPending reads both.
	onBase(t, platform.EmOS, "", "ctrl_interface=x\n")
	if got, want := backupPath(), emosConfP+".echomuse-bak"; got != want {
		t.Errorf("emos: backup %q, want %q", got, want)
	}
	if got, want := legacyBackupPath(), androidConfP+".echomuse-bak"; got != want {
		t.Errorf("emos: legacy backup %q, want %q", got, want)
	}
}

func TestComposeConfKeepsTheSocketWhereItWas(t *testing.T) {
	// Writing the socket directory as a constant would move it out from under
	// init's reassociate nudge and em-wifi, and nothing in that failure names
	// composeConf. So a change on a migrated device must carry Android's
	// directory into emOS's file.
	onBase(t, platform.EmOS, "ctrl_interface="+androidDir+"\n", "")
	conf := composeConf("net", "12345678")
	if !strings.Contains(conf, "ctrl_interface="+androidDir+"\n") {
		t.Errorf("composeConf dropped the socket directory in use:\n%s", conf)
	}

	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	if conf := composeConf("net", "12345678"); !strings.Contains(conf, "ctrl_interface="+emosDir+"\n") {
		t.Errorf("composeConf dropped emOS's socket directory:\n%s", conf)
	}
}

func TestComposeConfOmitsWpsAndP2pOnEmos(t *testing.T) {
	// Our supplicant is hostap 2.10 built with neither CONFIG_WPS nor
	// CONFIG_P2P (emos/tools/build-wpa-supplicant.sh), so these are fields it
	// cannot use. It tolerates them — the build patches unknown globals to warn
	// and carry on, so a conf left by FireOS 6 does not cost the network — but
	// that patch is the safety net, not a licence to write dead lines.
	unsupported := []string{
		"driver_param=", "device_name=", "manufacturer=", "model_name=",
		"model_number=", "serial_number=", "device_type=", "os_version=",
		"config_methods=", "p2p_no_group_iface=", "external_sim=",
		"wowlan_triggers=",
	}

	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	conf := composeConf("net", "12345678")
	for _, f := range unsupported {
		if strings.Contains(conf, f) {
			t.Errorf("emos conf carries %q, which our supplicant cannot use:\n%s", f, conf)
		}
	}
	// The parts that must survive the trim.
	for _, want := range []string{"update_config=1", "ssid=\"net\"", "psk=\"12345678\"", "key_mgmt=WPA-PSK"} {
		if !strings.Contains(conf, want) {
			t.Errorf("emos conf is missing %q:\n%s", want, conf)
		}
	}

	// FireOS keeps the wizard's template: the framework populates those
	// fields, and the long tail of FireOS 5 devices is the case they are for.
	onBase(t, platform.FireOS, "ctrl_interface="+androidDir+"\n", "")
	conf = composeConf("net", "12345678")
	for _, f := range unsupported {
		if !strings.Contains(conf, f) {
			t.Errorf("fireos conf lost %q from the wizard's template:\n%s", f, conf)
		}
	}
}

func TestComposeConfOpenNetwork(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	conf := composeConf("open", "")
	if !strings.Contains(conf, "key_mgmt=NONE") {
		t.Errorf("an empty psk must produce an open network:\n%s", conf)
	}
	if strings.Contains(conf, "psk=") {
		t.Errorf("an open network must carry no psk:\n%s", conf)
	}
}
