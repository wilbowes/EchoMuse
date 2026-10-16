//go:build server

package mixer

// Ordinary controls use functions verified on both FireOS 5 and 6. The
// compiler's header is newer than the devices' libraries: optional APIs must
// be resolved at runtime, or a missing symbol prevents even unrelated boards
// from loading the binary. Byte-array support below is one such optional API.

/*
#cgo LDFLAGS: -ltinyalsa -ldl
#include <stdlib.h>
#include <errno.h>
#include <dlfcn.h>
#include <tinyalsa/asoundlib.h>

// Resolve the byte-array API only when needed. Older boards must still load
// the binary even if their system tinyalsa does not export this entry point.
static int set_byte_array(struct mixer_ctl *ctl, const void *data, size_t count) {
    typedef int (*set_array_fn)(struct mixer_ctl *, const void *, size_t);
    set_array_fn fn = (set_array_fn)dlsym(RTLD_DEFAULT, "mixer_ctl_set_array");
    return fn ? fn(ctl, data, count) : -ENOSYS;
}
*/
import "C"

import (
	"fmt"
	"strconv"
	"unsafe"
)

func init() { Use(&tinyalsa{card: 0}) }

// tinyalsa holds the mixer open for the life of the process. Callers are
// serialised by the package mutex.
type tinyalsa struct {
	card uint
	m    *C.struct_mixer
}

func (t *tinyalsa) ctl(name string) (*C.struct_mixer_ctl, error) {
	if t.m == nil {
		t.m = C.mixer_open(C.uint(t.card))
		if t.m == nil {
			return nil, fmt.Errorf("mixer: cannot open card %d", t.card)
		}
	}
	cs := C.CString(name)
	defer C.free(unsafe.Pointer(cs))
	c := C.mixer_get_ctl_by_name(t.m, cs)
	if c == nil {
		return nil, fmt.Errorf("mixer: no control named %q on card %d", name, t.card)
	}
	return c, nil
}

func (t *tinyalsa) Set(name string, values []string) error {
	c, err := t.ctl(name)
	if err != nil {
		return err
	}
	if C.mixer_ctl_get_type(c) == C.MIXER_CTL_TYPE_BYTE {
		// Byte controls (Radar's biquad profile) must be one atomic array
		// write. mixer_ctl_set_value does not support this control type.
		buf, err := byteValues(name, values, int(C.mixer_ctl_get_num_values(c)))
		if err != nil {
			return err
		}
		if rc := C.set_byte_array(c, unsafe.Pointer(&buf[0]), C.size_t(len(buf))); rc != 0 {
			return fmt.Errorf("mixer: %q: byte array write failed (%d)", name, int(rc))
		}
		return nil
	}
	if C.mixer_ctl_get_type(c) == C.MIXER_CTL_TYPE_ENUM {
		cs := C.CString(values[0])
		defer C.free(unsafe.Pointer(cs))
		if C.mixer_ctl_set_enum_by_string(c, cs) != 0 {
			return fmt.Errorf("mixer: %q: cannot set %q", name, values[0])
		}
		return nil
	}
	vals, err := spread(name, values, int(C.mixer_ctl_get_num_values(c)))
	if err != nil {
		return err
	}
	isBool := C.mixer_ctl_get_type(c) == C.MIXER_CTL_TYPE_BOOL
	for i, s := range vals {
		var v int
		var ok bool
		if isBool {
			v, ok = boolValue(s)
		} else {
			var perr error
			v, perr = strconv.Atoi(s)
			ok = perr == nil
		}
		if !ok {
			return fmt.Errorf("mixer: %q: bad value %q", name, s)
		}
		if C.mixer_ctl_set_value(c, C.uint(i), C.int(v)) != 0 {
			return fmt.Errorf("mixer: %q: write %d=%d failed", name, i, v)
		}
	}
	return nil
}

func (t *tinyalsa) Get(name string) (string, error) {
	c, err := t.ctl(name)
	if err != nil {
		return "", err
	}
	v := C.mixer_ctl_get_value(c, 0)
	typ := C.mixer_ctl_get_type(c)
	if v < 0 && typ != C.MIXER_CTL_TYPE_INT {
		// an errno, since enum indices and switches are never negative
		return "", fmt.Errorf("mixer: %q: read failed (%d)", name, v)
	}
	switch typ {
	case C.MIXER_CTL_TYPE_ENUM:
		s := C.mixer_ctl_get_enum_string(c, C.uint(v))
		if s == nil {
			return "", fmt.Errorf("mixer: %q: enum %d has no name", name, v)
		}
		return C.GoString(s), nil
	case C.MIXER_CTL_TYPE_BOOL:
		if v != 0 {
			return "On", nil
		}
		return "Off", nil
	case C.MIXER_CTL_TYPE_INT:
		return strconv.Itoa(int(v)), nil
	}
	return "", fmt.Errorf("mixer: %q: unsupported control type", name)
}
