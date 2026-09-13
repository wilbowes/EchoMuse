// Package wifi implements safe WiFi network changes with automatic
// rollback, plus scan/status queries for the dashboard Connectivity tab.
//
// It runs on BOTH bases from one firmware — see the "Which base OS" section
// below and internal/platform. Everything down to reloadConf is shared; what
// differs is which file the config lives in, where the supplicant's control
// socket is, and how the supplicant is made to re-read it. FireOS is expected
// to have a long tail, so the framework path is not transitional.
//
// The FireOS mechanics mirror the provisioning wizard's runConfigWifi
// (controller/static/dashboard.jsx), which was hard-won on real hardware:
//
//   - The only safe reload path THERE is `svc wifi disable` + `svc wifi enable`.
//     The framework-managed wpa_supplicant instance auto-associates and
//     gets a DHCP lease on its own. Never use raw `start wpa_supplicant`,
//     kill -9, manual wpa_cli reconnect, or manual dhcpcd — a second bare
//     supplicant instance fights the framework one over wlan0 and the
//     interface dies (INTERFACE_DISABLED, never recovers).
//   - The config is a FULL replacement of wpa_supplicant.conf with a
//     single network block — no ambiguity about which AP it joins.
//   - wpa_cli needs BOTH -p <socket dir> (never the default) and -i wlan0.
//     The directory is declared inside the conf and is NOT the same on both
//     bases, so it is read from there rather than written down — see sockDir.
//
// Two further lessons found on hardware 2026-07-11, AFTER the wizard:
//
//   - /system/bin/svc is a shebang-less shell script: execve (and hence
//     Go's exec.Command("svc", ...)) fails ENOEXEC. It must be run via
//     /system/bin/sh, and the disable must be VERIFIED to have dropped
//     association — a silent no-op bounce makes every gate below pass
//     against the old network and falsely commits the new conf.
//   - The conf must be written while WiFi is DOWN: on `svc wifi disable`,
//     WifiStateMachine saves its in-memory network list back over
//     wpa_supplicant.conf, clobbering anything written beforehand (the
//     wizard got away with write-then-bounce only because a factory
//     device has no framework-known networks to save). See reloadConf.
//
// Unlike the wizard (ADB shell), this package runs inside the root Go
// binary, so file writes use plain os.WriteFile — none of the mksh
// redirect quirks apply. On FireOS ownership must still be restored to
// wifi:wifi (AID_WIFI=1010) mode 0660 or the framework can't read the config;
// on emOS there is no such user and the file holds a PSK, so it is 0600.
//
// Safety model (the connection to the controller dies mid-change, so the
// device owns the whole sequence):
//
//  1. Back up the current conf and drop a pending marker file.
//  2. Swap the conf in and get the supplicant onto it (reloadConf — which of
//     the two sequences depends on the base).
//  3. Gates: associate to the TARGET SSID ≤45s → IPv4 on wlan0 ≤20s →
//     control WebSocket re-registered ≤90s. Any failure → restore the
//     backup the same way and report the failure once the connection
//     returns.
//  4. On success the controller sends wifi_commit, which deletes the
//     marker + backup. Until then the change is provisional.
//  5. Crash safety: if the marker exists at process start, a previous
//     switch never got committed — RecoverIfPending restores the backup
//     and bounces, so a crash or power cycle mid-switch self-heals back
//     to the old network (same philosophy as the A/B binary slots).
package wifi

import (
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/platform"
)

const (
	androidConf = "/data/misc/wifi/wpa_supplicant.conf"
	emosConf    = "/data/emos/wpa.conf"
	markerPath  = "/data/local/tmp/echomuse_wifi_pending"

	// Fallback control socket directory, and the default on FireOS. Mirrors
	// WPA_CTRL_DEFAULT in emos/init/init.c — the real value is read out of
	// the conf by sockDir, because on emOS it is not a constant.
	defaultSockDir = "/data/misc/wifi/sockets"
	iface          = "wlan0"

	// AID_WIFI — fixed uid/gid on Android; the framework reads the conf
	// as this user.
	aidWifi = 1010

	// 20s (the provisioning wizard's window) proved too tight on hardware
	// for a network the framework hasn't joined before — autojoin's scan
	// cycle alone can eat most of it. Reverts re-associate to a known
	// network well inside 20s, so only first-join pays the longer wait.
	associateTimeout = 45 * time.Second
	ipTimeout        = 20 * time.Second
	// The reconnect gate covers mDNS rediscovery plus the control client's
	// 5s retry cadence; generous because a false negative reverts a
	// perfectly good network change.
	reconnectTimeout = 90 * time.Second
)

