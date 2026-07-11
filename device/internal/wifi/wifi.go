// Package wifi implements safe WiFi network changes with automatic
// rollback, plus scan/status queries for the dashboard Connectivity tab.
//
// The mechanics mirror the provisioning wizard's runConfigWifi
// (controller/static/dashboard.jsx), which was hard-won on real hardware:
//
//   - The ONLY safe reload path is `svc wifi disable` + `svc wifi enable`.
//     The framework-managed wpa_supplicant instance auto-associates and
//     gets a DHCP lease on its own. Never use raw `start wpa_supplicant`,
//     kill -9, manual wpa_cli reconnect, or manual dhcpcd — a second bare
//     supplicant instance fights the framework one over wlan0 and the
//     interface dies (INTERFACE_DISABLED, never recovers).
//   - The config is a FULL replacement of wpa_supplicant.conf with a
//     single network block — no ambiguity about which AP it joins.
//   - wpa_cli needs BOTH -p /data/misc/wifi/sockets (non-default socket
//     dir) and -i wlan0.
//
// Unlike the wizard (ADB shell), this package runs inside the root Go
// binary, so file writes use plain os.WriteFile — none of the mksh
// redirect quirks apply. Ownership must still be restored to wifi:wifi
// (AID_WIFI=1010) mode 0660 or the framework can't read the config.
//
// Safety model (the connection to the controller dies mid-change, so the
// device owns the whole sequence):
//
//  1. Back up the current conf and drop a pending marker file.
//  2. Write the new conf, bounce wifi via svc.
//  3. Gates: associate ≤20s → IPv4 on wlan0 ≤20s → control WebSocket
//     re-registered ≤90s. Any failure → restore the backup, bounce again,
//     and report the failure once the connection returns.
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
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	confPath   = "/data/misc/wifi/wpa_supplicant.conf"
	backupPath = "/data/misc/wifi/wpa_supplicant.conf.echomuse-bak"
	markerPath = "/data/local/tmp/echomuse_wifi_pending"

	wpaSockDir = "/data/misc/wifi/sockets"
	iface      = "wlan0"

	// AID_WIFI — fixed uid/gid on Android; the framework reads the conf
	// as this user.
	aidWifi = 1010

	associateTimeout = 20 * time.Second
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

// ─── Queries ──────────────────────────────────────────────────────────────────

func wpaCli(args ...string) (string, error) {
	full := append([]string{"-p", wpaSockDir, "-i", iface}, args...)
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

// composeConf builds the full-replacement wpa_supplicant.conf — the same
// template the provisioning wizard writes. An empty psk produces an open
// (key_mgmt=NONE) network block.
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
		"ctrl_interface=" + wpaSockDir,
		"driver_param=use_p2p_group_interface=1",
		"update_config=1",
		"device_name=" + getprop("ro.product.name", "echomuse"),
		"manufacturer=" + getprop("ro.product.manufacturer", "Amazon"),
		"model_name=" + getprop("ro.product.model", "AEOBC"),
		"model_number=" + getprop("ro.product.model", "AEOBC"),
		"serial_number=" + getprop("ro.serialno", getprop("ro.boot.serialno", "unknown")),
		"device_type=1-0050F204-9",
		"os_version=01020300",
		"config_methods=physical_display virtual_push_button",
		"p2p_no_group_iface=1",
		"external_sim=1",
		"wowlan_triggers=disconnect",
	}
	lines = append(lines, network...)
	return strings.Join(lines, "\n") + "\n"
}

