package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"runtime/pprof"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/internal/bindings/als"
	"github.com/wilbowes/EchoMuse/internal/bindings/jack"
	internalbuttons "github.com/wilbowes/EchoMuse/internal/bindings/buttons"
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/bluetooth"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/server"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
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

	// Keep a second CPU core online. procfs, so this has to be re-applied on
	// every start — see applyCoreFloor for why the mic pipeline's 160ms
	// deadline makes it worth doing.
	applyCoreFloor()

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
	applyAecConfig(canceller, dataClient) // arm from env defaults before any config push

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
		// Muted presses are FORWARDED, with the mute state attached, and the
		// controller decides what the gesture is allowed to do. Dropping them
		// here was right while the dot button meant only "start a voice turn";
		// it became wrong when a hold started firing an HA event, because a
		// hold bound to something unrelated to speech then stopped working
		// whenever the mic was muted.
		//
		// This does not weaken mute. The mic_start rejection above is what
		// makes mute sovereign — the controller cannot open the mic while
		// muted however it reads this event, and the ADC is muted in hardware
		// regardless.
		event.Muted = s.IsMuted()
		// A press outranks the volume arc: adjusting volume and immediately
		// pressing the button used to leave the arc holding the ring for the
		// rest of its 2s window, with nothing showing that the device had
		// started listening. Release, not press, so it lines up with the
		// event the controller actually starts a turn on.
		if event.ClickType == pkgbuttons.DotClick && !event.Down {
			s.CancelVolumeDisplay()
		}
		controlClient.SendButton(event)
	})
	if err != nil {
		log.Fatalf("Button subscription failed: %v", err)
	}

	// Disconnected — orange pulse
	//
	// pulseKind is what the ring is CURRENTLY showing, and restarting a pulse
	// that is already running is the bug it exists to prevent. OnDisconnected
	// fires once per reconnect-loop iteration, not once per disconnection —
	// so every retry used to cancel the goroutine mid-cycle and start a new
	// one from phase zero, which is mid-brightness and rising. The ring ran
	// roughly two smooth cycles and then hard-cut back to the middle, at an
	// interval that is not a multiple of the pulse period, so the jump landed
	// somewhere different each time. Reported as "like a poorly repeating
	// gif" and it is exactly that: a loop being restarted, not a loop.
	var (
		pulseCancel context.CancelFunc
		pulseKind   string
	)
	// Watch the ambient light sensor for step changes (a lamp switching on)
	// and report them immediately; the steady-state value rides the ~30s
	// stats tick. No-ops on a device without the sensor.
	//
	// Started here rather than earlier because it captures controlClient,
	// which does not exist until above — and the amd64 build cannot catch
	// that, since this package is excluded from it by build constraints
	// (mic/speaker are ARM-only). Only compile.sh compiles this file.
	go als.Watch(ctx, func(lux int) {
		controlClient.SendAmbientLight(lux)
	})

	// Headphone jack — accdet mutes the internal speaker amp on insert and
	// never restores it on removal, so without this the speaker stays dead
	// until the next reboot (issue #80, reproduced and fixed on hardware
	// 2026-08-09). Insert needs nothing from us: accdet already handles the
	// mute, and the output routing itself is done by the jack's own switch
	// contacts, not by any mixer control.
	go jack.Watch(ctx, func(inserted bool) {
		if !inserted {
			pcmSpeaker.EnableSpeakerAmp()
		}
	})

	controlClient.OnDisconnected(func() {
		// Stop any device-local animation: the controller that owned it is
		// gone, and the pulse below would otherwise fight its ticker. Safe to
		// repeat — StopAnim only bumps the animator generation, and the pulse
		// paints through SetLEDs rather than the animator, so it is not what
		// this cancels.
		s.StopAnim()
		if pulseKind == "orange" {
			return // already pulsing; restarting is what breaks the cycle
		}
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		pulseKind = "orange"
		go pulseOrange(pulseCtx, s)
	})

	// Pending approval — slow white pulse
	controlClient.OnPending(func() {
		if pulseKind == "white" {
			return
		}
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		pulseKind = "white"
		go pulseWhite(pulseCtx, s)
	})

	// Connected — stop pulse, report current mute state, restore ring or hand
	// back to direction arc depending on mute state.
	controlClient.OnConnected(func() {
		if pulseCancel != nil {
			pulseCancel()
			pulseCancel = nil
		}
		pulseKind = ""
		// Always report mute state on (re)connect — the controller may have
		// restarted and lost its record of our state. Volume is only
		// reported once the device holds an authoritative level (seeded
		// from config or set locally): the controller persists every
		// volume_state into startupVolume, so reporting the boot-default
		// level here is what used to clobber the saved volume on reboot.
		// On a fresh boot the config push seeds the volume, and Set()'s
		// change callback sends the report instead.
		muted := s.IsMuted()
		controlClient.SendMuteState(muted)
		if s.VolumeSeeded() {
			controlClient.SendVolumeState(s.VolumeLevel())
		}
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
			st.OwwShadow = shadowStats(dataClient)
			st.AecRef = canceller.RefSource()
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
		// startupVolume is the controller's persisted record of this
		// device's volume (updated on every volume_state report) — restore
		// it through the Server, not a raw tinymix write: SeedVolume keeps
		// the recorded level in sync and only honours the first push per
		// run, so a reconnect's config can't stomp a live volume change.
		if msg.StartupVolume > 0 {
			s.SeedVolume(msg.StartupVolume)
		}
		applyAecConfig(canceller, dataClient)
		applyBleConfig(bleScanner)
		applyShadowConfig(dataClient, controlClient, pcmSpeaker, s)
	})

	// Speaker flush — barge-in: cut buffered TTS the moment the controller
	// hears the wake word during playback.
	controlClient.OnSpeakerFlush(func() {
		pcmSpeaker.Flush()
	})

	// Music flush — the user genuinely stopped or paused. A voice turn ducks
	// instead and must never send this.
	controlClient.OnMusicFlush(func() {
		pcmSpeaker.FlushMusic()
	})

	// Duck — music is attenuated under a voice turn and restored at the end.
	// The depth is read at duck time rather than latched, so a config change
	// takes effect on the next turn without a restart.
	controlClient.OnDuck(func(on bool) {
		if on {
			pcmSpeaker.SetDuck(config.Get().DuckDb)
		} else {
			pcmSpeaker.SetDuck(0)
		}
	})

	// Per-stream playback stats — underrun/period counts reported upstream
	// once per completed TTS stream, persisted against the voice turn.
	pcmSpeaker.OnStreamStats(func(st speaker.StreamStats) {
		controlClient.SendPlaybackStats(st.Periods, st.Underruns, st)
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
		// The hardware echo reference is tapped upstream of the DAC volume
		// control, so it holds full scale whatever the user sets. Tell the
		// canceller the scalar it cannot see, or every volume change is an
		// echo-path gain step the adaptive filter can only find by
		// re-converging — measured on 2026-08-29 as cancellation dropping to
		// -1.7dB after a change and taking 3-4s to recover, repeatedly.
		canceller.SetPlaybackLevel(level)
	})
	// Seed it from where the device actually is, right now. The callback
	// above only fires on a CHANGE, and the two things that would produce
	// one at startup both have holes: SeedVolume is skipped entirely when
	// the controller pushes startupVolume=0 (a device it has no record
	// for), and Set() is a no-op-shaped path nothing guarantees runs. Miss
	// it and refScale stays 0 — read as unity — while the codec sits at
	// whatever level the previous run left behind, which is round one's
	// 33dB-hot reference reappearing on a device nobody touched.
	canceller.SetPlaybackLevel(s.VolumeLevel())

	// Volume set from controller (HA MediaPlayerCommandRequest forwarded down).
	// Calls Set() which applies tinymix, updates LEDs, and fires the change
	// callback above — so SendVolumeState fires automatically, closing the loop.
	controlClient.OnVolumeSet(func(level int) {
		s.SetVolume(level)
	})

	// Heap-profile dump on SIGUSR1 — the ~1MB/h leak hunt (2026-07-17).
	// The device accepts no inbound connections, so instead of an HTTP pprof
	// endpoint the profile is written to /tmp and pulled over the shell
	// proxy: `kill -USR1 $(pidof server)` then base64 the file out. A GC
	// runs first so the profile reflects live objects, not garbage awaiting
	// collection. Fixed filenames (2 slots, alternating) so repeated dumps
	// for before/after diffing can't fill /tmp.
	usrCh := make(chan os.Signal, 1)
	signal.Notify(usrCh, syscall.SIGUSR1)
	go func() {
		slot := 0
		for range usrCh {
			runtime.GC()
			path := fmt.Sprintf("/tmp/heap-%d.pprof", slot)
			f, err := os.Create(path)
			if err != nil {
				log.Printf("[pprof] create %s: %v", path, err)
				continue
			}
			if err := pprof.WriteHeapProfile(f); err != nil {
				log.Printf("[pprof] write %s: %v", path, err)
			} else {
				log.Printf("[pprof] heap profile written to %s", path)
			}
			f.Close()
			slot = 1 - slot
		}
	}()

	log.Println("Ready")
	time.Sleep(2 * time.Second)

	go func() {
		if err := controlClient.Run(ctx, dataClient); err != nil && err != context.Canceled {
			log.Printf("Control client stopped: %v", err)
		}
	}()

	// Periodic stats reporter — every 30s. SendStats silently drops when
	// the device is not connected, so this goroutine runs unconditionally.
	// Every 10th tick (~5min) a [mem] line goes to the local log: the
	// process RSS is growing ~1.2MB/h (measured 2026-07-16) and the Go
	// runtime's own accounting is what distinguishes heap growth (leak —
	// HeapAlloc climbs), fragmentation/retained-but-free memory (HeapAlloc
	// flat, HeapSys/RSS climb), and goroutine leaks (goroutines climb).
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		tick := 0
		for range ticker.C {
			st := collectStats()
			st.Ble = bleScanner.Stats()
			st.OwwShadow = shadowStats(dataClient)
			st.AecRef = canceller.RefSource()
			controlClient.SendStats(st)
			if tick%10 == 0 {
				var ms runtime.MemStats
				runtime.ReadMemStats(&ms)
				memLine := fmt.Sprintf("[mem] goroutines=%d heap_alloc=%dKB heap_sys=%dKB heap_idle=%dKB released=%dKB stack=%dKB rss=%dKB num_gc=%d pause_total=%dms",
					runtime.NumGoroutine(),
					ms.HeapAlloc/1024, ms.HeapSys/1024, ms.HeapIdle/1024,
					ms.HeapReleased/1024, ms.StackSys/1024, selfRSSKb(),
					ms.NumGC, ms.PauseTotalNs/1e6)
				log.Print(memLine)
				// Forward to the controller's device_logs too — the local
				// /tmp/server.log is RAM-backed and dies with every reboot,
				// and the 2026-07 leak hunt needed these lines pulled over
				// the shell proxy by hand. One message per ~5min; SendLog
				// silently drops while disconnected, same as SendStats.
				controlClient.SendLog("info", memLine)
			}
			tick++
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

// shadowStats drains the on-device wake word counters for this reporting
// window, or nil when shadow mode is off — nil marshals the field away, so the
// controller can tell "off" from "on and saw nothing", which are very different
// answers to "why were there no detections".
func shadowStats(dc *client.DataClient) interface{} {
	sc := dc.ShadowScorer()
	if sc == nil {
		return nil
	}
	st := sc.Drain()
	return map[string]interface{}{
		"frames":    st.Frames,
		"drops":     st.Drops,
		"notReady":  st.NotReady,
		"crossings": st.Crossings,
		"maxScore":  st.MaxScore,
		"threshold": st.Threshold,
		"errors":    st.Errors,
		"lastErr":   st.LastErr,
		"ready":     sc.Ready(),
		// Maxima that explain a drop: the slowest single inference (consumer
		// stalling) against the longest gap between frames arriving (producer
		// bursting). Cheap enough to send every window — two integers on a
		// message that already exists.
		"maxInferMs": st.MaxInferMs,
		"maxGapMs":   st.MaxGapMs,
	}
}

func collectStats() client.DeviceStats {
	cpuPct := cpuPercent()
	memUsed, memTotal := memStats()
	stoUsed, stoTotal := storageStats()
	rssi := wifiRSSI()
	tx, rx, txErr, txDrop, rxCrc := netDeltas()
	speed, freq, bssid := linkInfo()
	cpuC, maxC, coreLimit := thermals()
	return client.DeviceStats{
		AmbientLux:       als.Lux(),
		CPUTempC:         cpuC,
		MaxTempC:         maxC,
		CoresOnline:      coresOnline(),
		CoresTotal:       coresTotal(),
		ThermalCoreLimit: coreLimit,
		CPUPct:           cpuPct,
		MemUsedMb:        memUsed,
		MemTotalMb:       memTotal,
		StorageUsedMb:    stoUsed,
		StorageTotalMb:   stoTotal,
		WifiRssi:         rssi,
		WifiSsid:         wifi.CurrentSSID(),
		LinkSpeedMbps:    speed,
		WifiFreqMhz:      freq,
		WifiBssid:        bssid,
		TxBytes:          tx,
		RxBytes:          rx,
		TxErrors:         txErr,
		TxDropped:        txDrop,
		RxCrcErrors:      rxCrc,
	}
}

// ─── Network telemetry ────────────────────────────────────────────────────────

// netCounters holds the previous sysfs read so stats can be reported as
// per-interval deltas. Only collectStats touches it (single stats goroutine).
var netCounters struct {
	tx, rx, txErr, txDrop, rxCrc uint64
	primed                       bool
}

// netDeltas returns tx/rx bytes and error counts accumulated since the
// previous call, read from /sys/class/net/wlan0/statistics/. Plain file
// reads — no process spawn — so this is cheap enough for every stats tick.
// The first call primes the baseline and reports zeros.
func netDeltas() (tx, rx, txErr, txDrop, rxCrc uint64) {
	read := func(name string) uint64 {
		b, err := os.ReadFile("/sys/class/net/wlan0/statistics/" + name)
		if err != nil {
			return 0
		}
		v, _ := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
		return v
	}
	ctx, crx := read("tx_bytes"), read("rx_bytes")
	cErr, cDrop, cCrc := read("tx_errors"), read("tx_dropped"), read("rx_crc_errors")

	// delta guards against counter resets (interface bounce) by clamping
	// a negative difference to 0 rather than reporting a huge number.
	delta := func(cur, prev uint64) uint64 {
		if cur < prev {
			return 0
		}
		return cur - prev
	}
	if netCounters.primed {
		tx = delta(ctx, netCounters.tx)
		rx = delta(crx, netCounters.rx)
		txErr = delta(cErr, netCounters.txErr)
		txDrop = delta(cDrop, netCounters.txDrop)
		rxCrc = delta(cCrc, netCounters.rxCrc)
	}
	netCounters.tx, netCounters.rx = ctx, crx
	netCounters.txErr, netCounters.txDrop, netCounters.rxCrc = cErr, cDrop, cCrc
	netCounters.primed = true
	return
}

// linkInfoCache holds the last wpa_cli result and when it was taken.
var linkInfoCache struct {
	speed, freq int
	bssid       string
	at          time.Time
}

// linkInfoInterval — how often the wpa_cli subprocess is actually run.
// Unlike everything else in collectStats this costs a process spawn, and
// PHY rate / band / AP change on the scale of minutes, not seconds. Cached
// values are reused between refreshes so every stats message still carries
// the fields.
const linkInfoInterval = 2 * time.Minute

// linkInfo returns negotiated PHY rate (Mbps), frequency (MHz) and BSSID.
//
// Requires the -p control-socket path: plain `wpa_cli -i wlan0` answers
// UNKNOWN COMMAND on FireOS because the default socket dir doesn't exist.
// Returns zero values if wpa_supplicant isn't reachable — the fields are
// omitempty, so the controller sees them absent rather than wrong.
func linkInfo() (speed, freq int, bssid string) {
	if time.Since(linkInfoCache.at) < linkInfoInterval {
		return linkInfoCache.speed, linkInfoCache.freq, linkInfoCache.bssid
	}
	linkInfoCache.at = time.Now()

	out, err := exec.Command("wpa_cli", "-p", "/data/misc/wifi/sockets",
		"-i", "wlan0", "signal_poll").Output()
	if err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
			if !ok {
				continue
			}
			n, convErr := strconv.Atoi(v)
			if convErr != nil {
				continue
			}
			switch k {
			case "LINKSPEED":
				linkInfoCache.speed = n
			case "FREQUENCY":
				linkInfoCache.freq = n
			}
		}
	}
	if out, err := exec.Command("wpa_cli", "-p", "/data/misc/wifi/sockets",
		"-i", "wlan0", "status").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			if v, ok := strings.CutPrefix(strings.TrimSpace(line), "bssid="); ok {
				linkInfoCache.bssid = v
				break
			}
		}
	}
	return linkInfoCache.speed, linkInfoCache.freq, linkInfoCache.bssid
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