// Result is the outcome of a change attempt, reported to the controller
// as a wifi_result message once a connection exists to carry it.
type Result struct {
	OK    bool   `json:"ok"`
	SSID  string `json:"ssid"`
	Error string `json:"error,omitempty"`
}

// Network is one scan result row.
type Network struct {
	SSID   string `json:"ssid"`
	Signal int    `json:"signal"`
}

type marker struct {
	NewSSID   string `json:"newSsid"`
	StartedAt int64  `json:"startedAt"`
}

var (
	mu       sync.Mutex
	inFlight bool
	// pending holds an unreported Result until the controller connection
	// can carry it (drained by TakeResult from the OnConnected callback).
	pending *Result
)

// ─── Which base OS, and therefore which paths ─────────────────────────────────
//
// ONE firmware runs on both bases and resolves this at runtime — see
// internal/platform for why that is a value rather than a build flag. FireOS is
// expected to have a long tail, so neither path is transitional: the framework
// sequence below is what most of the fleet runs and is hardware-proven, and the
// emOS one sits beside it rather than replacing it.
//
// Indirected through vars so both bases are exercisable from a host that is
// neither, which is the same reason platform.Detect takes a root.
var (
	baseOS       = platform.Base
	androidConfP = androidConf
	emosConfP    = emosConf
)

func onEmOS() bool { return baseOS() == platform.EmOS }

// confPaths says where the supplicant's config is read from and written to.
//
// The READ rule mirrors wpa_conf() in emos/init/init.c exactly — emOS's own
// file when it exists, Android's otherwise — because reading a different file
// than the running supplicant was started with is how every value below ends up
// describing a config nothing is using.
//
// The WRITE path on emOS is always emOS's own file, whichever one it is reading
// now. That namespace is the whole point: anything under /data/misc/wifi
// belongs to whichever OS booted last, and booting FireOS 6 from the other slot
// rewrites it with fields our supplicant survives only by patch.
//
// So the two differ on exactly one device — an emOS install that crossed over
// from FireOS and has never been told its network here. A change there backs up
// Android's file and writes ours, which then takes precedence; a revert
// restores the old content into ours, so the network that comes back is the one
// that was there. Android's copy is left alone deliberately, as the thing to
// fall back to if ours is ever lost.
func confPaths() (read, write string) {
	if !onEmOS() {
		return androidConfP, androidConfP
	}
	if _, err := os.Stat(emosConfP); err == nil {
		return emosConfP, emosConfP
	}
	if _, err := os.Stat(androidConfP); err == nil {
		return androidConfP, emosConfP
	}
	return emosConfP, emosConfP
}

// backupPath keeps the pre-change config beside the config it backs up, for
// the same reason em-wifi writes to /data/emos at all — a backup in Android's
// directory is one FireOS is free to clobber, and it would be read back on
// precisely the boot where the current config has already been lost.
func backupPath() string {
	_, write := confPaths()
	return write + ".echomuse-bak"
}

// legacyBackupPath is where every firmware before this one wrote the backup,
// on both bases. RecoverIfPending has to look here too: the marker outlives an
// OTA, so a change still in flight when the device takes new firmware comes
// back to a process looking in a place the old one never wrote. It would
// recover — "no backup to restore" clears the marker and keeps the current
// conf — but the current conf is the UNCONFIRMED one, which is the network
// nobody has established works, and the backup sitting on disk is the answer.
func legacyBackupPath() string { return androidConfP + ".echomuse-bak" }

