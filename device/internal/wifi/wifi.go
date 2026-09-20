// Package wifi implements safe WiFi network changes with automatic
// rollback, plus scan/status queries for the dashboard Connectivity tab.
//
// One firmware, both bases — see "Which base OS" below. What differs is which
// file the config lives in, where the control socket is, and how the supplicant
// is made to re-read it. FireOS has a long tail, so its path is not
// transitional.
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
//   - wpa_cli needs BOTH -p <socket dir> and -i wlan0. That directory is
//     declared inside the conf and differs by base — see sockDir.
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
// redirect quirks apply. FireOS needs ownership restored to wifi:wifi
// (AID_WIFI=1010) 0660 or the framework can't read it. emOS depends on which
// supplicant the image carries, since they run as different users — see
// confMode, and note that 0600 there strands a FireOS 5 device on its next
// boot.
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
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
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

	// Fallback, and the default on FireOS. Mirrors WPA_CTRL_DEFAULT in
	// emos/init/init.c; the real value comes from sockDir.
	defaultSockDir = "/data/misc/wifi/sockets"
	iface          = "wlan0"

	// AID_WIFI — fixed uid/gid on Android; the framework reads the conf
	// as this user. Amazon's supplicant runs as it under emOS too, which is
	// what confMode is about.
	aidWifi = 1010

	// emOS's own supplicant, present only in images that carry the payload's
	// WiFi tools. Its presence is what init selects on (first_exec in
	// emos/init/init.c), so it is also what decides who must be able to read
	// the conf we write.
	emosSupp = "/sbin/wpa_supplicant"

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

// Network is one scan result row. SSID is for display; SSIDHex is the exact
// bytes, which is what a change request sends back (see ssid.go).
type Network struct {
	SSID    string `json:"ssid"`
	SSIDHex string `json:"ssid_hex"`
	Signal  int    `json:"signal"`
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
// One firmware, resolved at runtime — see internal/platform. FireOS has a long
// tail, so neither path is transitional. Indirected through vars so both bases
// are exercisable from a host that is neither.
var (
	baseOS       = platform.Base
	androidConfP = androidConf
	emosConfP    = emosConf
	emosSuppP    = emosSupp
	// Indirected so the ownership rules are testable off-target: a test host
	// is not root and cannot hand a file to AID_WIFI, and the uid it asks for
	// is the whole assertion.
	chownFile = os.Chown
)

func onEmOS() bool { return baseOS() == platform.EmOS }

// confPaths says where the supplicant's config is read from and written to.
//
// READ mirrors wpa_conf() in emos/init/init.c — ours if it exists, Android's
// otherwise — since reading a different file than the running supplicant was
// started with describes a config nothing is using.
//
// WRITE on emOS is always ours: anything under /data/misc/wifi belongs to
// whichever OS booted last, and FireOS 6 rewrites it with fields our supplicant
// survives only by patch. The two differ on exactly one device — a migrated
// install never told its network here, which backs up Android's file and writes
// ours. Android's copy is deliberately left as the fallback.
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

// backupPath sits beside the config it backs up: one in Android's directory is
// one FireOS may clobber, read back on the boot that already lost the current.
func backupPath() string {
	_, write := confPaths()
	return write + ".echomuse-bak"
}

// legacyBackupPath is where pre-upgrade firmware wrote it. The marker outlives
// an OTA, so without this a change in flight across one keeps the UNCONFIRMED
// network while its backup sits unread.
func legacyBackupPath() string { return androidConfP + ".echomuse-bak" }

// sockDir reads the control socket directory out of the conf. Not a constant:
// em-wifi declares /data/emos/sockets and a wizard-provisioned device Android's,
// and a wpa_cli pointed at the wrong one fails silently — presenting as a device
// that cannot scan and reports no SSID.
//
// Re-read per call, since em-wifi can move it while this process runs. Parsing
// matches wpa_ctrl_dir in emos/init/init.c, last declaration winning; pinned by
// test on both sides.
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
		// Amazon's uses hostap's "DIR=/path GROUP=wifi"; ours a bare path.
		v = strings.TrimPrefix(v, "DIR=")
		if i := strings.IndexAny(v, " \t"); i >= 0 {
			v = v[:i]
		}
		// Relative means hostap's abstract namespace, which `wpa_cli -p`
		// cannot address.
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

// CurrentSSID returns the associated SSID for display, or "" when not
// associated.
func CurrentSSID() string {
	return SSIDText(currentSSIDBytes())
}

// currentSSIDBytes is the associated SSID's exact bytes, or nil. The status
// line is printf_encode'd like scan_results, and only the trailing CR a line
// can carry is stripped: spaces at either end are part of an SSID.
func currentSSIDBytes() []byte {
	out, _ := wpaCli("status")
	if !strings.Contains(out, "wpa_state=COMPLETED") {
		return nil
	}
	for _, line := range strings.Split(out, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimRight(line, "\r"), "ssid="); ok {
			return UnescapeSSID(v)
		}
	}
	return nil
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

	return parseScan(out), nil
}