// selfRSSKb reads the process's resident set size from /proc/self/status —
// the OS's ground truth, against which the Go runtime numbers in the [mem]
// log line are compared. 0 if unreadable.
func selfRSSKb() int {
	f, err := os.Open("/proc/self/status")
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) >= 2 && fields[0] == "VmRSS:" {
			kb, _ := strconv.Atoi(fields[1])
			return kb
		}
	}
	return 0
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
}

// applyAecConfig pushes the current effective AEC config into the canceller.
// SetParams no-ops when nothing changed, so calling it on every config push
// is free; when delay/tail change it rebuilds the echo state (adaptive
// filter state is meaningless across a timing change anyway).
func applyAecConfig(canceller *aec.Canceller, dataClient *client.DataClient) {
	snap := config.Get().Snapshot()
	enabled := snap.AecEnabled != nil && *snap.AecEnabled
	delayMs := 250
	if snap.AecDelayMs != nil {
		delayMs = *snap.AecDelayMs
	}
	canceller.SetParams(enabled, delayMs, snap.AecTailMs)
	// The reference SOURCE lives on the data client, not the canceller: it
	// is the mic goroutine that extracts ch8 and decides per period whether
	// to hand it over, so that goroutine has to own the switch.
	dataClient.SetAecRefSource(snap.AecRefSource)
}

