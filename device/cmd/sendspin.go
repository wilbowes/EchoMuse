package main

import (
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/sendspin"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The Sendspin player's lifecycle (internal/sendspin, #89). Off by default,
// switched by sendspinEnabled on the config push: it listens on a port and
// advertises over mDNS, so it runs only where someone asked for it.

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

// applySendspinConfig starts, stops or updates the player to match config.
// A rename restarts it: the name is in the mDNS record and in client/hello,
// and neither is re-sent on a live connection.
func applySendspinConfig(spk *speaker.PcmSpeaker, cc *client.ControlClient, deviceID string) {
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
			err = c.Start()
		}
		if err != nil {
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
	}
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
