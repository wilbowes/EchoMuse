package main

import (
	"bufio"
	"context"
	"encoding/json"
	"sync/atomic"
	"log"
	"math"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/aec"
	internalbuttons "github.com/wilbowes/EchoMuse/internal/bindings/buttons"
	"github.com/wilbowes/EchoMuse/internal/bluetooth"
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/server"
	"github.com/wilbowes/EchoMuse/internal/wifi"
	pkgbuttons "github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

func main() {
	log.SetOutput(os.Stdout)
	log.Printf("EchoMuse %s starting", client.Version)

	deviceID := client.GetSerialNo()
	log.Printf("Device ID: %s", deviceID)

	// A WiFi change that never got committed (crash/power cycle mid-switch)
	// is rolled back before anything tries to use the network — same
	// self-healing philosophy as the A/B binary slots.
	wifi.RecoverIfPending()

	// Amazon's WiFi Simple Setup daemon (BLE+WiFi provisioning of
	// neighbouring Amazon devices) is useless on a repurposed device and
	// was caught busy-looping at ~50% CPU / 40% sys on one unit (Office,
	// 2026-07-13 — likely retrying the Bluetooth transport the BLE proxy
	// takes over). Same stock-service takeover as `stop mixer` /
	// `stop acebutton` / `stop ledcontroller` in the hardware bindings.
	// Idempotent: a no-op on boots where init never starts it (Lounge).
	exec.Command("stop", "smarthomewifid").Run()

	buttonController, err := internalbuttons.NewButtonController()
	if err != nil {
		log.Fatalf("Failed to initialize Button controller: %v", err)
	}

	microphone, err := mic.NewMicrophone()
	if err != nil {
		log.Fatalf("Failed to initialize Microphone: %v", err)
	}

	// AEC canceller — far end fed by the speaker's echo tap, near end run
	// by the data client on the mono mic stream. Starts disabled; armed by
	// applyAecConfig from env defaults below and on every config push.
	canceller := aec.New()

	// The level tap drives the energy-reactive LED ring ("meter" pattern).
	// The Server doesn't exist yet when the speaker starts its pump loop,
	// so the tap goes through an atomic pointer armed just below.
	var srvPtr atomic.Pointer[server.Server]
	pcmSpeaker, err := speaker.NewPcmSpeaker(canceller.WriteFar, func(rms float64) {
		if srv := srvPtr.Load(); srv != nil {
			srv.SetAudioLevel(rms)
		}
	})
	if err != nil {
		log.Fatalf("Failed to initialize PCM Speaker: %v", err)
	}

	s := server.NewServer(buttonController, microphone, pcmSpeaker)
	srvPtr.Store(s)

	buttonController.SetVolumeCallback(func(direction string) {
		if direction == "up" {
			s.VolumeStepUp()
		} else {
			s.VolumeStepDown()
		}
	})
	buttonController.SetMuteCallback(func() {
		s.MuteToggle()
	})

	ctx := context.Background()

	dataClient := client.NewDataClient(deviceID, microphone, pcmSpeaker, canceller)
	applyAecConfig(canceller) // arm from env defaults before any config push

	// Direction callback — update LED ring to show estimated source angle
	dataClient.OnDirectionChanged(func(angle float64) {
		s.SetDirectionLEDs(angle)
	})
	controlClient := client.NewControlClient(
		deviceID,
		func(leds []led.Led, listening *bool) {
			// A raw frame from the controller supersedes any running
			// device-local animation — stop it so its next tick can't
			// paint over this frame.
			s.StopAnim()
			s.SetLEDs(leds, listening)
		},
		func(lockMic bool) {
			if s.IsMuted() {
				// Mute is device-sovereign — the physical button cannot be
				// overridden remotely. Refuse the controller's mic_start.
				log.Println("[cmd] mic_start from controller rejected — device is muted")
				return
			}
			dataClient.StartMic(lockMic)
		},
		func() { dataClient.StopMic() },
	)

	// Device-rendered ring animations (led_anim) — the animation engine
	// runs on the device's own ticker, immune to controller/WiFi jitter.
	controlClient.OnLEDAnim(func(raw json.RawMessage) {
		var spec server.AnimSpec
		if err := json.Unmarshal(raw, &spec); err != nil {
			log.Printf("[cmd] bad led_anim spec: %v", err)
			return
		}
		s.StartAnim(spec)
	})

	// BLE proxy scanner — passive scan over /dev/stpbt, batches forwarded
	// to the controller on the control plane. Armed from env defaults here
	// and toggled live on config push (bleProxyEnabled, applyBleConfig).
	bleScanner := bluetooth.NewScanner(func(batch []bluetooth.Advert) {
		controlClient.SendBleAdverts(batch)
	})
	applyBleConfig(bleScanner)

	// Button events — forward to controller via control plane
	_, err = buttonController.SubscribeToButton(func(event pkgbuttons.ButtonClickEvent) {
		log.Printf("Button event: clickType=%d down=%v", event.ClickType, event.Down)
		if event.ClickType == pkgbuttons.DotClick && s.IsMuted() {
			log.Println("Dot button blocked — mic is muted")
			return
		}
		controlClient.SendButton(event)
	})
	if err != nil {
		log.Fatalf("Button subscription failed: %v", err)
	}

	// Disconnected — orange pulse
	var pulseCancel context.CancelFunc
	controlClient.OnDisconnected(func() {
		// Stop any device-local animation: the controller that owned it is
		// gone, and the pulse below would otherwise fight its ticker.
		s.StopAnim()
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		go pulseOrange(pulseCtx, s)
	})

	// Pending approval — slow white pulse
	controlClient.OnPending(func() {
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		go pulseWhite(pulseCtx, s)
	})

	// Connected — stop pulse, report current mute state, restore ring or hand
	// back to direction arc depending on mute state.
	controlClient.OnConnected(func() {
		if pulseCancel != nil {
			pulseCancel()
			pulseCancel = nil
		}
		// Always report mute and volume state on (re)connect — the controller
		// may have restarted and lost its record of our state.
		muted := s.IsMuted()
		controlClient.SendMuteState(muted)
		controlClient.SendVolumeState(s.VolumeLevel())
		s.StopAnim() // fresh controller session owns the ring from here
		if muted {
			// Orange pulse overwrote the red ring — restore it.
			s.RestoreMuteRing()
		} else {
			s.SetLEDs(allLEDs(0, 0, 0), nil)
			s.LEDModeDirection()
		}
		// Send an immediate stats snapshot so the dashboard populates on
		// (re)connect rather than waiting up to 30s for the first tick.
		go func() {
			st := collectStats()
			st.Ble = bleScanner.Stats()
			controlClient.SendStats(st)
		}()
		// Deliver any unacknowledged WiFi change outcome (including the
		// "restarted before commit" result RecoverIfPending leaves
		// behind). Not cleared here — the controller's wifi_commit ack
		// does that (wifi.Commit), so a result lost in transit re-sends.
		if r := wifi.PendingResult(); r != nil {
			controlClient.SendWifiResult(r.OK, r.SSID, r.Error)
		}
	})

	// Config applied — apply hardware changes via tinymix, AEC params to
	// the canceller. AEC/BLE read the merged post-Apply snapshot rather than
	// the (partial) message so unmentioned fields keep their values.
	controlClient.OnConfigApplied(func(msg config.ConfigMessage) {
		applyHardwareConfig(msg)
		applyAecConfig(canceller)
		applyBleConfig(bleScanner)
	})

	// Speaker flush — barge-in: cut buffered TTS the moment the controller
	// hears the wake word during playback.
	controlClient.OnSpeakerFlush(func() {
		pcmSpeaker.Flush()
	})

	// Per-stream playback stats — underrun/period counts reported upstream
	// once per completed TTS stream, persisted against the voice turn.
	pcmSpeaker.OnStreamStats(func(periods, underruns uint64) {
		controlClient.SendPlaybackStats(periods, underruns)
	})

	// WiFi change — the executor owns the whole switch/rollback sequence
	// (internal/wifi); the reconnect gate polls IsConnected. The outcome
	// is sent as wifi_result with at-least-once delivery: retried on a
	// ticker (and by the OnConnected drain above) until the controller's
	// wifi_commit ack clears it. IsConnected can report true against a
	// half-open TCP connection the interface bounce killed, so a single
	// send is not enough — the very first hardware success vanished that
	// way while the WS looked connected the whole time.
	controlClient.OnWifiChange(func(ssid, psk string) {
		go func() {
			wifi.Change(ssid, psk, controlClient.IsConnected)
			for i := 0; i < 30; i++ { // ~5 min, then give up (dashboard TTL is 4)
				r := wifi.PendingResult()
				if r == nil {
					return
				}
				if controlClient.IsConnected() {
					controlClient.SendWifiResult(r.OK, r.SSID, r.Error)
				}
				time.Sleep(10 * time.Second)
			}
		}()
	})
	controlClient.OnWifiCommit(wifi.Commit)
	controlClient.OnWifiScan(func() {
		go func() {
			nets, err := wifi.Scan()
			if err != nil {
				controlClient.SendWifiScanResult(nil, err.Error())
				return
			}
			controlClient.SendWifiScanResult(nets, "")
		}()
	})

	// Beam lock/unlock — controller locks the beamformer onto the speaker's
	// perimeter mic at wake detection (mid-stream, no restart) and releases
	// it at turn end. Requests are consumed by the mic streaming goroutine.
	controlClient.OnBeamLock(func(lock bool) {
		if lock {
			dataClient.RequestBeamLock()
		} else {
			dataClient.RequestBeamUnlock()
		}
	})

	// Mute state change — notify controller so dashboard can reflect it,
	// and stop/restart the mic stream device-side so mute is authoritative
	// regardless of controller state (C5 fix, 2026-07-05 review). Previously
	// only the *controller-initiated* mic_start was refused while muted (see
	// the mic_start callback above) — an already-running stream (e.g. the
	// permanent OWW listening stream) kept running if mute was toggled
	// mid-stream, so audio kept leaving the device while the ring showed
	// red. Note this is a partial fix: it stops audio leaving the device
	// over the network, but does not address the still-open, hardware-
	// unverified half of C5 — whether tinymix ctls 105/106 (chip A only)
	// actually silence the physical ADC path for ch6 and the perimeter
	// mics on chips B–D. That requires an on-device `tinymix -D 0` full
	// dump to confirm the sibling mute controls before touching them (see
	// review C5 fix sequence) — deliberately not guessed at here.
	s.SetMuteChangeCallback(func(muted bool) {
		controlClient.SendMuteState(muted)
		if muted {
			dataClient.StopMic()
		} else {
			// Restore the permanent OWW listening stream on unmute — no
			// lock_mic, matching the normal idle state. If the controller
			// also sends its own mic_start around the same time, StartMic
			// is idempotent (ignores the call while already active).
			dataClient.StartMic(false)
		}
	})

	// Volume change — notify controller so HA entity and dashboard reflect it.
	// Fires on every Set() call: physical button press or future volume_set command.
	s.SetVolumeChangeCallback(func(level int) {
		controlClient.SendVolumeState(level)
	})

	// Volume set from controller (HA MediaPlayerCommandRequest forwarded down).
	// Calls Set() which applies tinymix, updates LEDs, and fires the change
	// callback above — so SendVolumeState fires automatically, closing the loop.
	controlClient.OnVolumeSet(func(level int) {
		s.SetVolume(level)
	})

	log.Println("Ready")
	time.Sleep(2 * time.Second)

	go func() {
		if err := controlClient.Run(ctx, dataClient); err != nil && err != context.Canceled {
			log.Printf("Control client stopped: %v", err)
		}
	}()

	// Periodic stats reporter — every 30s. SendStats silently drops when
	// the device is not connected, so this goroutine runs unconditionally.
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			st := collectStats()
			st.Ble = bleScanner.Stats()
			controlClient.SendStats(st)
		}
	}()

	// Graceful shutdown on SIGTERM/SIGINT — both the OTA restart
	// (`kill $PPID` from the deploy shell) and start_server.sh's trap send
	// SIGTERM, so this runs on every normal stop. The speaker Close mutes
	// and disables the amp before the PCM stream tears down: without it,
	// every stop/restart/OTA clicked (amp cut mid-stream) and the amp was
	// left driving an idle DAC while the server was down (audible hiss
	// between OTA slots). Nothing else needs orderly teardown — mic/LED/
	// WS state all reset cleanly on the next start.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	sig := <-sigCh
	log.Printf("Received %v — shutting down (muting output, amp off)", sig)
	bleScanner.SetEnabled(false) // scan off + /dev/stpbt closed so the chip idles
	pcmSpeaker.Close()
	os.Exit(0)
}

