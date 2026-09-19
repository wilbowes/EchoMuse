package platform

import "syscall"

// Kernel returns the running kernel's machine and release, as `uname -m` and
// `uname -r` print them, or empty strings if uname fails.
//
// Reported so the controller can tell emOS on biscuit's two kernels apart:
// base_os says "emos" and board says "biscuit" on both, but FireOS 5's kernel
// is 64-bit (aarch64, "3.18.19+") and FireOS 6's is 32-bit (armv7l,
// "3.18.19-g…"), measured 2026-09-19 on EFF and the spare. This firmware is a
// 32-bit binary either way and sets no personality, so it sees the kernel's
// own answer — the same one emOS's 32-bit busybox printed on both.
func Kernel() (machine, release string) {
	var u syscall.Utsname
	if syscall.Uname(&u) != nil {
		return "", ""
	}
	return cstr(u.Machine[:]), cstr(u.Release[:])
}

// Utsname's fields are int8 on some architectures and uint8 on others.
func cstr[T ~int8 | ~uint8](b []T) string {
	out := make([]byte, 0, len(b))
	for _, c := range b {
		if c == 0 {
			break
		}
		out = append(out, byte(c))
	}
	return string(out)
}
