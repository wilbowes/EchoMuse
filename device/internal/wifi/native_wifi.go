package wifi

import (
	"bytes"
	"context"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type nativeProfile struct {
	ssid []byte
	psk  string
	auth string
}

// nativePlan checks, before anything disconnects, that the OEM CLI can express
// the requested network and that the current one can be rejoined from the
// saved conf if the change fails.
func nativePlan(ssid []byte, psk, conf string, current []byte) (target, previous nativeProfile, err error) {
	switch {
	case current == nil:
		return target, previous, fmt.Errorf("current network unknown; a failed change could not be rolled back")
	case bytes.IndexByte(ssid, 0) >= 0:
		return target, previous, fmt.Errorf("native WiFi CLI cannot represent a NUL SSID")
	}
	target = nativeProfile{ssid: ssid, psk: psk, auth: "wpa2"}
	if psk == "" {
		target.auth = "open"
	} else if isHexPSK(psk) {
		target.psk = strings.ToLower(psk) // as pskLine writes it, for the persistence check
	}
	if previous, err = nativeProfileFromConf(conf, current); err != nil {
		err = fmt.Errorf("current network could not be rejoined after a failed change: %w", err)
	}
	return target, previous, err
}

// nativeRestore rejoins a profile the conf already held and waits until it is
// the associated network.
func nativeRestore(previous nativeProfile) error {
	if err := nativeConnect(previous); err != nil {
		return err
	}
	if !waitFor("previous network re-association", associateTimeout, func() bool { return associatedTo(previous.ssid) }) {
		return fmt.Errorf("previous network did not re-associate")
	}
	return nil
}

// The original manager owns the credential cache as well as the supplicant.
func nativeConnect(profile nativeProfile) error {
	if !waitFor("native WiFi ready", 10*time.Second, func() bool {
		out, err := wpaCli("ping")
		return err == nil && strings.TrimSpace(out) == "PONG"
	}) {
		return fmt.Errorf("native WiFi control is not ready")
	}
	stateOutput, err := nativeWifiCommand("get_net_state")
	if err != nil {
		return err
	}
	if nativeNetworkState(stateOutput) != "DISCONNECTED" || associated() {
		if _, err := nativeWifiCommand("disconnect"); err != nil {
			return err
		}
	}
	previousState := ""
	if !waitFor("native network disconnection", 45*time.Second, func() bool {
		output, err := nativeWifiCommand("get_net_state")
		state := nativeNetworkState(output)
		if state != previousState {
			log.Printf("[wifi] native network state: %s", state)
			previousState = state
		}
		return err == nil && state == "DISCONNECTED" && !associated()
	}) {
		return fmt.Errorf("native WiFi did not finish disconnecting")
	}
	args := []string{"ssid=\"" + string(profile.ssid) + "\"", "auth=" + profile.auth, "prio=1"}
	if profile.psk != "" {
		args = append(args, "psk=\""+profile.psk+"\"")
	}
	// The OEM add path creates missing profiles and updates existing ones.
	if _, err := nativeWifiCommand(append([]string{"add_network"}, args...)...); err != nil {
		return err
	}
	if _, err := nativeWifiCommand("connect", string(profile.ssid), profile.auth); err != nil {
		return err
	}
	// Save after selection, so the selected profile survives a cold boot.
	if _, err := nativeWifiCommand("save_config"); err != nil {
		return err
	}
	// The OEM CLI acknowledges asynchronous requests even if SAVE_CONFIG fails.
	// Confirm the exact credentials reached disk before reporting acceptance.
	if !waitFor("native profile persistence", 10*time.Second, func() bool {
		path, _ := confPaths()
		data, err := os.ReadFile(path)
		if err != nil {
			return false
		}
		saved, err := nativeProfileFromConf(string(data), profile.ssid)
		return err == nil && bytes.Equal(saved.ssid, profile.ssid) && saved.psk == profile.psk && saved.auth == profile.auth
	}) {
		return fmt.Errorf("native WiFi profile was not persisted")
	}
	return nil
}

func nativeWifiCommand(args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/system/bin/ace", append([]string{"mw", "wifi_cli"}, args...)...)
	command.WaitDelay = 2 * time.Second
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("native WiFi %s: %w", args[0], err)
	}
	if !nativeCommandSucceeded(string(output)) {
		// The OEM CLI prints passwords. Never log its output or arguments.
		return "", fmt.Errorf("native WiFi %s was not accepted", args[0])
	}
	return string(output), nil
}