// sockDir reads the supplicant's control socket directory out of the conf.
//
// It cannot be a constant: em-wifi writes /data/emos/sockets into emOS's own
// file while a wizard-provisioned device carries Android's, and confPaths
// prefers ours the moment it exists — so the same device moves from one to the
// other the first time somebody sets WiFi from the console. A wpa_cli pointed
// at the wrong one fails with "Failed to connect to non-global ctrl_ifname",
// which here reads as a device that cannot scan and reports no SSID at all.
//
// Deliberately re-read per call rather than cached: em-wifi can move it while
// this process is running, and reading a 4KB file costs nothing beside the
// wpa_cli fork it is about to feed. The parsing matches wpa_ctrl_dir in
// emos/init/init.c, including last-declaration-wins — pinned by test on both
// sides, since the two halves are in different languages in different trees.
func sockDir() string {
	read, _ := confPaths()
	b, err := os.ReadFile(read)
	if err != nil {
		return defaultSockDir
	}
	dir := defaultSockDir
	for _, line := range strings.Split(string(b), "\n") {
		v, ok := strings.CutPrefix(strings.TrimLeft(line, " \t"), "ctrl_interface=")
		if !ok {
			continue
		}
		// Two spellings, both legal and both in the field: Amazon's conf
		// uses hostap's "DIR=/path GROUP=wifi" form, ours a bare path.
		v = strings.TrimPrefix(v, "DIR=")
		if i := strings.IndexAny(v, " \t"); i >= 0 {
			v = v[:i]
		}
		// A relative value is hostap's abstract socket namespace, which
		// `wpa_cli -p` cannot address — keep the default rather than pass on
		// an argument that looks plausible and can never connect.
		if strings.HasPrefix(v, "/") {
			dir = v
		}
	}
	return dir
}

// ─── Queries ──────────────────────────────────────────────────────────────────

func wpaCli(args ...string) (string, error) {
	full := append([]string{"-p", sockDir(), "-i", iface}, args...)
	out, err := exec.Command("wpa_cli", full...).CombinedOutput()
	return string(out), err
}

// CurrentSSID returns the associated SSID, or "" when not associated.
func CurrentSSID() string {
	out, _ := wpaCli("status")
	if !strings.Contains(out, "wpa_state=COMPLETED") {
		return ""
	}
	for _, line := range strings.Split(out, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "ssid="); ok {
			return v
		}
	}
	return ""
}

// currentIPv4 returns the interface's IPv4 address, or "".
func currentIPv4() string {
	ifi, err := net.InterfaceByName(iface)
	if err != nil {
		return ""
	}
	addrs, err := ifi.Addrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if ipn, ok := a.(*net.IPNet); ok {
			if v4 := ipn.IP.To4(); v4 != nil {
				return v4.String()
			}
		}
	}
	return ""
}

// Scan triggers a wpa_cli scan and returns networks sorted strongest
// first, deduped by SSID (strongest AP wins — multiple APs/bands share
// SSIDs). Safe while associated; expect a brief audio-free RF glitch.
func Scan() ([]Network, error) {
	if _, err := wpaCli("scan"); err != nil {
		return nil, fmt.Errorf("scan trigger: %w", err)
	}
	time.Sleep(4 * time.Second)
	out, err := wpaCli("scan_results")
	if err != nil {
		return nil, fmt.Errorf("scan_results: %w", err)
	}

	best := map[string]int{}
	for _, line := range strings.Split(out, "\n") {
		// bssid \t frequency \t signal \t flags \t ssid
		parts := strings.Split(line, "\t")
		if len(parts) < 5 {
			continue
		}
		ssid := strings.TrimSpace(parts[4])
		if ssid == "" || ssid == "SSID" {
			continue
		}
		// Hidden networks: wpa_cli prints the zeroed SSID bytes as literal
		// \xNN escapes (e.g. \x00\x00…). Unjoinable by name — drop them.
		if hiddenSSID.MatchString(ssid) {
			continue
		}
		sig, err := strconv.Atoi(strings.TrimSpace(parts[2]))
		if err != nil {
			continue
		}
		if cur, ok := best[ssid]; !ok || sig > cur {
			best[ssid] = sig
		}
	}
	nets := make([]Network, 0, len(best))
	for ssid, sig := range best {
		nets = append(nets, Network{SSID: ssid, Signal: sig})
	}
	sort.Slice(nets, func(i, j int) bool { return nets[i].Signal > nets[j].Signal })
	return nets, nil
}

