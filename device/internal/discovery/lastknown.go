package discovery

import (
	"bytes"
	"encoding/json"
	"log"
	"os"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
)

// LastKnownControllerPath is the endpoint the last successful registration
// used. The device writes it, never a human, so a restart begins with the
// address that worked rather than with mDNS alone.
const LastKnownControllerPath = "/data/local/etc/echomuse/last_controller.json"

// lastKnownPath is a variable so tests can point it at a temp dir.
var lastKnownPath = LastKnownControllerPath

// SaveLastKnown persists the endpoint that just registered. Rewriting an
// unchanged file is skipped: a reconnect writes this every time it comes back,
// and the flash is better left alone.
func SaveLastKnown(ep *ServerInfo) error {
	raw, err := json.Marshal(staticEndpoint{Host: ep.Host, Port: ep.Port, TLSPort: ep.TLSPort})
	if err != nil {
		return err
	}
	if cur, err := os.ReadFile(lastKnownPath); err == nil && bytes.Equal(cur, raw) {
		return nil
	}
	return atomicfile.Write(lastKnownPath, raw, 0o644)
}

// LoadLastKnown reads the endpoint of the last successful registration. A
// missing or unreadable file is the normal pre-configuration state and returns
// nil: it must never keep the device from rediscovering a controller.
func LoadLastKnown() *ServerInfo {
	raw, err := os.ReadFile(lastKnownPath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		log.Printf("[discovery] %s: %v — ignoring the last-known controller", lastKnownPath, err)
		return nil
	}
	var ep staticEndpoint
	if err := json.Unmarshal(raw, &ep); err != nil {
		log.Printf("[discovery] %s is malformed: %v — ignoring the last-known controller", lastKnownPath, err)
		return nil
	}
	server, err := serverFromEndpoint(ep)
	if err != nil {
		log.Printf("[discovery] %s is invalid: %v — ignoring the last-known controller", lastKnownPath, err)
		return nil
	}
	return server
}
