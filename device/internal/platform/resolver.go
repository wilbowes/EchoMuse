package platform

import "net"

// UseResolverFor picks the DNS resolver for the base the firmware booted on.
//
// The firmware is built for GOOS=android with cgo, so Go resolves names
// through bionic's getaddrinfo, which on Android asks netd over
// /dev/socket/dnsproxyd. emOS runs no netd, so every lookup failed ("no such
// host") on both 32- and 64-bit emOS, while Go's own resolver, reading the
// /etc/resolv.conf emOS's init writes from the DHCP lease (#1066), worked
// (15LE and C95, 2026-11-02). FireOS has netd and no /etc/resolv.conf, so it
// keeps bionic's. An Unknown base gets Go's: a Linux file beats an Android
// daemon that may not be there.
func UseResolverFor(r *net.Resolver, base string) {
	r.PreferGo = base != FireOS
}