// ─── Change with rollback ─────────────────────────────────────────────────────

// hiddenSSID matches scan_results entries that are entirely \xNN escape
// sequences — wpa_cli's rendering of hidden/zeroed SSIDs.
var hiddenSSID = regexp.MustCompile(`^(\\x[0-9a-fA-F]{2})+$`)

// validCred matches wpaConfEscape in the provisioning wizard: a literal
// " or \ can't be represented safely in a wpa_supplicant.conf quoted
// string, so reject rather than mis-escape.
var validCred = regexp.MustCompile(`["\\]`)

func validate(ssid, psk string) error {
	if ssid == "" {
		return fmt.Errorf("empty SSID")
	}
	if validCred.MatchString(ssid) || validCred.MatchString(psk) {
		return fmt.Errorf("SSID/passphrase contains a double-quote or backslash, which wpa_supplicant.conf cannot represent safely")
	}
	if psk != "" && (len(psk) < 8 || len(psk) > 63) {
		return fmt.Errorf("WPA passphrase must be 8–63 characters (got %d)", len(psk))
	}
	return nil
}

func getprop(key, fallback string) string {
	out, err := exec.Command("getprop", key).Output()
	if err != nil {
		return fallback
	}
	if v := strings.TrimSpace(string(out)); v != "" {
		return v
	}
	return fallback
}

// composeConf builds the full-replacement wpa_supplicant.conf. An empty psk
// produces an open (key_mgmt=NONE) network block.
//
// The control socket is carried through from whatever the conf already declared
// (sockDir), never written as a constant. Writing the constant would MOVE the
// socket out from under everything else that talks to the supplicant — init's
// reassociate nudge and em-wifi, each of which resolves it its own way — and
// nothing in that failure names this function.
//
// On FireOS the globals are the provisioning wizard's template, including the
// WPS and P2P block the framework populates from properties. On emOS they are
// dropped: our supplicant is hostap 2.10 built without CONFIG_WPS or
// CONFIG_P2P, so every one of those lines is a field it cannot use. It survives
// them — the build patches unknown globals to warn and carry on, precisely so a
// conf left behind by FireOS 6 does not cost the network — but relying on that
// patch to absorb lines we chose to write is using the safety net as the floor.
func composeConf(ssid, psk string) string {
	network := []string{
		"network={",
		fmt.Sprintf("\tssid=%q", ssid),
	}
	if psk == "" {
		network = append(network, "\tkey_mgmt=NONE")
	} else {
		network = append(network,
			fmt.Sprintf("\tpsk=%q", psk),
			"\tkey_mgmt=WPA-PSK",
		)
	}
	network = append(network, "\tpriority=1", "}")

	lines := []string{
		"ctrl_interface=" + sockDir(),
		"update_config=1",
	}
	if !onEmOS() {
		lines = append(lines,
			"driver_param=use_p2p_group_interface=1",
			"device_name="+getprop("ro.product.name", "echomuse"),
			"manufacturer="+getprop("ro.product.manufacturer", "Amazon"),
			"model_name="+getprop("ro.product.model", "AEOBC"),
			"model_number="+getprop("ro.product.model", "AEOBC"),
			"serial_number="+getprop("ro.serialno", getprop("ro.boot.serialno", "unknown")),
			"device_type=1-0050F204-9",
			"os_version=01020300",
			"config_methods=physical_display virtual_push_button",
			"p2p_no_group_iface=1",
			"external_sim=1",
			"wowlan_triggers=disconnect",
		)
	}
	lines = append(lines, network...)
	return strings.Join(lines, "\n") + "\n"
}