func nativeCommandSucceeded(output string) bool {
	confirmed := false
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		lower := strings.ToLower(line)
		for _, prefix := range []string{"error", "failed ", "could not ", "need ", "usage:"} {
			if strings.HasPrefix(lower, prefix) {
				return false
			}
		}
		if strings.HasPrefix(line, "Ret:") {
			fields := strings.Fields(line)
			if len(fields) < 2 {
				return false
			}
			status, err := strconv.Atoi(fields[1])
			confirmed = err == nil && status == 0
		}
	}
	return confirmed
}

// Ponytail: accept personal/open syntax emitted by EchoMuse and the OEM.
// Enterprise/custom syntax needs a native profile export, not guesswork.
func nativeProfileFromConf(content string, preferred []byte) (nativeProfile, error) {
	var profiles []map[string]string
	var fields map[string]string
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		switch line {
		case "network={":
			if fields != nil {
				return nativeProfile{}, fmt.Errorf("nested WiFi profile")
			}
			fields = make(map[string]string)
		case "}":
			if fields != nil {
				profiles = append(profiles, fields)
				fields = nil
			}
		default:
			if fields != nil {
				key, value, ok := strings.Cut(line, "=")
				if ok {
					key, value = strings.TrimSpace(key), strings.TrimSpace(value)
					if _, exists := fields[key]; exists {
						return nativeProfile{}, fmt.Errorf("duplicate WiFi profile field")
					}
					fields[key] = value
				}
			}
		}
	}
	if fields != nil {
		return nativeProfile{}, fmt.Errorf("unfinished WiFi profile")
	}
	var selected *nativeProfile
	for _, fields := range profiles {
		value := fields["ssid"]
		var ssid []byte
		if len(value) >= 2 && value[0] == '"' && value[len(value)-1] == '"' {
			ssid = []byte(value[1 : len(value)-1])
		} else {
			var err error
			ssid, err = hex.DecodeString(value)
			if err != nil {
				return nativeProfile{}, fmt.Errorf("invalid WiFi profile SSID")
			}
		}
		if len(preferred) != 0 && !bytes.Equal(ssid, preferred) {
			continue
		}
		if selected != nil {
			return nativeProfile{}, fmt.Errorf("ambiguous WiFi profile")
		}
		profile := nativeProfile{ssid: ssid, auth: "open"}
		switch fields["key_mgmt"] {
		case "NONE":
		case "WPA-PSK", "WPA-PSK WPA-PSK-SHA256", "WPA-PSK-SHA256 WPA-PSK":
			profile.auth = "wpa2"
			profile.psk = fields["psk"]
			if len(profile.psk) >= 2 && profile.psk[0] == '"' && profile.psk[len(profile.psk)-1] == '"' {
				profile.psk = profile.psk[1 : len(profile.psk)-1]
			}
			if profile.psk == "" {
				return nativeProfile{}, fmt.Errorf("missing WiFi profile PSK")
			}
		default:
			return nativeProfile{}, fmt.Errorf("unsupported native WiFi profile security")
		}
		if err := validate(ssid, profile.psk); err != nil {
			return nativeProfile{}, err
		}
		if bytes.IndexByte(ssid, 0) >= 0 {
			return nativeProfile{}, fmt.Errorf("native WiFi CLI cannot represent a NUL SSID")
		}
		selected = &profile
	}
	if selected == nil {
		return nativeProfile{}, fmt.Errorf("previous WiFi profile not found")
	}
	return *selected, nil
}

// The manager's L3 state outlives supplicant disassociation during DHCP teardown.
func nativeNetworkState(output string) string {
	state, count := "UNKNOWN", 0
	for _, line := range strings.Split(output, "\n") {
		if value, ok := strings.CutPrefix(strings.TrimSpace(line), "networkState: "); ok {
			state, count = value, count+1
		}
	}
	if count != 1 {
		return "UNKNOWN"
	}
	switch state {
	case "CONNECTING", "CONNECTED", "DISCONNECTING", "DISCONNECTED", "L2CONNECTED":
		return state
	}
	return "UNKNOWN"
}
