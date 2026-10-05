package config

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

// ReportPath is where the device records the config it was sent and the
// config it is running, after every config push. It exists so release UAT
// (tools/uat) can check over USB that a dashboard control reached the Echo
// and took effect, and so a support session can see the same. /tmp is RAM
// on both userspaces, so a push costs no flash write.
const ReportPath = "/tmp/em-config.json"

var (
	receivedMu sync.Mutex
	received   = map[string]json.RawMessage{}
)

// RecordReceived merges one config message into the running record of what
// the controller has sent, key by key, since a push carries only what changed
// or everything, depending on the caller. The console password is recorded
// as present, never its value.
func RecordReceived(raw []byte) {
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil {
		return
	}
	receivedMu.Lock()
	defer receivedMu.Unlock()
	for k, v := range m {
		if k == "type" {
			continue
		}
		if k == "consolePassword" {
			v = json.RawMessage(`"<set>"`)
			if string(m[k]) == `""` {
				v = json.RawMessage(`""`)
			}
		}
		received[k] = v
	}
}

// WriteReport writes ReportPath: what was received, and what is applied.
func (d *Device) WriteReport() error {
	receivedMu.Lock()
	recv := make(map[string]json.RawMessage, len(received))
	for k, v := range received {
		recv[k] = v
	}
	receivedMu.Unlock()
	b, err := json.Marshal(map[string]interface{}{
		"atUnixMs": time.Now().UnixMilli(),
		"received": recv,
		"applied":  d.Snapshot(),
		"output":   d.OutputChain(),
	})
	if err != nil {
		return err
	}
	tmp := ReportPath + ".tmp"
	if err := os.WriteFile(tmp, b, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, ReportPath)
}
