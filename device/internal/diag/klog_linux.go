package diag

import "syscall"

const (
	syslogReadAll    = 3  // SYSLOG_ACTION_READ_ALL: the ring, without clearing it
	syslogSizeBuffer = 10 // SYSLOG_ACTION_SIZE_BUFFER
)

// KernelLog reads the kernel ring buffer through syslog(2), as dmesg does,
// without spawning it.
func KernelLog() ([]byte, error) {
	n, err := syscall.Klogctl(syslogSizeBuffer, nil)
	if err != nil {
		return nil, err
	}
	if n <= 0 || n > 4<<20 {
		n = 1 << 20
	}
	buf := make([]byte, n)
	n, err = syscall.Klogctl(syslogReadAll, buf)
	if err != nil {
		return nil, err
	}
	return buf[:n], nil
}