func writeConf(content string) error {
	_, path := confPaths()
	if onEmOS() {
		// Nothing on emOS reads this as another user, so 0600 — and it holds
		// the PSK. MkdirAll because a device that has never had WiFi set here
		// has no /data/emos/wpa.conf and may have no /data/emos either; init
		// creates it, but only on a boot that got that far.
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return fmt.Errorf("mkdir %s: %w", filepath.Dir(path), err)
		}
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			return fmt.Errorf("write %s: %w", path, err)
		}
		return os.Chmod(path, 0o600)
	}
	// Traverse bit on the dir — 666 here made every file inside
	// unopenable (provisioning finding).
	_ = os.Chmod(filepath.Dir(path), 0o770)
	if err := os.WriteFile(path, []byte(content), 0o660); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	if err := os.Chown(path, aidWifi, aidWifi); err != nil {
		return fmt.Errorf("chown %s: %w", path, err)
	}
	return os.Chmod(path, 0o660)
}

// svcWifi toggles the framework WiFi service. /system/bin/svc is a
// shebang-less shell script — execve returns ENOEXEC on it, so it must be
// run through sh explicitly (exec.Command("svc", ...) silently no-ops).
func svcWifi(state string) error {
	out, err := exec.Command("/system/bin/sh", "/system/bin/svc", "wifi", state).CombinedOutput()
	if err != nil {
		return fmt.Errorf("svc wifi %s: %v (%s)", state, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// disableWifi brings the framework WiFi down and verifies it actually
// dropped. A no-op disable leaves wpa_supplicant running against its old
// in-memory config, and every downstream gate then passes vacuously
// against the old network — a false success that commits an untried conf.
func disableWifi() error {
	log.Println("[wifi] svc wifi disable")
	if err := svcWifi("disable"); err != nil {
		return err
	}
	if !waitFor("disassociation after disable", 10*time.Second, func() bool { return !associated() }) {
		_ = svcWifi("enable")
		return fmt.Errorf("wifi did not go down after 'svc wifi disable'")
	}
	return nil
}

func enableWifi() error {
	log.Println("[wifi] svc wifi enable")
	if err := svcWifi("enable"); err != nil {
		return err
	}
	time.Sleep(3 * time.Second)
	return nil
}

// retireSupplicant ends the running supplicant so emOS's init restarts it
// against the conf just written. That supervision loop is a 5s ticker
// (net_main in emos/init/init.c), so the replacement is along within about
// five seconds and brings up DHCP behind it.
//
// The pid is found by walking /proc rather than by shelling out to killall:
// that applet only exists because init symlinks busybox into /sbin, and a
// missing symlink would present here as a WiFi change that silently does
// nothing. /proc/<pid>/comm is the whole name — "wpa_supplicant" is 14
// characters against the kernel's 15-character field, so there is no
// truncation to match around.
func retireSupplicant() error {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return fmt.Errorf("read /proc: %w", err)
	}
	found := 0
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		comm, err := os.ReadFile("/proc/" + e.Name() + "/comm")
		if err != nil || strings.TrimSpace(string(comm)) != "wpa_supplicant" {
			continue
		}
		if err := syscall.Kill(pid, syscall.SIGTERM); err != nil {
			log.Printf("[wifi] SIGTERM to supplicant pid %d: %v", pid, err)
			continue
		}
		found++
	}
	if found == 0 {
		// Not an error: init starts one as soon as a conf exists, so a device
		// that has never had one is exactly where a first change lands.
		log.Println("[wifi] no running supplicant to retire — init will start one")
		return nil
	}
	log.Printf("[wifi] retired %d supplicant process(es) — init restarts against the new conf", found)
	return nil
}

// reloadConf swaps in a new config and gets the supplicant onto it.
//
// On FireOS the conf must be written with WiFi DOWN, and that order is what
// makes it work: on disable, WifiStateMachine saves its in-memory network list
// back to wpa_supplicant.conf, so a conf written while WiFi is up gets
// clobbered by that save and the device silently rejoins the old network (found
// on hardware 2026-07-11; provisioning never hit it because a factory device
// has no framework-known networks to save).
//
// On emOS the opposite is true and the whole dance is wrong. There is no
// framework to bounce and nothing else that writes the file, so there is no save
// to lose a write to — and `svc` needs a package manager and a property service
// that are both absent, meaning the disable would fail and take the change with
// it. Write, then retire the supplicant and let init's supervisor bring up a
// replacement, which is what em-wifi does from the console for the same reason.
func reloadConf(content string) error {
	if onEmOS() {
		if err := writeConf(content); err != nil {
			return err
		}
		return retireSupplicant()
	}
	if err := disableWifi(); err != nil {
		return err
	}
	if err := writeConf(content); err != nil {
		// Leave WiFi usable rather than down next to a bad conf.
		_ = enableWifi()
		return err
	}
	return enableWifi()
}

func waitFor(what string, timeout time.Duration, cond func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(time.Second)
	}
	log.Printf("[wifi] timed out waiting for %s (%s)", what, timeout)
	return false
}

