package client

// DeviceStats holds the periodic hardware metrics sent to the controller.
// Fields that cannot be read are omitted from the JSON payload (WifiRssi
// uses a pointer so it marshals as null when the wireless interface is
// not available, letting the dashboard distinguish "no data" from "-0 dBm").
type DeviceStats struct {
	CPUPct         float64 `json:"cpuPct"`
	MemUsedMb      int     `json:"memUsedMb"`
	MemTotalMb     int     `json:"memTotalMb"`
	StorageUsedMb  int     `json:"storageUsedMb"`
	StorageTotalMb int     `json:"storageTotalMb"`
	WifiRssi       *int    `json:"wifiRssi"`
	WifiSsid       string  `json:"wifiSsid"`
	// Link context for playback-stall diagnosis. LinkSpeedMbps/FreqMhz/
	// Bssid come from wpa_cli and are refreshed on a slower cadence than
	// the rest (see linkInfo in cmd/server.go) because they cost a process
	// spawn; the tx/rx counters are plain sysfs reads and ride every tick.
	// Band and BSSID matter because a single SSID spanning 2.4/5GHz means
	// a device can silently re-associate to a much slower radio.
	LinkSpeedMbps  int     `json:"linkSpeedMbps,omitempty"`
	WifiFreqMhz    int     `json:"wifiFreqMhz,omitempty"`
	WifiBssid      string  `json:"wifiBssid,omitempty"`
	// Deltas since the previous stats tick — throughput and loss.
	TxBytes        uint64  `json:"txBytes"`
	RxBytes        uint64  `json:"rxBytes"`
	TxErrors       uint64  `json:"txErrors"`
	TxDropped      uint64  `json:"txDropped"`
	RxCrcErrors    uint64  `json:"rxCrcErrors"`
	// Ble carries the BLE scanner diagnostics snapshot (bluetooth.Stats),
	// nil when the proxy has never been enabled this boot.
	Ble interface{} `json:"ble,omitempty"`
}

// SendStats sends a stats message to the controller.
// Safe for concurrent use — silently drops if not connected.
func (c *ControlClient) SendStats(s DeviceStats) {
	_ = c.writeJSON(map[string]interface{}{
		"type":           "stats",
		"cpuPct":         s.CPUPct,
		"memUsedMb":      s.MemUsedMb,
		"memTotalMb":     s.MemTotalMb,
		"storageUsedMb":  s.StorageUsedMb,
		"storageTotalMb": s.StorageTotalMb,
		"wifiRssi":       s.WifiRssi,
		"wifiSsid":       s.WifiSsid,
		"linkSpeedMbps":  s.LinkSpeedMbps,
		"wifiFreqMhz":    s.WifiFreqMhz,
		"wifiBssid":      s.WifiBssid,
		"txBytes":        s.TxBytes,
		"rxBytes":        s.RxBytes,
		"txErrors":       s.TxErrors,
		"txDropped":      s.TxDropped,
		"rxCrcErrors":    s.RxCrcErrors,
		"ble":            s.Ble,
	})
}
