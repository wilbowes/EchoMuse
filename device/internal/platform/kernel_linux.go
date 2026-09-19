package platform

import "syscall"

// Kernel returns the running kernel's machine and release, as `uname -m` and
// `uname -r` print them, or empty strings if uname fails.
//
// Reported so the controller can tell emOS on biscuit's two kernels apart:
// base_os says "emos" and board says "biscuit" on both, but FireOS 5's kernel
// is 64-bit ("3.18.19+") and FireOS 6's is 32-bit ("3.18.19-g…").
//
// This firmware is a 32-bit program, so on the 64-bit kernel uname reports
// "armv8l" (the arm64 kernel's name for a 32-bit task), not "aarch64" —
// measured 2026-09-19 on EFF and NF; the spare's 32-bit kernel says armv7l.
// The raw value is sent and the dashboard reads armv8l as 64-bit.
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
