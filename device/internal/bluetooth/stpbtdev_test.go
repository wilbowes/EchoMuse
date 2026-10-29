package bluetooth

import (
	"bytes"
	"errors"
	"os"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestAHangUpIsATransportFault(t *testing.T) {
	for _, c := range []struct {
		revents int16
		fault   bool
	}{
		{unix.POLLIN, false},
		{0, false},
		{unix.POLLERR, true},
		{unix.POLLNVAL, true},
		{unix.POLLHUP, true},
		{unix.POLLIN | unix.POLLHUP, true},
	} {
		if got := pollFault(c.revents); got != c.fault {
			t.Errorf("revents %#x: fault %v, want %v", c.revents, got, c.fault)
		}
	}
}

// pipeDev is a stpbtDev over the read end of a pipe, whose write end the
// test holds: the one host-side fd that polls like a driver with a poll
// handler, readable only when something is queued.
func pipeDev(t *testing.T) (*stpbtDev, *os.File) {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { w.Close() })
	d := &stpbtDev{fd: int(r.Fd())}
	t.Cleanup(func() { d.Close() })
	return d, w
}

// Read waits past an empty poll window and returns the data once it comes,
// never a zero-byte read.
func TestReadWaitsForDataThenReturnsIt(t *testing.T) {
	d, w := pipeDev(t)
	pkt := []byte{0x04, 0x0e, 0x04, 0x01, 0x03, 0x0c, 0x00}
	go func() {
		time.Sleep(pollWait + pollWait/2)
		w.Write(pkt)
	}()
	buf := make([]byte, 64)
	n, err := d.Read(buf)
	if err != nil || !bytes.Equal(buf[:n], pkt) {
		t.Fatalf("read % x, %v; want the packet", buf[:n], err)
	}
}

// Close returns a Read blocked in poll within pollWait, as os.ErrClosed, and
// a Read after it says the same.
func TestCloseUnblocksARead(t *testing.T) {
	d, _ := pipeDev(t)
	done := make(chan error, 1)
	go func() {
		_, err := d.Read(make([]byte, 64))
		done <- err
	}()
	time.Sleep(pollWait / 4)
	start := time.Now()
	if err := d.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if !errors.Is(err, os.ErrClosed) {
			t.Errorf("read returned %v, want ErrClosed", err)
		}
		if took := time.Since(start); took > 2*pollWait {
			t.Errorf("read took %s to notice the close", took)
		}
	case <-time.After(5 * pollWait):
		t.Fatal("read still blocked after close")
	}
	if _, err := d.Read(make([]byte, 1)); !errors.Is(err, os.ErrClosed) {
		t.Errorf("read after close: %v", err)
	}
	if err := d.Close(); err != nil {
		t.Errorf("second close: %v", err)
	}
}

// The writer going away hangs the pipe up, and a hang-up ends the Read with
// an error, not a spin of zero-byte reads.
func TestAHungUpNodeEndsTheRead(t *testing.T) {
	d, w := pipeDev(t)
	w.Close()
	if _, err := d.Read(make([]byte, 64)); err == nil {
		t.Fatal("read returned no error on a hung-up node")
	}
}