// parseScan turns scan_results into one Network per SSID, strongest AP first.
// Keyed by the SSID's BYTES, so two names that only differ in bytes a display
// cannot show stay two networks.
func parseScan(out string) []Network {
	best := map[string]int{}
	for _, line := range strings.Split(out, "\n") {
		// bssid \t frequency \t signal \t flags \t ssid. Tabs inside an SSID
		// arrive escaped, so the fifth field is the whole name; only a
		// transport CR is stripped, never spaces.
		parts := strings.Split(strings.TrimRight(line, "\r"), "\t")
		if len(parts) < 5 {
			continue
		}
		ssid := UnescapeSSID(parts[4])
		// Hidden networks advertise an empty or all-zero SSID: unjoinable by
		// name, so dropped.
		if hiddenOrEmpty(ssid) {
			continue
		}
		sig, err := strconv.Atoi(strings.TrimSpace(parts[2]))
		if err != nil {
			continue
		}
		key := string(ssid)
		if cur, ok := best[key]; !ok || sig > cur {
			best[key] = sig
		}
	}
	nets := make([]Network, 0, len(best))
	for key, sig := range best {
		b := []byte(key)
		nets = append(nets, Network{SSID: SSIDText(b), SSIDHex: hex.EncodeToString(b), Signal: sig})
	}
	sort.Slice(nets, func(i, j int) bool { return nets[i].Signal > nets[j].Signal })
	return nets
}

