package bluetooth

// stpbtDev is an HCI node opened for a driver whose read() does not block.
//
// Observed on the Echo Dot 3rd gen (FireOS 6, MT8167, driver log prefix
// btmtk_fops_*): read() returns 0 bytes when no packet is queued, in blocking
// mode too — `cat </dev/stpbt` exits at once. os.File reports a zero-byte
// read as io.EOF, so the scanner's first command (HCI Reset) died with
// "read during cmd 0c03: EOF" on every session, 430 times in a row. The Dot
// 2's driver blocks in read(), which is why the same code worked there.
//
// So a zero-byte read is "nothing yet", not end-of-file: wait with poll(2)
// (the driver has a poll handler; if it does not, the short sleep below
// bounds the cost to a few wakeups a second) and read again. A real failure
// is still an error. Read returns only data, or an error once closed.
//
// Which boards read this way is board data (Hardware.HCIPollRead), never a
// board id. The Dot 2's driver blocks in read(), and if it has no poll
// handler the kernel calls it always readable: Read would then sit in read()
// holding the lock, and Close would wait for the next HCI event, which after
// the scan is disabled may never come. os.File has worked there since the
// scanner was written, so it stays.

import (
	"errors"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/board"
	"golang.org/x/sys/unix"
)

const (
	// pollWait bounds how long one Read iteration holds the fd, and so how
	// long Close can wait for it. Not a latency: poll returns the moment
	// data arrives.
	pollWait = 200 * time.Millisecond

	// emptyReadPause is the sleep after a "readable" poll that yields no
	// bytes, which is what a driver without a poll handler produces.
	emptyReadPause = 10 * time.Millisecond
)

type stpbtDev struct {
	fd     int
	closed atomic.Bool
	mu     sync.RWMutex // Read holds R per iteration; Close takes W before closing the fd
}

// openHCI opens the board's HCI node the way its driver needs. The scanner's
// host, the connection manager and the bench all read whatever it returns.
func openHCI() (io.ReadWriteCloser, error) {
	l := board.CurrentLayout()
	if l.HCIPollRead {
		return openStpbt(l.HCI)
	}
	return os.OpenFile(l.HCI, os.O_RDWR, 0)
}

func openStpbt(path string) (*stpbtDev, error) {
	fd, err := unix.Open(path, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	return &stpbtDev{fd: fd}, nil
}

func (d *stpbtDev) Read(p []byte) (int, error) {
	for {
		if d.closed.Load() {
			return 0, os.ErrClosed
		}
		n, err := d.readOnce(p)
		if err != nil {
			return 0, err
		}
		if n > 0 {
			return n, nil
		}
	}
}

// readOnce does one poll+read under the read lock. It returns (0, nil) when
// there was nothing to read, so the caller re-checks closed and goes round.
func (d *stpbtDev) readOnce(p []byte) (int, error) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if d.closed.Load() {
		return 0, os.ErrClosed
	}
	fds := []unix.PollFd{{Fd: int32(d.fd), Events: unix.POLLIN}}
	pn, err := unix.Poll(fds, int(pollWait/time.Millisecond))
	if err != nil {
		if errors.Is(err, unix.EINTR) {
			return 0, nil
		}
		return 0, os.NewSyscallError("poll", err)
	}
	if pn == 0 {
		return 0, nil // timed out; let Close in
	}
	if pollFault(fds[0].Revents) {
		return 0, os.NewSyscallError("poll", unix.EIO)
	}
	n, err := unix.Read(d.fd, p)
	if err != nil {
		if errors.Is(err, unix.EINTR) || errors.Is(err, unix.EAGAIN) {
			return 0, nil
		}
		return 0, os.NewSyscallError("read", err)
	}
	if n == 0 {
		time.Sleep(emptyReadPause)
	}
	return n, nil
}

// Write sends one whole HCI packet. A driver that takes a packet per write
// would mis-frame a split one, so a short write is an error, not a retry.
func (d *stpbtDev) Write(b []byte) (int, error) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if d.closed.Load() {
		return 0, os.ErrClosed
	}
	for {
		n, err := unix.Write(d.fd, b)
		if err != nil {
			if errors.Is(err, unix.EINTR) {
				continue
			}
			return 0, os.NewSyscallError("write", err)
		}
		if n != len(b) {
			return n, os.NewSyscallError("write", unix.EIO)
		}
		return n, nil
	}
}

// Close makes a blocked Read return os.ErrClosed within pollWait and then
// closes the fd. Taking the write lock first means the fd number is never
// closed (and reusable by another open) while a Read is still using it.
func (d *stpbtDev) Close() error {
	if !d.closed.CompareAndSwap(false, true) {
		return nil
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return unix.Close(d.fd)
}

// pollFault says whether poll's revents mean the transport is gone. POLLHUP
// counts: a hung-up fd is readable at once with nothing to read, so without
// it the loop polled, read 0 bytes and slept 10 ms, a hundred times a second
// for as long as the session lasted, and only the scan's silence watchdog
// ever reopened the device. An error here ends the session, which reopens
// the node the way every other transport fault does.
func pollFault(revents int16) bool {
	return revents&(unix.POLLERR|unix.POLLNVAL|unix.POLLHUP) != 0
}