// applyBleConfig starts/stops the BLE proxy scanner from the current
// effective config. SetEnabled is idempotent, so calling it on every config
// push is free.
// shadowState remembers what the live scorer was built for, so a config push
// that changes neither the mode nor the model does not rebuild it. Rebuilding
// means reloading a 12MB runtime and starting a fresh ~1.28s not-ready window,
// and config pushes arrive on every reconnect — so "idempotent unless something
// changed" is the difference between a stable shadow run and one that is
// perpetually warming up.
var shadowState struct {
	mode    string
	model   string
	lastErr string
}

// applyShadowConfig starts, stops or re-points on-device wake word scoring from
// the current effective config.
//
// Failure to load is an ordinary condition, not an error state: the runtime and
// models are installed out of band, so "not installed" is what every device
// reports until someone puts them there. It is logged once per distinct reason
// rather than on every config push, and the device carries on with
// controller-side wake word exactly as before.
func applyShadowConfig(dc *client.DataClient, cc *client.ControlClient,
	spk *speaker.PcmSpeaker, srv *server.Server) {
	snap := config.Get().Snapshot()
	mode, model := snap.OwwOnDevice, snap.OwwModel
	threshold := float32(snap.OwwThreshold)

	if mode == config.OnDeviceOff {
		if dc.ShadowScorer() != nil {
			dc.SetShadowScorer(nil)
			log.Printf("[shadow] on-device wake word disabled")
		}
		shadowState.mode, shadowState.model, shadowState.lastErr = mode, model, ""
		return
	}

	// Already running for this model: thresholds and the mode change live.
	if sc := dc.ShadowScorer(); sc != nil && shadowState.model == model {
		sc.SetThreshold(threshold)
		if snap.BargeInEnabled != nil && *snap.BargeInEnabled && spk != nil {
			sc.SetBargeThreshold(float32(snap.BargeInThreshold), spk.IsStreaming)
		} else {
			sc.SetBargeThreshold(0, nil)
		}
		if shadowState.mode != mode {
			shadowState.mode = mode
			log.Printf("[shadow] mode now %q (%s)", mode, actsOnCrossings(mode))
		}
		return
	}

	// The mode is read at CROSSING time, not baked in here, so flipping
	// between "shadow" and "on" costs nothing: the scorer is identical in
	// both and rebuilding it would reload a 12MB runtime and open a fresh
	// ~1.28s not-ready window every time someone changed their mind.
	sc, err := shadow.Open(model, threshold, func(score, crossed float32, at time.Time) {
		onWakeCrossing(cc, srv, score, crossed, at)
	})
	if err != nil {
		if msg := err.Error(); msg != shadowState.lastErr {
			shadowState.lastErr = msg
			log.Printf("[shadow] not started: %v", err)
		}
		dc.SetShadowScorer(nil)
		shadowState.mode, shadowState.model = mode, model
		return
	}
	// Mirror the controller's barge-in behaviour: while the speaker is
	// streaming its wake bar drops to bargeInThreshold, and a device scoring
	// against the normal threshold would disagree on every barge-in.
	if snap.BargeInEnabled != nil && *snap.BargeInEnabled && spk != nil {
		sc.SetBargeThreshold(float32(snap.BargeInThreshold), spk.IsStreaming)
	}
	dc.SetShadowScorer(sc)
	shadowState.mode, shadowState.model, shadowState.lastErr = mode, model, ""
	log.Printf("[shadow] on-device wake word scoring (%s, threshold %.2f) — %s",
		sc.Info(), threshold, actsOnCrossings(mode))
}

