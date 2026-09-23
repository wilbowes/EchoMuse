//go:build !bench

package main

import "log"

func runRaw(int, int, int, bool, string, int, int, string) {
	log.Fatal("-raw needs a build with -tags bench")
}
