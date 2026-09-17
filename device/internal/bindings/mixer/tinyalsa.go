//go:build server

package mixer

// Only functions exported by BOTH devices' /system/lib/libtinyalsa.so are
// called here (checked 2026-09-17, FireOS 5 and FireOS 6): the header is
// tinyalsa 2.x and the devices are not, and a symbol the device library lacks
// stops the binary loading at all.

// #cgo LDFLAGS: -ltinyalsa
// #include <stdlib.h>
// #include <tinyalsa/asoundlib.h>
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
