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
	// Thermals and CPU topology. CoresOnline is what makes CPUPct legible:
	// that figure comes from the aggregate /proc/stat line, so it is a share
	// of ONLINE capacity — the same absolute work reads as half the percentage
	// once MTK's hotplug brings a second core up. Reporting one without the
	// other invites the conclusion that load dropped when only the divisor
	// changed. ThermalCoreLimit is the sharpest throttling signal this SoC
	// offers: below 4 means the thermal governor is capping capacity, which
	// bites long before a temperature reading looks alarming.
	// AmbientLux is the room light level from the TSL2540 on i2c, or nil on
	// a device with no sensor. Pointer, not int: a covered sensor reads a
	// genuine 0 lux, so 0 must not also mean "no hardware".
	AmbientLux       *int     `json:"ambientLux"`
	CPUTempC         *float64 `json:"cpuTempC"`
	MaxTempC         *float64 `json:"maxTempC"`
	CoresOnline      int      `json:"coresOnline,omitempty"`
	CoresTotal       int      `json:"coresTotal,omitempty"`
	ThermalCoreLimit int      `json:"thermalCoreLimit,omitempty"`
	// OwwShadow carries the on-device wake word window summary
	// (shadow.Stats), nil when shadow mode is off. Riding the existing 30s
	// tick keeps the DB cost of on-device scoring at one upsert per 30s —
	// the same cost class as every other counter here, and the reason
	// per-frame scores are never sent.
	OwwShadow interface{} `json:"owwShadow,omitempty"`
	// AecRef is which far-end reference echo cancellation is running on:
	// "hw" (the frame-aligned ch8 playback loopback), "sw" (the tap at the
	// ALSA write) or "off". The aec_hw_ref capability says this firmware
	// CAN use a hardware reference; this says whether the board actually
	// provided one, which cannot be known at registration because proving
	// it needs the speaker to have played (see noteEchoRef).
	//
	// Never omitempty, unlike its neighbours: absent must mean "firmware too
	// old to report it", and "off" collapsing into that would tell the
	// dashboard a disarmed AEC is an unknown one.
	AecRef string `json:"aecRef"`
	// BaseOs is which userspace the firmware booted on: "emos", "fireos" or
	// "unknown" (see internal/platform). The controller needs it because it
	// distributes payloads that only mean anything under Android — the debloat
	// script and the pm-hide list — and a device running emOS has no package
	// manager to hide anything from.
	//
	// Not omitempty, for AecRef's reason and with more at stake: absent means
	// "firmware too old to say", which the controller must treat as unknown
	// rather than as FireOS. Reading absence as Android is how a device gets
	// sent payloads it cannot use.
	BaseOs string `json:"baseOs"`
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
		"ble":              s.Ble,
		"owwShadow":        s.OwwShadow,
		"ambientLux":       s.AmbientLux,
		"cpuTempC":         s.CPUTempC,
		"maxTempC":         s.MaxTempC,
		"coresOnline":      s.CoresOnline,
		"coresTotal":       s.CoresTotal,
		"thermalCoreLimit": s.ThermalCoreLimit,
		"aecRef":           s.AecRef,
		"baseOs":           s.BaseOs,
	})
}
