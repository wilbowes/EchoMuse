package mixer

import (
	"fmt"
	"strconv"
)

func byteValues(name string, values []string, n int) ([]byte, error) {
	// Never broadcast one byte over a DSP profile or accept partial writes.
	if n < 1 || len(values) != n {
		return nil, fmt.Errorf("mixer: %q takes %d bytes, got %d", name, n, len(values))
	}
	out := make([]byte, n)
	for i, s := range values {
		v, err := strconv.ParseUint(s, 10, 8)
		if err != nil {
			return nil, fmt.Errorf("mixer: %q: invalid byte %q", name, s)
		}
		out[i] = byte(v)
	}
	return out, nil
}
