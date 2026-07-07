package client

import (
	"fmt"
	"os"
	"syscall"

	"golang.org/x/sys/unix"
)

// openPty allocates a pseudo-terminal pair via /dev/ptmx and returns the
// master and slave ends. FireOS 5 mounts devpts at /dev/pts (adbd relies on
// it), so no fallback discovery is needed.
func openPty() (master, slave *os.File, err error) {
	m, err := os.OpenFile("/dev/ptmx", os.O_RDWR, 0)
	if err != nil {
		return nil, nil, fmt.Errorf("open /dev/ptmx: %w", err)
	}
	n, err := unix.IoctlGetInt(int(m.Fd()), unix.TIOCGPTN)
	if err != nil {
		m.Close()
		return nil, nil, fmt.Errorf("TIOCGPTN: %w", err)
	}
	if err := unix.IoctlSetPointerInt(int(m.Fd()), unix.TIOCSPTLCK, 0); err != nil {
		m.Close()
		return nil, nil, fmt.Errorf("TIOCSPTLCK: %w", err)
	}
	// O_NOCTTY: the child acquires the slave as its controlling TTY itself
	// (Setsid+Setctty in SysProcAttr) — the parent must not.
	s, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		m.Close()
		return nil, nil, fmt.Errorf("open pts %d: %w", n, err)
	}
	return m, s, nil
}

// setWinsize applies a terminal resize to the PTY. Safe to call on the
// master end; the kernel delivers SIGWINCH to the foreground process group.
func setWinsize(f *os.File, cols, rows uint16) error {
	return unix.IoctlSetWinsize(int(f.Fd()), unix.TIOCSWINSZ, &unix.Winsize{
		Col: cols,
		Row: rows,
	})
}
