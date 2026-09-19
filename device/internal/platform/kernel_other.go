//go:build !linux

package platform

// Kernel is only meaningful on Linux; see kernel_linux.go.
func Kernel() (machine, release string) { return "", "" }