// ─── Change with rollback ─────────────────────────────────────────────────────

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
// The control socket is carried through from the conf (sockDir), never written
// as a constant: that would move the socket out from under init's reassociate
// nudge and em-wifi, and nothing in that failure names this function.
//
// FireOS keeps the wizard's WPS/P2P globals. emOS drops them — our hostap is
// built without CONFIG_WPS or CONFIG_P2P, so they are fields it cannot use. It
// tolerates them by patch, but that patch is for confs FireOS 6 left behind, not
// a licence to write dead lines.
func composeConf(ssid []byte, psk string) string {
	network := []string{
		"network={",
		ssidLine(ssid),
	}
	if psk == "" {
		network = append(network, "\tkey_mgmt=NONE")
	} else {
		network = append(network,
			pskLine(psk),
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

// confMode says how tightly the conf we write on emOS may be locked down:
// root-only when emOS's own supplicant will read it, wifi-readable when
// Amazon's will.
//
// The two supplicants run as different users. Ours is started as root; Amazon's
// drops to AID_WIFI, so a 0600 root conf is one it cannot open — it exits at
// startup, init leaves a zombie, and the device comes up with NO network and no
// way in but a cable. Measured on EFF 2026-09-20, after a WiFi change wrote the
// file and the next boot tried to read it.
//
// Selected on the same test init uses (first_exec in emos/init/init.c), rather
// than on the base OS: a FireOS 5 image carries no /sbin/wpa_supplicant, since
// the payload's WiFi tools ship only in the ARM32 image, and that is exactly
// the fleet this stranded. Drop the binary in and both halves switch together.
func confMode() (mode os.FileMode, wifiOwned bool) {
	if _, err := os.Stat(emosSuppP); err == nil {
		return 0o600, false
	}
	return 0o660, true
}

func writeConf(content string) error {
	_, path := confPaths()
	if onEmOS() {
		// No wider than the supplicant that reads it — it holds the PSK.
		// MkdirAll because /data/emos may not exist on a device never
		// configured here.
		mode, wifiOwned := confMode()
		dir, dirMode := filepath.Dir(path), os.FileMode(0o700)
		if wifiOwned {
			// Group-writable, like Android's /data/misc/wifi: the supplicant
			// rewrites the conf in place on SAVE_CONFIG, which the
			// provisioning wizard's emOS WiFi step sends after joining
			// (dashboard.jsx, runStep 8). Traverse alone would fail that step
			// on any device that already has a conf here.
			dirMode = 0o770
		}
		if err := os.MkdirAll(dir, dirMode); err != nil {
			return fmt.Errorf("mkdir %s: %w", dir, err)
		}
		if err := os.WriteFile(path, []byte(content), mode); err != nil {
			return fmt.Errorf("write %s: %w", path, err)
		}
		if wifiOwned {
			// An existing directory keeps its old mode through MkdirAll, and
			// every device that has taken a WiFi change already has one at
			// 0700.
			if err := os.Chmod(dir, dirMode); err != nil {
				return fmt.Errorf("chmod %s: %w", dir, err)
			}
			if err := chownFile(dir, 0, aidWifi); err != nil {
				return fmt.Errorf("chown %s: %w", dir, err)
			}
			if err := chownFile(path, aidWifi, aidWifi); err != nil {
				return fmt.Errorf("chown %s: %w", path, err)
			}
		}
		return os.Chmod(path, mode)
	}
	// Traverse bit on the dir — 666 here made every file inside
	// unopenable (provisioning finding).
	_ = os.Chmod(filepath.Dir(path), 0o770)
	if err := os.WriteFile(path, []byte(content), 0o660); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	if err := chownFile(path, aidWifi, aidWifi); err != nil {
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
// against the new conf; that supervision loop is a 5s ticker (net_main in
// emos/init/init.c).
//
// /proc is walked rather than shelling out to killall, which exists only because
// init symlinks busybox into /sbin — a missing symlink would present as a WiFi
// change that silently does nothing. "wpa_supplicant" is 14 chars against the
// kernel's 15-char comm field, so there is no truncation to match around.
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
		// Not an error: init starts one as soon as a conf exists.
		log.Println("[wifi] no running supplicant to retire — init will start one")
		return nil
	}
	log.Printf("[wifi] retired %d supplicant process(es) — init restarts against the new conf", found)
	return nil
}

// reloadConf swaps in a new config and gets the supplicant onto it.
//
// On FireOS the conf must be written with WiFi DOWN: on disable,
// WifiStateMachine saves its in-memory network list back over it, so a conf
// written while WiFi is up is clobbered and the device silently rejoins the old
// network (hardware, 2026-07-11).
//
// On emOS that dance is wrong. `svc` needs a framework and property service that
// are absent, so the disable fails and takes the change with it — and nothing
// else writes the file, so there is no save to lose a write to. Write, then
// retire the supplicant for init's supervisor to replace, as em-wifi does.
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
// supplicant never actually restarted. Compared as bytes.
func associatedTo(ssid []byte) bool {
	cur := currentSSIDBytes()
	return cur != nil && string(cur) == string(ssid)
}

// waitForAssociation polls for association to ssid, logging the raw
// supplicant state every 5s so a timeout in the field says what the
// framework was doing (SCANNING vs 4WAY_HANDSHAKE vs INTERFACE_DISABLED).
func waitForAssociation(ssid []byte, timeout time.Duration) bool {
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
			log.Printf("[wifi] waiting for association to %q — wpa_state=%s", SSIDText(ssid), state)
			lastDiag = time.Now()
		}
		time.Sleep(time.Second)
	}
	log.Printf("[wifi] timed out waiting for association to %q (%s)", SSIDText(ssid), timeout)
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
func Change(ssidBytes []byte, psk string, connected func() bool) {
	// Everything below reports and logs the display name; the bytes are what
	// the conf and the association gate use.
	ssid := SSIDText(ssidBytes)
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

	if err := validate(ssidBytes, psk); err != nil {
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

	if err := reloadConf(composeConf(ssidBytes, psk)); err != nil {
		revert(err.Error())
		return
	}

	if !waitForAssociation(ssidBytes, associateTimeout) {
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

	// Ours, then where pre-upgrade firmware wrote it. Same path on FireOS.
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
