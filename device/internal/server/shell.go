package server

// Shell sessions are initiated outbound by the device on receipt of a
// shell_open control message from the controller. See internal/client/control.go.
//
// This file is intentionally empty — the /shell Gin route has been removed
// to preserve the device's no-inbound-ports security principle.
