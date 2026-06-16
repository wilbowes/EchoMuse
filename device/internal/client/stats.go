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
	})
}