// actsOnCrossings describes what a crossing will DO, for the log line. The
// distinction is the whole difference between the two live modes and is not
// otherwise visible on the device.
func actsOnCrossings(mode string) string {
	if mode == config.OnDeviceOn {
		return "triggering turns"
	}
	return "reporting only, not triggering"
}

// onWakeCrossing is what a threshold crossing does, decided fresh each time
// from the current config rather than at scorer-construction time.
//
// Mute is checked HERE, on the device, and that placement is deliberate. Mute
// is device-sovereign: the device already refuses every mic_start while muted
// and the ADC is muted in hardware, so a wake sent while muted could at worst
// start a turn that captures silence. But "at worst" still means the ring
// lights up and HA runs a pipeline because a muted device thought it heard
// something, and there is nothing on the device connecting the two for the
// person watching it happen. The controller-side check stays as well — this is
// the same belt-and-braces as the button path, not a replacement for it.
// crossed is the bar this score actually cleared — the lower barge-in one
// during playback. The controller records it against the turn, and recording
// the nominal threshold instead is what once made every barge-in look like a
// wake that had fired below its own bar.
func onWakeCrossing(cc *client.ControlClient, srv *server.Server,
	score, crossed float32, at time.Time) {
	ageMs := time.Since(at).Milliseconds()
	if config.Get().Snapshot().OwwOnDevice != config.OnDeviceOn {
		cc.SendOwwShadowCross(score, ageMs)
		return
	}
	if srv != nil && srv.IsMuted() {
		// Still reported, because a crossing while muted is real data about
		// the detector and shadow mode would have recorded it. It just does
		// not become a turn.
		cc.SendOwwShadowCross(score, ageMs)
		log.Printf("[shadow] wake %.3f suppressed — muted", score)
		return
	}
	// #263: light the listening ring NOW, from the one place that already
	// knows the wake happened. The crossing used to travel to the controller
	// and wait for leds_listening to come back — measured at +522ms before
	// the animation moved, on a link whose control-plane tail reaches 2s
	// (#139). The controller's own frame lands within an RTT and takes over
	// via StartAnim's generation counter; same pattern as the volume arc,
	// where the device draws immediately and the authoritative state follows.
	// No new arbitration: the LED priority system already handles a newer
	// frame superseding a local one. Only devices that have received a
	// listening spec (config push) can do this; everyone else keeps the old
	// behaviour exactly.
	if srv != nil {
		if raw := config.Get().Snapshot().ListeningAnim; len(raw) > 0 {
			var spec server.AnimSpec
			if err := json.Unmarshal(raw, &spec); err == nil && spec.Pattern != "" {
				srv.StartAnim(spec)
			}
		}
	}
	cc.SendOwwWake(score, crossed, ageMs)
}

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

