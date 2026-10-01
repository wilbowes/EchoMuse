package main

import (
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/firewall"
	"github.com/wilbowes/EchoMuse/internal/sendspin"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The Sendspin player's lifecycle (internal/sendspin, #89). Off by default,
// switched by sendspinEnabled on the config push: it listens on a port and
// advertises over mDNS, so it runs only where someone asked for it. emOS
// drops inbound connections, so the port is opened in the filter while the
// player runs and closed when it stops.

// sendspinStore is beside the TLS credentials and state.json, on /data, so it
// survives OTA slot flips. It holds the device's Sendspin identity: losing it
// unpairs the device from every server.
const sendspinStore = "/data/local/etc/echomuse/sendspin.json"

var ss struct {
	mu   sync.Mutex
	c    *sendspin.Client
	name string
}

func sendspinPlayer() *sendspin.Client {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	return ss.c
}

// deviceVolume is the Echo's one volume: the level HA and the buttons move,
// 0..127 with unity at 127. Server volume is mapped onto it with the same
// proportion as HA's percentage (controller em_volume), so the same volume
// reads the same number in Music Assistant and in Home Assistant.
type deviceVolume interface {
	VolumeLevel() int
	SetVolume(level int)
}

const volumeMax = 127

func levelToPct(level int) int { return sendspin.LevelToPercent(level, volumeMax) }
func pctToLevel(pct int) int   { return sendspin.PercentToLevel(pct, volumeMax) }

// sendspinVolumeChanged tells the player the device's volume moved, by any
// route. Wired into the volume change callback.
func sendspinVolumeChanged(level int) {
	if c := sendspinPlayer(); c != nil {
		c.SetVolume(levelToPct(level))
	}
}

// sendspinLinkDown stops the player when the controller link goes: EchoMuse
// is one system, and an Echo whose ring says it is disconnected should not
// be playing music (Wil, 2026-09-30). "restart" asks Music Assistant to
// redial, and the next config push after the link returns starts it again.
func sendspinLinkDown(spk *speaker.PcmSpeaker) {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	if ss.c == nil {
		return
	}
	spk.SetMusicSource(nil)
	ss.c.Stop("restart")
	ss.c = nil
	firewall.Close(sendspin.DefaultPort)
	log.Printf("[sendspin] player stopped: controller link down")
}

// applySendspinConfig starts, stops or updates the player to match config.
// A rename restarts it: the name is in the mDNS record and in client/hello,
// and neither is re-sent on a live connection. It runs only on a config
// push, so only with the controller link up.
func applySendspinConfig(spk *speaker.PcmSpeaker, cc *client.ControlClient, vol deviceVolume, deviceID string) {
	snap := config.Get().Snapshot()
	on := snap.SendspinEnabled != nil && *snap.SendspinEnabled
	unpaired := snap.SendspinUnpaired != nil && *snap.SendspinUnpaired
	name := snap.SendspinName
	if name == "" {
		name = "Echo " + tail(deviceID, 4)
	}

	ss.mu.Lock()
	defer ss.mu.Unlock()
	if ss.c != nil && (!on || name != ss.name) {
		spk.SetMusicSource(nil)
		reason := "user_request"
		if on {
			reason = "restart"
		}
		ss.c.Stop(reason)
		ss.c = nil
		log.Printf("[sendspin] player stopped (%s)", reason)
	}
	if !on {
		// Also clears a rule left by a firmware that died with the player on.
		firewall.Close(sendspin.DefaultPort)
		cc.SendSendspinStatus(nil)
		return
	}
	if ss.c == nil {
		c, err := sendspin.New(sendspin.Config{
			StorePath: sendspinStore,
			Name:      name,
			Instance:  "echomuse-" + deviceID,
			Product:   "EchoMuse " + board.IDOf(board.Detect("")),
			Version:   client.Version,
			Unpaired:  unpaired,
		})
		if err == nil {
			// Before Start: a server can connect and command the volume at once.
			c.OnVolume(func(pct int) { vol.SetVolume(pctToLevel(pct)) })
			c.SetVolume(levelToPct(vol.VolumeLevel()))
			// Open before Start advertises: Music Assistant dials when the
			// record appears and may not retry a timeout. Logged, not
			// fatal: FireOS has no filter to open.
			if ferr := firewall.Open(sendspin.DefaultPort); ferr != nil {
				log.Printf("[sendspin] could not open port %d: %v", sendspin.DefaultPort, ferr)
			}
			err = c.Start()
		}
		if err != nil {
			firewall.Close(sendspin.DefaultPort)
			log.Printf("[sendspin] player not started: %v", err)
			cc.SendSendspinStatus(map[string]string{"state": "error", "error": err.Error()})
			return
		}
		c.OnChange(debounce(300*time.Millisecond, func() { cc.SendSendspinStatus(c.Status()) }))
		spk.SetMusicSource(c)
		ss.c, ss.name = c, name
		cc.SendSendspinStatus(c.Status())
	}
	ss.c.SetUnpaired(unpaired)
}

// sendspinPoll runs on the BLE yield ticker (100ms). Home Assistant's music
// wins the music plane, so while the 0x04 plane has anything in it the
// player reports itself taken, and the server moves it out of its group.
func sendspinPoll(spk *speaker.PcmSpeaker) {
	if c := sendspinPlayer(); c != nil {
		c.SetExternal(spk.MusicPlaneBusy())
		if c.Active() && time.Since(sendspinLoggedAt) >= time.Minute {
			sendspinLoggedAt = time.Now()
			logSendspinStats(c.Status())
		}
	}
}

// sendspinLoggedAt paces the playing-stats log line. Only sendspinPoll's
// ticker goroutine touches it.
var sendspinLoggedAt time.Time

// logSendspinStats puts the sync numbers in the device log once a minute
// while playing. The counts run from the stream's start, so a rate is the
// difference between two lines.
func logSendspinStats(st sendspin.Status) {
	p := st.Player
	log.Printf("[sendspin] playing: synced=%v syncErr=%dus buffered=%dms snaps=%d corrections=%d underruns=%d lateDrops=%d lastErr=%dus",
		st.Synced, st.SyncErrUs, st.BufferedMs, p.Snaps, p.Corrections, p.Underruns, p.LateDrops, p.LastErrorUs)
}

// sendspinMusic is the player's share of what the BLE duty cycle weighs:
// whether synced music is arriving, and how much is buffered.
func sendspinMusic() (active bool, lead time.Duration) {
	if c := sendspinPlayer(); c != nil && c.Active() {
		return true, c.Buffered()
	}
	return false, 0
}

func sendspinToken() map[string]any {
	c := sendspinPlayer()
	if c == nil {
		return nil
	}
	return map[string]any{"token": c.PairingToken(), "clientId": c.ClientID()}
}

func sendspinStatus() any {
	if c := sendspinPlayer(); c != nil {
		return c.Status()
	}
	return nil
}

// debounce collapses a burst of calls into one, delay after the first.
func debounce(delay time.Duration, f func()) func() {
	var mu sync.Mutex
	pending := false
	return func() {
		mu.Lock()
		defer mu.Unlock()
		if pending {
			return
		}
		pending = true
		time.AfterFunc(delay, func() {
			mu.Lock()
			pending = false
			mu.Unlock()
			f()
		})
	}
}

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}
