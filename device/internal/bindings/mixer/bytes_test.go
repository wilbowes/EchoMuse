package mixer

import "testing"

func TestByteValues(t *testing.T) {
	b, e := byteValues("profile", []string{"0", "128", "255"}, 3)
	if e != nil || len(b) != 3 || b[1] != 128 || b[2] != 255 {
		t.Fatal(b, e)
	}
	for _, v := range [][]string{nil, {"1"}, {"1", "2", "3", "4"}, {"-1", "0", "0"}, {"256", "0", "0"}, {"x", "0", "0"}} {
		if _, e := byteValues("profile", v, 3); e == nil {
			t.Fatal("accepted", v)
		}
	}
}