// pulsePhase returns the current point in a cycle of `period` that began at
// `start`, as a fraction in [0,1).
//
// Elapsed time, not a step counter: a counter advances one step per tick
// whether or not the tick was on time, so a delayed tick stretches that cycle
// rather than skipping ahead within it — the ring slows down under load and
// never catches up. animator.go's runPulse already works this way.
func pulsePhase(start time.Time, period time.Duration) float64 {
	return math.Mod(float64(time.Since(start))/float64(period), 1.0)
}

// pulseOrange — sine-wave orange pulse while disconnected from server.
//
// Started ONCE per disconnection, not once per reconnect attempt — see
// pulseKind at the OnDisconnected call site. Cancelling and relaunching this
// resets the phase to mid-brightness, which is visible as a stutter.
func pulseOrange(ctx context.Context, s *server.Server) {
	const (
		minBr  = 0.05
		maxBr  = 0.6
		period = 2000 * time.Millisecond
		stepMs = 50
	)
	ticker := time.NewTicker(stepMs * time.Millisecond)
	defer ticker.Stop()
	start := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t := pulsePhase(start, period)
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			s.SetLEDs(allLEDs(uint8(255*br), uint8(40*br), 0), nil)
		}
	}
}

// pulseWhite — slow white pulse while pending controller approval.
// Slower and dimmer than orange to be visually distinct.
func pulseWhite(ctx context.Context, s *server.Server) {
	const (
		minBr  = 0.02
		maxBr  = 0.35
		period = 3000 * time.Millisecond // slower than orange
		stepMs = 50
	)
	ticker := time.NewTicker(stepMs * time.Millisecond)
	defer ticker.Stop()
	start := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t := pulsePhase(start, period)
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			v := uint8(255 * br)
			s.SetLEDs(allLEDs(v, v, v), nil)
		}
	}
}

