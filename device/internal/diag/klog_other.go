//go:build !linux

package diag

import "errors"

// KernelLog is only meaningful on Linux; see klog_linux.go.
func KernelLog() ([]byte, error) { return nil, errors.New("not linux") }