func associated() bool {
	out, _ := wpaCli("status")
	return strings.Contains(out, "wpa_state=COMPLETED")
}

// associatedTo reports association specifically to the named network —
// bare wpa_state=COMPLETED is satisfied by the *old* network if the
// supplicant never actually restarted.
func associatedTo(ssid string) bool {
	return CurrentSSID() == ssid
}

// waitForAssociation polls for association to ssid, logging the raw
// supplicant state every 5s so a timeout in the field says what the
// framework was doing (SCANNING vs 4WAY_HANDSHAKE vs INTERFACE_DISABLED).
func waitForAssociation(ssid string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	lastDiag := time.Now()
	for time.Now().Before(deadline) {
		if associatedTo(ssid) {
			return true
		}
		if time.Since(lastDiag) >= 5*time.Second {
			out, _ := wpaCli("status")
			state := "?"
			for _, line := range strings.Split(out, "\n") {
				if v, ok := strings.CutPrefix(strings.TrimSpace(line), "wpa_state="); ok {
					state = v
					break
				}
			}
			log.Printf("[wifi] waiting for association to %q — wpa_state=%s", ssid, state)
			lastDiag = time.Now()
		}
		time.Sleep(time.Second)
	}
	log.Printf("[wifi] timed out waiting for association to %q (%s)", ssid, timeout)
	return false
}

func setResult(r Result) {
	mu.Lock()
	pending = &r
	mu.Unlock()
}

// PendingResult returns the unacknowledged change outcome, if any,
// WITHOUT clearing it. Delivery is at-least-once: the result stays
// pending (and is re-sent on reconnect and on a retry ticker) until the
// controller acks with wifi_commit — a fire-and-forget send can vanish
// into a half-open TCP connection that the interface bounce killed but
// that still looks connected to the writer (seen on hardware 2026-07-11).
func PendingResult() *Result {
	mu.Lock()
	defer mu.Unlock()
	if pending == nil {
		return nil
	}
	r := *pending
	return &r
}

// Commit handles the controller's wifi_commit ack: the provisional state
// (marker + backup) is deleted so a future crash/restart keeps the new
// network, and the pending result stops being re-sent. The controller
// acks failure results too — the revert already removed marker/backup,
// so the removes are harmless no-ops there.
func Commit() {
	_ = os.Remove(markerPath)
	_ = os.Remove(backupPath())
	mu.Lock()
	pending = nil
	mu.Unlock()
	log.Println("[wifi] result acknowledged — backup and pending marker removed")
}