// ─── Thermals and core hotplug ────────────────────────────────────────────────
//
// Two facts about this SoC make these worth reporting, and they are related.
//
// The MT8163 is a QUAD-core Cortex-A53, but MediaTek's hotplug strategy parks
// cores that are not needed: /sys/devices/system/cpu/online is usually just
// "0". A second core comes online only after utilisation holds above
// /proc/hps/up_threshold (80%) for up_times (2) samples. So a device sitting
// at 54% is not near a ceiling — it is comfortably inside one core's budget
// with three more parked.
//
// That directly undermines cpuPct, which is derived from the aggregate
// /proc/stat line and is therefore a share of ONLINE capacity: the same
// absolute work halves its reported percentage the moment a second core
// appears. Reporting coresOnline alongside it is what makes the number
// interpretable rather than merely available.
//
// Thermals matter for the opposite reason — to show there is nothing to worry
// about, or to show when there is. thermalCoreLimit is the sharpest indicator
// this SoC offers: it is how many cores the thermal governor will currently
// permit, so anything below 4 means throttling has begun, which shows up as
// capacity loss long before a temperature reading looks alarming.

// thermalZones maps a zone type ("mtktscpu") to its temp file. Resolved once —
// the names are stable for the life of the boot, and rescanning 11 sysfs
// directories every 30s to learn nothing would be silly.
var (
	thermalOnce   sync.Once
	thermalByType map[string]string
)