// ─── Hardware stats collection ────────────────────────────────────────────────

func collectStats() client.DeviceStats {
	cpuPct := cpuPercent()
	memUsed, memTotal := memStats()
	stoUsed, stoTotal := storageStats()
	rssi := wifiRSSI()
	return client.DeviceStats{
		CPUPct:         cpuPct,
		MemUsedMb:      memUsed,
		MemTotalMb:     memTotal,
		StorageUsedMb:  stoUsed,
		StorageTotalMb: stoTotal,
		WifiRssi:       rssi,
		WifiSsid:       wifi.CurrentSSID(),
	}
}

// cpuPercent samples /proc/stat twice over 500ms and returns utilisation %.
func cpuPercent() float64 {
	type snap struct{ total, idle uint64 }

	read := func() (snap, bool) {
		f, err := os.Open("/proc/stat")
		if err != nil {
			return snap{}, false
		}
		defer f.Close()
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			line := sc.Text()
			if !strings.HasPrefix(line, "cpu ") {
				continue
			}
			fields := strings.Fields(line)[1:] // skip "cpu"
			var vals [8]uint64
			for i := 0; i < len(fields) && i < 8; i++ {
				vals[i], _ = strconv.ParseUint(fields[i], 10, 64)
			}
			// user nice system idle iowait irq softirq steal
			idle := vals[3] + vals[4] // idle + iowait
			total := vals[0] + vals[1] + vals[2] + vals[3] +
				vals[4] + vals[5] + vals[6] + vals[7]
			return snap{total, idle}, true
		}
		return snap{}, false
	}

	s1, ok1 := read()
	time.Sleep(500 * time.Millisecond)
	s2, ok2 := read()
	if !ok1 || !ok2 {
		return 0
	}
	dTotal := float64(s2.total - s1.total)
	if dTotal <= 0 {
		return 0
	}
	dIdle := float64(s2.idle - s1.idle)
	pct := (1 - dIdle/dTotal) * 100
	// Round to one decimal place
	return math.Round(pct*10) / 10
}