// Change switches to a new network with automatic rollback. Runs
// synchronously (call from a goroutine); connected must report whether
// the control WebSocket is currently registered with the controller.
// The outcome lands in TakeResult either way.
func Change(ssid, psk string, connected func() bool) {
	mu.Lock()
	if inFlight {
		mu.Unlock()
		setResult(Result{OK: false, SSID: ssid, Error: "another WiFi change is already in progress"})
		return
	}
	inFlight = true
	pending = nil
	mu.Unlock()
	defer func() {
		mu.Lock()
		inFlight = false
		mu.Unlock()
	}()

	if err := validate(ssid, psk); err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: err.Error()})
		return
	}

	log.Printf("[wifi] change requested → %q", ssid)

	readPath, _ := confPaths()
	old, err := os.ReadFile(readPath)
	if err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot read current config: %v", err)})
		return
	}
	if err := os.WriteFile(backupPath(), old, 0o600); err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot write backup: %v", err)})
		return
	}
	mk, _ := json.Marshal(marker{NewSSID: ssid, StartedAt: time.Now().Unix()})
	if err := os.WriteFile(markerPath, mk, 0o600); err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot write pending marker: %v", err)})
		return
	}

	revert := func(reason string) {
		log.Printf("[wifi] change to %q failed (%s) — reverting", ssid, reason)
		// Restore with WiFi down (see reloadConf) — but if the restore
		// write fails, conf is beyond self-healing: leave the marker so
		// RecoverIfPending retries on next start.
		restoreErr := reloadConf(string(old))
		if restoreErr != nil {
			log.Printf("[wifi] REVERT FAILED: %v — marker left for recovery on restart", restoreErr)
		} else {
			_ = os.Remove(markerPath)
			_ = os.Remove(backupPath())
		}
		waitFor("re-association after revert", associateTimeout, associated)
		setResult(Result{OK: false, SSID: ssid, Error: reason})
	}

	if err := reloadConf(composeConf(ssid, psk)); err != nil {
		revert(err.Error())
		return
	}

	if !waitForAssociation(ssid, associateTimeout) {
		revert(fmt.Sprintf("did not associate to %q within %s (wrong passphrase or AP out of range?)", ssid, associateTimeout))
		return
	}
	log.Printf("[wifi] associated to %q", ssid)

	if !waitFor("IPv4 address", ipTimeout, func() bool { return currentIPv4() != "" }) {
		revert(fmt.Sprintf("associated to %q but no IP within %s (DHCP problem?)", ssid, ipTimeout))
		return
	}
	log.Printf("[wifi] got IP %s", currentIPv4())

	if !waitFor("controller reconnect", reconnectTimeout, connected) {
		revert(fmt.Sprintf("joined %q (IP %s) but could not reach the controller within %s — wrong VLAN or isolated network?", ssid, currentIPv4(), reconnectTimeout))
		return
	}

	// Connected on the new network. Marker + backup stay until the
	// controller acknowledges with wifi_commit.
	log.Printf("[wifi] change to %q succeeded — awaiting commit from controller", ssid)
	setResult(Result{OK: true, SSID: ssid})
}

// RecoverIfPending restores the pre-change config if a previous change
// never got committed (crash, power cycle, or a failed revert). Call
// once at process start, before the control client runs.
func RecoverIfPending() {
	mk, err := os.ReadFile(markerPath)
	if err != nil {
		return // no pending change — the normal case
	}
	var m marker
	_ = json.Unmarshal(mk, &m)
	log.Printf("[wifi] uncommitted change to %q found at startup — restoring previous network", m.NewSSID)

	// Ours first, then where pre-upgrade firmware wrote it — see
	// legacyBackupPath. On FireOS the two are the same path.
	from := backupPath()
	backup, err := os.ReadFile(from)
	if err != nil {
		if legacy, lerr := os.ReadFile(legacyBackupPath()); lerr == nil {
			from, backup, err = legacyBackupPath(), legacy, nil
			log.Printf("[wifi] restoring from the pre-upgrade backup at %s", from)
		}
	}
	if err != nil {
		// Marker without backup: the change already reverted its conf but
		// couldn't remove the marker, or the backup was lost. Nothing to
		// restore from — clear the marker and carry on with whatever conf
		// is in place.
		log.Printf("[wifi] no backup to restore (%v) — clearing marker", err)
		_ = os.Remove(markerPath)
		return
	}
	if err := reloadConf(string(backup)); err != nil {
		log.Printf("[wifi] startup restore failed: %v — leaving marker for next start", err)
		return
	}
	_ = os.Remove(markerPath)
	_ = os.Remove(from)
	setResult(Result{OK: false, SSID: m.NewSSID, Error: "device restarted before the change was confirmed — previous network restored"})
}