func resolveThermalZones() map[string]string {
	thermalOnce.Do(func() {
		thermalByType = map[string]string{}
		dirs, err := filepath.Glob("/sys/class/thermal/thermal_zone*")
		if err != nil {
			return
		}
		for _, d := range dirs {
			b, err := os.ReadFile(filepath.Join(d, "type"))
			if err != nil {
				continue
			}
			thermalByType[strings.TrimSpace(string(b))] = filepath.Join(d, "temp")
		}
		log.Printf("[thermal] %d zones: %s", len(thermalByType), strings.Join(zoneTypes(), " "))
	})
	return thermalByType
}

func zoneTypes() []string {
	out := make([]string, 0, len(thermalByType))
	for t := range thermalByType {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

// readMilliC reads a sysfs temperature (millidegrees C) as degrees.
func readMilliC(path string) (float64, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		return 0, false
	}
	// Sanity bound: a plausible reading is roughly -20..150C. Some MTK zones
	// report a sentinel (0, or a huge value) when their sensor is not wired,
	// and averaging that into a trend would quietly ruin it — the same reason
	// the RF counters are deliberately not surfaced.
	c := float64(n) / 1000.0
	if c < -20 || c > 150 {
		return 0, false
	}
	return c, true
}

// thermals returns the CPU zone temperature, the hottest zone of any kind, and
// how many cores the thermal governor currently permits.
//
// mtktscpu is the SoC/CPU zone. The hottest-of-all figure is reported too
// because the PMIC and board sensors can run warmer than the CPU, and a device
// in trouble will not necessarily show it on the zone you thought to watch.
func thermals() (cpuC *float64, maxC *float64, coreLimit int) {
	zones := resolveThermalZones()
	if c, ok := readMilliC(zones["mtktscpu"]); ok {
		cpuC = &c
	}
	var hottest float64
	var any bool
	for _, p := range zones {
		if c, ok := readMilliC(p); ok && (!any || c > hottest) {
			hottest, any = c, true
		}
	}
	if any {
		maxC = &hottest
	}
	// /proc/hps/num_limit_thermal — cores the thermal governor allows. Absent
	// on a kernel without MTK HPS, reported as 0 = unknown rather than 0 cores.
	if b, err := os.ReadFile("/proc/hps/num_limit_thermal"); err == nil {
		if n, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil {
			coreLimit = n
		}
	}
	return cpuC, maxC, coreLimit
}

// coresOnline counts online CPUs from /sys/devices/system/cpu/online, whose
// format is a range list ("0", "0-3", "0,2-3").
func coresOnline() int {
	b, err := os.ReadFile("/sys/devices/system/cpu/online")
	if err != nil {
		return 0
	}
	n := 0
	for _, part := range strings.Split(strings.TrimSpace(string(b)), ",") {
		if part == "" {
			continue
		}
		lo, hi, found := strings.Cut(part, "-")
		a, err1 := strconv.Atoi(strings.TrimSpace(lo))
		if !found {
			if err1 == nil {
				n++
			}
			continue
		}
		z, err2 := strconv.Atoi(strings.TrimSpace(hi))
		if err1 == nil && err2 == nil && z >= a {
			n += z - a + 1
		}
	}
	return n
}

// hpsCoreFloor is the minimum number of CPU cores kept online.
//
// The MT8163 has four Cortex-A53 cores and MediaTek's hotplug strategy parks
// all but one, bringing a second up only after utilisation holds above
// /proc/hps/up_threshold (80%) for up_times (2) samples. That is a sensible
// default for an idle appliance and a poor one for this workload: the mic
// pipeline has a hard 160ms deadline (the ALSA ring's whole depth) and now
// shares a core with wake word inference that runs in ~31ms bursts. Time-
// slicing those on one core works — measured, zero stalls — but it works with
// no margin for a coincidence, and it depends on hotplug reacting in time to a
// burst that has already started.
//
// A floor of 2 lets the two actually run in parallel, and leaves up_threshold
// to scale to 3 and 4 exactly as before. The cost is one A53 core out of idle,
// which on a mains-powered device sitting at 33C is not a real cost: measured
// +0.3C at the PMIC and no change at the CPU zone.
//
// Set via num_base_perf_serv, which is HPS's core-count FLOOR (the num_limit_*
// files are its ceilings, all 4 here). Deliberately NOT done by writing
// cpu1/online directly: HPS would re-park it within down_times samples, and
// fighting the governor is how you get a setting that appears to work and
// silently stops.
const hpsCoreFloor = 2

// applyCoreFloor raises the hotplug floor, best-effort.
//
// procfs, so it does not survive a reboot — which is why it lives here, in the
// binary, rather than in a provisioning script: it travels with the firmware
// and re-applies on every start. Absent on a kernel without MTK HPS, in which
// case there is nothing to do and nothing to warn about.
func applyCoreFloor() {
	const path = "/proc/hps/num_base_perf_serv"
	before, err := os.ReadFile(path)
	if err != nil {
		return // not an MTK HPS kernel
	}
	if strings.TrimSpace(string(before)) == strconv.Itoa(hpsCoreFloor) {
		return
	}
	if err := os.WriteFile(path, []byte(strconv.Itoa(hpsCoreFloor)), 0o644); err != nil {
		log.Printf("[cpu] could not raise core floor to %d: %v", hpsCoreFloor, err)
		return
	}
	log.Printf("[cpu] core floor %s -> %d (online=%d, hotplug still scales above up_threshold)",
		strings.TrimSpace(string(before)), hpsCoreFloor, coresOnline())
}

// coresTotal is how many cores the SoC has, online or parked. Reported so a
// "1 of 4 online" reads as a power state rather than a one-core device — which
// is how the MT8163's hotplug behaviour gets misread.
func coresTotal() int {
	b, err := os.ReadFile("/sys/devices/system/cpu/present")
	if err != nil {
		return 0
	}
	n := 0
	for _, part := range strings.Split(strings.TrimSpace(string(b)), ",") {
		lo, hi, found := strings.Cut(part, "-")
		a, err1 := strconv.Atoi(strings.TrimSpace(lo))
		if !found {
			if err1 == nil {
				n++
			}
			continue
		}
		z, err2 := strconv.Atoi(strings.TrimSpace(hi))
		if err1 == nil && err2 == nil && z >= a {
			n += z - a + 1
		}
	}
	return n
}