func writeConf(content string) error {
	// Traverse bit on the dir — 666 here made every file inside
	// unopenable (provisioning finding).
	_ = os.Chmod("/data/misc/wifi", 0o770)
	if err := os.WriteFile(confPath, []byte(content), 0o660); err != nil {
		return fmt.Errorf("write %s: %w", confPath, err)
	}
	if err := os.Chown(confPath, aidWifi, aidWifi); err != nil {
		return fmt.Errorf("chown %s: %w", confPath, err)
	}
	return os.Chmod(confPath, 0o660)
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

func bounceWifi() error {
	log.Println("[wifi] svc wifi disable")
	if err := svcWifi("disable"); err != nil {
		return err
	}
	// Verify the disable took effect. A no-op bounce leaves wpa_supplicant
	// running against its old in-memory config, and every downstream gate
	// then passes vacuously against the old network — a false success that
	// commits the new conf without ever trying it.
	if !waitFor("disassociation after disable", 10*time.Second, func() bool { return !associated() }) {
		_ = svcWifi("enable")
		return fmt.Errorf("wifi did not go down after 'svc wifi disable'")
	}
	log.Println("[wifi] svc wifi enable")
	if err := svcWifi("enable"); err != nil {
		return err
	}
	time.Sleep(3 * time.Second)
	return nil
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

func setResult(r Result) {
	mu.Lock()
	pending = &r
	mu.Unlock()
}

// TakeResult returns and clears the unreported change outcome, if any.
// Called from the control client's OnConnected path so the result rides
// the first connection able to carry it.
func TakeResult() *Result {
	mu.Lock()
	defer mu.Unlock()
	r := pending
	pending = nil
	return r
}

// Commit finalises a successful change: the provisional state (marker +
// backup) is deleted, so a future crash/restart keeps the new network.
func Commit() {
	_ = os.Remove(markerPath)
	_ = os.Remove(backupPath)
	log.Println("[wifi] change committed — backup and pending marker removed")
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

	old, err := os.ReadFile(confPath)
	if err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot read current config: %v", err)})
		return
	}
	if err := os.WriteFile(backupPath, old, 0o600); err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot write backup: %v", err)})
		return
	}
	mk, _ := json.Marshal(marker{NewSSID: ssid, StartedAt: time.Now().Unix()})
	if err := os.WriteFile(markerPath, mk, 0o600); err != nil {
		setResult(Result{OK: false, SSID: ssid, Error: fmt.Sprintf("cannot write pending marker: %v", err)})
		return
	}

	if err := writeConf(composeConf(ssid, psk)); err != nil {
		_ = os.Remove(markerPath)
		setResult(Result{OK: false, SSID: ssid, Error: err.Error()})
		return
	}

	revert := func(reason string) {
		log.Printf("[wifi] change to %q failed (%s) — reverting", ssid, reason)
		if err := writeConf(string(old)); err != nil {
			// Conf unwritable is beyond self-healing; leave the marker so
			// RecoverIfPending retries the restore on next start.
			log.Printf("[wifi] REVERT WRITE FAILED: %v — marker left for recovery on restart", err)
		} else {
			_ = os.Remove(markerPath)
			_ = os.Remove(backupPath)
		}
		if err := bounceWifi(); err != nil {
			log.Printf("[wifi] revert bounce failed: %v", err)
		}
		waitFor("re-association after revert", associateTimeout, associated)
		setResult(Result{OK: false, SSID: ssid, Error: reason})
	}

	if err := bounceWifi(); err != nil {
		revert(err.Error())
		return
	}

	if !waitFor("association", associateTimeout, func() bool { return associatedTo(ssid) }) {
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

	backup, err := os.ReadFile(backupPath)
	if err != nil {
		// Marker without backup: the change already reverted its conf but
		// couldn't remove the marker, or the backup was lost. Nothing to
		// restore from — clear the marker and carry on with whatever conf
		// is in place.
		log.Printf("[wifi] no backup to restore (%v) — clearing marker", err)
		_ = os.Remove(markerPath)
		return
	}
	if err := writeConf(string(backup)); err != nil {
		log.Printf("[wifi] startup restore failed: %v — leaving marker for next start", err)
		return
	}
	_ = os.Remove(markerPath)
	_ = os.Remove(backupPath)
	if err := bounceWifi(); err != nil {
		log.Printf("[wifi] startup restore bounce failed: %v", err)
	}
	setResult(Result{OK: false, SSID: m.NewSSID, Error: "device restarted before the change was confirmed — previous network restored"})
}
