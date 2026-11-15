package client

import "path/filepath"

// thermalZones is a variable only so the test can point it at a fixture.
var thermalZones = "/sys/class/thermal/thermal_zone*/temp"

// thermalReadable reports whether the kernel exposes any thermal zone, which
// decides the "thermal" capability: the controller advertises temperature
// entities in Home Assistant off the back of it, and an entity that can never
// produce a reading is worse than no entity at all. The zones themselves are
// read by type in cmd/server.go for the stats report.
func thermalReadable() bool {
	zones, err := filepath.Glob(thermalZones)
	return err == nil && len(zones) > 0
}