// memStats reads /proc/meminfo and returns (used MB, total MB).
func memStats() (usedMb, totalMb int) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0
	}
	defer f.Close()

	var totalKb, availKb uint64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 2 {
			continue
		}
		val, _ := strconv.ParseUint(fields[1], 10, 64)
		switch fields[0] {
		case "MemTotal:":
			totalKb = val
		case "MemAvailable:":
			availKb = val
		}
	}
	if totalKb == 0 {
		return 0, 0
	}
	usedKb := totalKb - availKb
	return int(usedKb / 1024), int(totalKb / 1024)
}

// storageStats returns (used MB, total MB) for /data via statfs.
func storageStats() (usedMb, totalMb int) {
	var st syscall.Statfs_t
	if err := syscall.Statfs("/data", &st); err != nil {
		return 0, 0
	}
	bsize := uint64(st.Bsize)
	total := st.Blocks * bsize
	free := st.Bfree * bsize
	used := total - free
	const mb = 1024 * 1024
	return int(used / mb), int(total / mb)
}

// wifiRSSI reads /proc/net/wireless and returns the signal level in dBm,
// or nil if the interface is not available.
//
// Some kernels encode the level field as a positive offset (0–255) rather
// than signed dBm; values > 0 are adjusted by subtracting 256 to recover
// the actual dBm reading (e.g. 206 → -50 dBm).
func wifiRSSI() *int {
	f, err := os.Open("/proc/net/wireless")
	if err != nil {
		return nil
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	lineNum := 0
	for sc.Scan() {
		lineNum++
		if lineNum <= 2 {
			continue // skip two header lines
		}
		fields := strings.Fields(sc.Text())
		// fields: [iface status link level noise ...]
		if len(fields) < 4 {
			continue
		}
		// level is fields[3], may have a trailing "."
		rssiStr := strings.TrimRight(fields[3], ".")
		rssi, err := strconv.Atoi(rssiStr)
		if err != nil {
			continue
		}
		// Correct offset encoding used by some kernels
		if rssi > 0 {
			rssi -= 256
		}
		// Sanity check — valid RSSI is roughly -30 to -100 dBm
		if rssi < -120 || rssi > 0 {
			continue
		}
		return &rssi
	}
	return nil
}

// ─── Hardware config ──────────────────────────────────────────────────────────

// applyHardwareConfig runs tinymix commands for fields that map to hardware.
// Called whenever the controller pushes a config message.
func applyHardwareConfig(msg config.ConfigMessage) {
	if msg.AdcDigitalGain > 0 {
		tinymix("89", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("107", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("125", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("143", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
	}
	if msg.AdcMicpga > 0 {
		tinymix("92", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("110", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("128", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("146", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
	}
	if msg.StartupVolume > 0 {
		tinymix("61", strconv.Itoa(msg.StartupVolume), strconv.Itoa(msg.StartupVolume))
	}
}

// applyAecConfig pushes the current effective AEC config into the canceller.
// SetParams no-ops when nothing changed, so calling it on every config push
// is free; when delay/tail change it rebuilds the echo state (adaptive
// filter state is meaningless across a timing change anyway).
func applyAecConfig(canceller *aec.Canceller) {
	snap := config.Get().Snapshot()
	enabled := snap.AecEnabled != nil && *snap.AecEnabled
	delayMs := 250
	if snap.AecDelayMs != nil {
		delayMs = *snap.AecDelayMs
	}
	canceller.SetParams(enabled, delayMs, snap.AecTailMs)
}

// applyBleConfig starts/stops the BLE proxy scanner from the current
// effective config. SetEnabled is idempotent, so calling it on every config
// push is free.
func applyBleConfig(scanner *bluetooth.Scanner) {
	snap := config.Get().Snapshot()
	scanner.SetEnabled(snap.BleProxyEnabled != nil && *snap.BleProxyEnabled)
}

func tinymix(ctl string, args ...string) {
	cmdArgs := append([]string{"-D", "0", ctl}, args...)
	out, err := exec.Command("tinymix", cmdArgs...).CombinedOutput()
	if err != nil {
		log.Printf("[tinymix] ctl %s failed: %v — %s", ctl, err, string(out))
	}
}

func allLEDs(r, g, b uint8) []led.Led {
	leds := make([]led.Led, 12)
	for i := range leds {
		leds[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	return leds
}

// ─── LED animations ───────────────────────────────────────────────────────────

// pulseOrange — sine-wave orange pulse while disconnected from server.
func pulseOrange(ctx context.Context, s *server.Server) {
	const (
		minBr    = 0.05
		maxBr    = 0.6
		periodMs = 2000
		stepMs   = 50
	)
	ticker := time.NewTicker(stepMs * time.Millisecond)
	defer ticker.Stop()
	step := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t := float64(step) / float64(periodMs/stepMs)
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			s.SetLEDs(allLEDs(uint8(255*br), uint8(40*br), 0), nil)
			step = (step + 1) % (periodMs / stepMs)
		}
	}
}

// pulseWhite — slow white pulse while pending controller approval.
// Slower and dimmer than orange to be visually distinct.
func pulseWhite(ctx context.Context, s *server.Server) {
	const (
		minBr    = 0.02
		maxBr    = 0.35
		periodMs = 3000 // slower than orange
		stepMs   = 50
	)
	ticker := time.NewTicker(stepMs * time.Millisecond)
	defer ticker.Stop()
	step := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t := float64(step) / float64(periodMs/stepMs)
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			v := uint8(255 * br)
			s.SetLEDs(allLEDs(v, v, v), nil)
			step = (step + 1) % (periodMs / stepMs)
		}
	}
}
